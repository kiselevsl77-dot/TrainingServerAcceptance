"""Состояние процесса пульта: настройки, клиенты, журнал, сессия и кэш реестров.

Перенос «клея» из `acceptance/ui/state.py` (модуль удалён вместе со Streamlit): сервер пульта
(`pult/server.py`) обслуживает одного оператора, поэтому httpx-клиент, журнал
обмена и выбранная сессия испытаний живут в процессе, а не в браузере.

Ключевые решения (те же, что приняты на этапах T0–T5):

* журнал (`Journal`) и httpx-клиент — ресурсы процесса: они создаются один раз и
  не теряют обмены между запросами интерфейса;
* журналирование перенастраивается при смене сессии: каждой сессии соответствует
  свой JSONL-файл (`acceptance_data/logs/session_<id>.jsonl`);
* сессия испытаний хранится на диске и читается при каждом запросе — она переживает
  перезапуск пульта (`FR-P-4`);
* реестры стенда читаются с коротким TTL-кэшем, который сбрасывается явно
  (`refresh`): оператор видит свежие данные, не нагружая общий стенд.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from acceptance import programme as programme_api
from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.api import Apis, build_client
from acceptance.config import PultConfig, load_config
from acceptance.exchange import build_console_client
from acceptance.http_log import HttpExchange, Journal
from acceptance.logging_setup import LoggingArtifacts, setup_logging
from acceptance.paths import ensure_dirs
from acceptance.session import TestSession
from client.errors import ClientError
from client.http import ApiHttpClient
from client.schemas import (
    CeleryTask,
    DatasetResponse,
    FileMetadataResponse,
    LoadItem,
    ModelMetadata,
)
from client.settings import TrainingServerSettings, get_settings

#: Текст состояния «стенд не настроен»: его показывают экраны вместо данных.
STAND_NOT_CONFIGURED = "Адрес испытуемого сервера не задан (TRAINING_SERVER_BASE_URL в .env)."

#: Время жизни кэша реестров: файлы и нагрузки меняются редко, задачи — часто.
FILES_TTL = 60.0
LOADS_TTL = 60.0
DATASETS_TTL = 30.0
MODELS_TTL = 30.0
TASKS_TTL = 15.0

#: Время жизни кэша составов «все датасеты» (режим «вне датасетов»): за каждым составом
#: стоит обращение к `$Задачам` стенда, поэтому повторные запросы страницы берут кэш.
COMPOSITIONS_TTL = 60.0

#: Верхняя граница выдачи при чтении реестра нагрузок (спецификацией не объявлена).
LOAD_LIMIT = 1000

#: Размер страницы списка задач (спецификация: по умолчанию 100).
TASK_LIMIT = 100


@dataclass
class Runtime:
    """Всё, что нужно запросам: настройки пульта и стенда, журнал, клиент, сервисы API."""

    config: PultConfig
    settings: TrainingServerSettings
    journal: Journal
    client: ApiHttpClient
    apis: Apis


def _error_text(exc: Exception) -> str:
    """Текст ошибки для оператора (ошибка клиента или сети не скрывается)."""
    return str(exc).strip() or exc.__class__.__name__


#: Ошибки чтения реестров: клиент, сеть и **ответ не по схеме**. Последнее — обычное
#: состояние во время испытаний (стенд в работе), поэтому реестр отдаёт ошибку текстом,
#: а не исключение: экран показывает, что именно не прочитано (`FR-P-25`).
READ_ERRORS: tuple[type[Exception], ...] = (ClientError, httpx.HTTPError, ValidationError)


class PultState:
    """Состояние пульта: один экземпляр на процесс, потокобезопасный доступ."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtime: Runtime | None = None
        self._console: httpx.Client | None = None
        self._journals: dict[int, Journal] = {}
        self._logging: dict[str, LoggingArtifacts] = {}
        self._session_id: str | None = None
        self._log_level: str = ""
        self._cache: dict[str, tuple[float, Any]] = {}
        self._sets: sets_api.SetsLibrary | None = None

    # -- настройки ----------------------------------------------------------
    def config(self) -> PultConfig:
        """Настройки пульта из окружения (`.env`)."""
        return load_config()

    def log_level(self) -> str:
        """Текущий уровень журналирования: выбранный оператором или из окружения."""
        return self._log_level or self.config().log_level

    def set_log_level(self, level: str) -> str:
        """Меняет уровень журналирования и перенастраивает журнал."""
        self._log_level = str(level or "").strip()
        self._logging.clear()
        self.ensure_logging(self._session_id)
        return self.log_level()

    def ensure_logging(self, session_id: str | None = None) -> LoggingArtifacts:
        """Настраивает журналирование под сессию и уровень (идемпотентно)."""
        key = f"{session_id or ''}|{self.log_level()}"
        with self._lock:
            cached = self._logging.get(key)
            if cached is not None:
                return cached
            ensure_dirs()
            artifacts = setup_logging(
                level=self.log_level(),
                session_id=session_id,
                enqueue=self.config().log_enqueue,
            )
            self._logging[key] = artifacts
            return artifacts

    # -- клиенты ------------------------------------------------------------
    def journal(self, max_records: int | None = None) -> Journal:
        """Журнал обмена с сервером (один на процесс, лимит задаёт конфигурация)."""
        limit = int(max_records or self.config().journal_max)
        with self._lock:
            journal = self._journals.get(limit)
            if journal is None:
                journal = Journal(max_records=limit)
                self._journals[limit] = journal
            return journal

    def runtime(self) -> Runtime | None:
        """Runtime пульта или None, если base URL испытуемого сервера не задан."""
        settings = get_settings()
        if not settings.is_configured:
            return None
        config = self.config()
        with self._lock:
            current = self._runtime
            if (
                current is not None
                and current.settings.base_url == settings.base_url
                and current.settings.timeout == settings.timeout
                and current.config.journal_max == config.journal_max
            ):
                return current
            journal = self.journal(config.journal_max)
            client = build_client(settings, journal, config=config)
            runtime = Runtime(
                config=config,
                settings=settings,
                journal=journal,
                client=client,
                apis=Apis.build(client, download_timeout=settings.download_timeout),
            )
            self._runtime = runtime
            return runtime

    def console_client(self) -> httpx.Client | None:
        """«Сырой» httpx-клиент консоли запросов; None — стенд не настроен (FR-P-59)."""
        runtime = self.runtime()
        if runtime is None:
            return None
        with self._lock:
            if self._console is None:
                self._console = build_console_client(
                    runtime.settings, runtime.journal, config=runtime.config
                )
            return self._console

    # -- сессия испытаний ---------------------------------------------------
    def session_id(self) -> str | None:
        """Идентификатор выбранной сессии испытаний."""
        return self._session_id

    def set_session(self, session_id: str | None) -> str | None:
        """Выбирает сессию (None — снять выбор) и переключает журналирование."""
        key = str(session_id or "").strip() or None
        if key and not session_api.session_exists(key):
            raise KeyError(f"сессия {key} не найдена")
        self._session_id = key
        self.ensure_logging(key)
        return key

    def current_session(self) -> TestSession | None:
        """Текущая сессия испытаний, прочитанная с диска (None — не выбрана)."""
        key = self._session_id
        if not key:
            return None
        try:
            return session_api.load_session(key)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def store_session(self, session: TestSession) -> TestSession:
        """Сохраняет сессию на диск и делает её текущей."""
        session_api.save_session(session)
        self._session_id = session.session_id
        return session

    def sessions(self) -> list[dict[str, Any]]:
        """Список сессий на диске (метаданные: id, наименование, статус, прогресс)."""
        return session_api.list_sessions()

    # -- реестры стенда (кэш) ----------------------------------------------
    def _cached(self, key: str, ttl: float, loader: Callable[[], Any], *, refresh: bool) -> Any:
        """Значение из TTL-кэша: `refresh=True` читает заново и обновляет запись."""
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if not refresh and entry is not None and now - entry[0] < ttl:
                return entry[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def invalidate(self, *keys: str) -> None:
        """Сбрасывает кэш по ключам (`files`, `loads`, `datasets`, `tasks`, `stand`)."""
        with self._lock:
            if not keys:
                self._cache.clear()
                return
            for key in keys:
                self._cache.pop(key, None)

    def files(self, *, refresh: bool = False) -> tuple[list[FileMetadataResponse], str | None]:
        """Реестр `$Файлов` стенда: (файлы, текст ошибки); ошибка не бросается."""

        def load() -> tuple[list[FileMetadataResponse], str | None]:
            runtime = self.runtime()
            if runtime is None:
                return [], STAND_NOT_CONFIGURED
            try:
                return list(runtime.apis.files.list_files().files), None
            except READ_ERRORS as exc:
                return [], _error_text(exc)

        return self._cached("files", FILES_TTL, load, refresh=refresh)

    def loads(self, *, refresh: bool = False) -> tuple[list[LoadItem], str | None]:
        """Реестр `$Нагрузок` стенда: (нагрузки, текст ошибки)."""

        def load() -> tuple[list[LoadItem], str | None]:
            runtime = self.runtime()
            if runtime is None:
                return [], STAND_NOT_CONFIGURED
            try:
                result = runtime.apis.loads.list_loads(limit=LOAD_LIMIT, offset=0)
                return list(result.loads), None
            except READ_ERRORS as exc:
                return [], _error_text(exc)

        return self._cached("loads", LOADS_TTL, load, refresh=refresh)

    def datasets(self, *, refresh: bool = False) -> tuple[list[DatasetResponse], str | None]:
        """Реестр `$Датасетов` стенда: (датасеты, текст ошибки)."""

        def load() -> tuple[list[DatasetResponse], str | None]:
            runtime = self.runtime()
            if runtime is None:
                return [], STAND_NOT_CONFIGURED
            try:
                return list(runtime.apis.datasets.list_datasets().datasets), None
            except READ_ERRORS as exc:
                return [], _error_text(exc)

        return self._cached("datasets", DATASETS_TTL, load, refresh=refresh)

    def models(self, *, refresh: bool = False) -> tuple[list[ModelMetadata], str | None]:
        """Реестр `$Моделей` стенда: (модели, текст ошибки).

        Нужен дважды: как подраздел «Данных стенда» (`UC-18`) и как варианты выбора
        модели для проверок T6–T9 (`llm_tasks/49`, вопрос 4 = а). На подготовленном
        стенде моделей может не быть вовсе — это не ошибка, а состояние (`[]`).
        """

        def load() -> tuple[list[ModelMetadata], str | None]:
            runtime = self.runtime()
            if runtime is None:
                return [], STAND_NOT_CONFIGURED
            try:
                return list(runtime.apis.models.list_models()), None
            except READ_ERRORS as exc:
                return [], _error_text(exc)

        return self._cached("models", MODELS_TTL, load, refresh=refresh)

    def composition_cache(
        self,
        loader: Callable[[], Any],
        *,
        key: str = "compositions|all",
        refresh: bool = False,
    ) -> Any:
        """Составы датасетов из кэша: (ключ, загрузчик) → значение.

        Режим «файлы вне датасетов» требует составов всех датасетов (`llm_tasks/49`,
        вопрос 3 = а): без кэша каждый запрос страницы читал бы задачу наполнения для
        каждого датасета. Сброс тот же, что у реестров: `refresh=1` или явный
        `invalidate("compositions|all")`.
        """
        return self._cached(key, COMPOSITIONS_TTL, loader, refresh=refresh)

    def tasks(
        self,
        *,
        task_type: str = "",
        status: str = "",
        limit: int = TASK_LIMIT,
        offset: int = 0,
        refresh: bool = False,
    ) -> tuple[list[CeleryTask], int, str | None]:
        """Список `$Задач` стенда: (задачи, всего на сервере, текст ошибки)."""

        def load() -> tuple[list[CeleryTask], int, str | None]:
            runtime = self.runtime()
            if runtime is None:
                return [], 0, STAND_NOT_CONFIGURED
            try:
                response = runtime.apis.tasks.list_tasks(
                    task_type=task_type or None,
                    status=status or None,
                    limit=limit or None,
                    offset=offset or None,
                )
            except READ_ERRORS as exc:
                return [], 0, _error_text(exc)
            return list(response.tasks), int(response.count or 0), None

        key = f"tasks|{task_type}|{status}|{limit}|{offset}"
        return self._cached(key, TASKS_TTL, load, refresh=refresh)

    def stand_status(self) -> dict[str, Any] | None:
        """Кэш последней проверки связи со стендом (версия сборки для шапки)."""
        with self._lock:
            entry = self._cache.get("stand")
        return dict(entry[1]) if entry is not None else None

    def set_stand_status(self, payload: dict[str, Any]) -> None:
        """Запоминает результат проверки связи (шапка и экран «Стенд»)."""
        with self._lock:
            self._cache["stand"] = (time.monotonic(), dict(payload))

    # -- планирование: наборы и программа -----------------------------------
    def sets_library(self, *, refresh: bool = False) -> sets_api.SetsLibrary:
        """Библиотека наборов проверок (`acceptance_data/check_sets.json`)."""
        with self._lock:
            library = self._sets
            if library is None or refresh:
                library = sets_api.load_sets()
                self._sets = library
            return library

    def save_sets_library(self, library: sets_api.SetsLibrary) -> Path:
        """Сохраняет библиотеку наборов на диск и делает её текущей."""
        path = sets_api.save_sets(library)
        with self._lock:
            self._sets = library
        return path

    def seed_sets_library(self, *, author: str = "") -> sets_api.SetsLibrary:
        """Создаёт стартовые наборы из каталога проверок (кнопка «Создать наборы»)."""
        library = sets_api.library_from_catalog(author=author)
        self.save_sets_library(library)
        return library

    def current_programme(self) -> programme_api.Programme:
        """Программа текущей сессии (пустая, если сессия не выбрана)."""
        session = self.current_session()
        if session is None:
            return programme_api.Programme()
        return programme_api.load_programme(session)

    def store_programme(
        self,
        programme: programme_api.Programme,
        *,
        event: str = "",
        message: str = "",
    ) -> programme_api.Programme | None:
        """Сохраняет программу в текущую сессию; None — если сессии нет."""
        session = self.current_session()
        if session is None:
            return None
        programme_api.save_programme(session, programme, event=event, message=message)
        self.store_session(session)
        return programme

    # -- журнал обмена ------------------------------------------------------
    def journal_records(self) -> tuple[HttpExchange, ...]:
        """Записи журнала обмена текущего процесса (лента прогона, `FR-P-21`)."""
        return tuple(self.journal().records)
