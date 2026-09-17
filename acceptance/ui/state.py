"""Состояние пульта: настройки, журналирование, клиент API и текущая сессия.

Ключевые решения:
    * журнал (`Journal`) и httpx-клиент — ресурсы процесса пульта
      (`st.cache_resource`), поэтому перерисовка экрана не создаёт новые
      соединения и не теряет журнал;
    * журналирование перенастраивается при смене сессии испытаний: каждой сессии
      соответствует свой JSONL-файл (`logs/session_<id>.jsonl`);
    * сессия испытаний хранится на диске и читается при каждой перерисовке —
      она переживает перезапуск приложения (FR-T5).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any

import httpx
import streamlit as st

from acceptance.api import Apis, build_client
from acceptance.config import PultConfig, load_config
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal
from acceptance.logging_setup import LoggingArtifacts, log_event, setup_logging
from acceptance.paths import ensure_dirs
from acceptance.session import (
    TestSession,
    load_session,
    save_session,
    session_exists,
)
from acceptance.tasks_monitor import TaskMonitor, task_card
from client.errors import ClientError
from client.http import ApiHttpClient
from client.schemas import CeleryTask, FileMetadataResponse
from client.settings import TrainingServerSettings, get_settings

KEY_SESSION_ID = "pult_session_id"
KEY_STAND_STATUS = "pult_stand_status"
KEY_LOG_LEVEL = "pult_log_level"


@dataclass
class Runtime:
    """Всё, что нужно экранам: настройки, журнал, клиент и сервисы API."""

    config: PultConfig
    settings: TrainingServerSettings
    journal: Journal
    client: ApiHttpClient
    apis: Apis


@st.cache_resource(show_spinner=False)
def _journal(max_records: int) -> Journal:
    """Журнал обмена с сервером (один на процесс пульта)."""
    return Journal(max_records=max_records)


@st.cache_resource(show_spinner=False)
def _client(
    base_url: str,
    timeout: float,
    body_limit: int,
    log_bodies: bool,
    max_records: int,
) -> ApiHttpClient:
    """httpx-клиент с логирующим транспортом (кэшируется по параметрам подключения)."""
    settings = TrainingServerSettings(base_url=base_url, timeout=timeout)
    config = PultConfig(body_limit=body_limit, log_bodies=log_bodies, journal_max=max_records)
    return build_client(settings, _journal(max_records), config=config)


@st.cache_resource(show_spinner=False)
def _console_client(
    base_url: str,
    timeout: float,
    body_limit: int,
    log_bodies: bool,
    max_records: int,
) -> httpx.Client:
    """«Сырой» httpx-клиент консоли запросов (FR-T3).

    Консоли нужны статус, заголовки и сырое тело ответа, поэтому она работает
    отдельным клиентом, но с тем же `LoggingTransport`: записи консоли попадают
    в общий журнал пульта и в JSONL текущей сессии.
    """
    settings = TrainingServerSettings(base_url=base_url, timeout=timeout)
    config = PultConfig(body_limit=body_limit, log_bodies=log_bodies, journal_max=max_records)
    return build_console_client(settings, _journal(max_records), config=config)


def console_client() -> httpx.Client | None:
    """Клиент консоли запросов; None, если стенд не настроен (FR-T3).

    Настройки и журнал берутся из текущего `Runtime`, поэтому консоль использует
    тот же журнал, что экраны и проверки (запросы консоли видны в «Журнале» и в
    JSONL сессии). В тестах подменяется `_console_client` — точка кэширования.
    """
    runtime = get_runtime()
    if runtime is None:
        return None
    return _console_client(
        runtime.settings.base_url,
        runtime.settings.timeout,
        runtime.config.body_limit,
        runtime.config.log_bodies,
        runtime.config.journal_max,
    )


def task_monitor() -> TaskMonitor | None:
    """Монитор задач поверх текущего `Runtime` (FR-8, этап T4).

    Монитор не хранит состояние — наблюдение живёт в сессии (`session.tasks`),
    поэтому его можно собирать на каждую перерисовку экрана. Интервал поллинга
    берётся из `PULT_POLL_INTERVAL`; None, если стенд не настроен.
    """
    runtime = get_runtime()
    if runtime is None:
        return None
    return TaskMonitor(
        runtime.apis.tasks,
        journal=runtime.journal,
        interval=runtime.config.poll_interval,
    )


@st.cache_resource(show_spinner=False)
def ensure_logging(level: str, session_id: str | None) -> LoggingArtifacts:
    """Настраивает журналирование под текущий уровень и сессию (идемпотентно)."""
    ensure_dirs()
    config = load_config()
    return setup_logging(
        level=level,
        session_id=session_id,
        enqueue=config.log_enqueue,
    )


def get_config() -> PultConfig:
    """Настройки пульта из окружения."""
    return load_config()


def log_level() -> str:
    """Текущий уровень журналирования (оператор может изменить его в сайдбаре)."""
    return str(st.session_state.get(KEY_LOG_LEVEL) or get_config().log_level)


def set_log_level(level: str) -> None:
    """Меняет уровень журналирования (применяется при следующей перерисовке)."""
    st.session_state[KEY_LOG_LEVEL] = level


def get_runtime() -> Runtime | None:
    """Runtime пульта или None, если base URL испытуемого сервера не задан."""
    settings = get_settings()
    if not settings.is_configured:
        return None

    config = get_config()
    client = _client(
        settings.base_url,
        settings.timeout,
        config.body_limit,
        config.log_bodies,
        config.journal_max,
    )
    return Runtime(
        config=config,
        settings=settings,
        journal=_journal(config.journal_max),
        client=client,
        apis=Apis.build(client, download_timeout=settings.download_timeout),
    )


# ---------------------------------------------------------------------------
# Сессия испытаний
# ---------------------------------------------------------------------------
def current_session_id() -> str | None:
    """Идентификатор выбранной сессии испытаний."""
    value = st.session_state.get(KEY_SESSION_ID)
    return str(value) if value else None


def set_current_session(session: TestSession) -> None:
    """Делает сессию текущей (без записи на диск)."""
    st.session_state[KEY_SESSION_ID] = session.session_id


def clear_current_session() -> None:
    """Снимает выбор сессии (файл на диске остаётся)."""
    st.session_state.pop(KEY_SESSION_ID, None)


def current_session() -> TestSession | None:
    """Текущая сессия испытаний, прочитанная с диска."""
    session_id = current_session_id()
    if not session_id or not session_exists(session_id):
        return None
    try:
        return load_session(session_id)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log_event(
            "session_load_failed",
            f"Не удалось прочитать сессию {session_id}: {exc}",
            level="ERROR",
            module="session",
            payload={"session_id": session_id},
        )
        return None


def store_session(session: TestSession) -> TestSession:
    """Сохраняет сессию на диск и делает её текущей."""
    save_session(session)
    set_current_session(session)
    return session


# ---------------------------------------------------------------------------
# Снимок стенда (кэш на время работы оператора)
# ---------------------------------------------------------------------------
def stand_status() -> dict[str, Any] | None:
    """Кэшированный результат проверки доступности/версии сервера."""
    value = st.session_state.get(KEY_STAND_STATUS)
    return value if isinstance(value, dict) else None


def set_stand_status(payload: dict[str, Any]) -> None:
    """Запоминает результат проверки доступности/версии сервера."""
    st.session_state[KEY_STAND_STATUS] = payload


def clear_stand_status() -> None:
    """Сбрасывает кэш проверки доступности сервера."""
    st.session_state.pop(KEY_STAND_STATUS, None)


# ---------------------------------------------------------------------------
# Реестр файлов (общий кэш: реестр читают и «Записи», и «Проверки»)
# ---------------------------------------------------------------------------
FILES_CACHE_TTL = 60.0


@st.cache_data(ttl=FILES_CACHE_TTL, show_spinner="Загрузка реестра файлов…")
def load_files() -> tuple[list[FileMetadataResponse], str | None]:
    """Читает реестр файлов испытуемого сервера (кэш 60 с).

    Returns:
        Кортеж (файлы, текст ошибки). При ошибке список пуст, а текст показывается
        оператору — экран не «падает». Кэш короче шага испытаний, поэтому только что
        загруженные файлы видны почти сразу; кнопка обновления сбрасывает кэш.
    """
    runtime = get_runtime()
    if runtime is None:
        return [], "Адрес испытуемого сервера не задан (TRAINING_SERVER_BASE_URL в .env)."

    try:
        response = runtime.apis.files.list_files()
    except ClientError as exc:
        return [], _error_text(exc)
    except httpx.HTTPError as exc:
        return [], f"Сеть недоступна: {exc}"
    return list(response.files), None


def refresh_files() -> None:
    """Сбрасывает кэш реестра файлов (кнопка «Обновить реестр»)."""
    load_files.clear()


# ---------------------------------------------------------------------------
# Список задач испытуемого сервера (кэш: экран «Задачи» и пикер `task_id` в консоли)
# ---------------------------------------------------------------------------
TASKS_CACHE_TTL = 15.0


@st.cache_data(ttl=TASKS_CACHE_TTL, show_spinner="Загрузка списка задач…")
def load_tasks(
    task_type: str = "",
    status: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[CeleryTask], int, str | None]:
    """Читает список задач испытуемого сервера (`GET /api/tasks/`, UC-27).

    Args:
        task_type: фильтр по типу задачи (значение перечисления `TaskType`).
        status: фильтр по статусу (значение перечисления `TaskStatus`).
        start_date: начало периода создания задачи (ISO-строка).
        end_date: конец периода создания задачи (ISO-строка).
        limit: размер страницы (спецификация: по умолчанию 100).
        offset: смещение для серверной пагинации.

    Returns:
        Кортеж (задачи, всего задач на сервере, текст ошибки). Кэш короткий
        (шаг испытаний), поэтому только что созданная задача видна почти сразу;
        задача с фильтрами кэшируется отдельно по каждому набору параметров.
    """
    runtime = get_runtime()
    if runtime is None:
        return [], 0, "Адрес испытуемого сервера не задан (TRAINING_SERVER_BASE_URL в .env)."

    try:
        response = runtime.apis.tasks.list_tasks(
            task_type=task_type or None,
            status=status or None,
            start_date=start_date or None,
            end_date=end_date or None,
            limit=limit or None,
            offset=offset or None,
        )
    except ClientError as exc:
        return [], 0, _error_text(exc)
    except httpx.HTTPError as exc:
        return [], 0, f"Сеть недоступна: {exc}"
    return list(response.tasks), int(response.count), None


def refresh_tasks() -> None:
    """Сбрасывает кэш списка задач и карточек (кнопка «Обновить список»)."""
    load_tasks.clear()
    clear_task_cards()


# ---------------------------------------------------------------------------
# Карточки строк списка задач (кэш: экран «Задачи», вкладка «Список сервера»)
# ---------------------------------------------------------------------------
#: Статус задачи в `GET /api/tasks/` не возвращается (в спецификации у `CeleryTask`
#: только `type`, `name`, `description`, `id`, `created_at`), поэтому он берётся из
#: карточки `GET /api/tasks/{id}` — один запрос на задачу (N+1). Время жизни кэша
#: больше прежнего: таблица рисуется из снимка и опроса сессии, а карточки
#: догружаются батчами в фоне, поэтому 30 с не «примораживают» картину.
STATUS_CACHE_TTL = 30.0

#: Число параллельных запросов карточек (замер на стенде 16.09.2026: 10 карточек —
#: ≈ 0,7 с в 5 потоков против ≈ 7,4 с последовательно).
STATUS_FETCH_WORKERS = 5

#: Размер батча догрузки статусов: столько карточек запрашивается за один проход,
#: после чего страница перерисовывается и берётся следующий батч.
STATUS_BATCH = 25

#: Предел задач, читаемых одним списком (`limit` сервера не ограничен, поэтому
#: список режется на стороне пульта — защита от тысяч строк).
LOAD_TASK_CAP = 500


@st.cache_data(ttl=STATUS_CACHE_TTL, show_spinner="Получение статусов задач…")
def load_task_cards(
    task_ids: tuple[str, ...],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Проекции карточек задач: статус, тип, название и времена последнего прогона.

    Экран «Задачи»: в элементах списка статуса нет, поэтому запрашиваются карточки
    (N+1 запрос) и берётся `runtimes[-1]` (`current_status`). Запросы идут
    параллельно (`STATUS_FETCH_WORKERS`) и помечаются меткой проверки — она живёт в
    `contextvars` вызывающего потока, поэтому контекст копируется в рабочие потоки.
    Результат кэшируется на `STATUS_CACHE_TTL` секунд.

    Args:
        task_ids: идентификаторы задач одного батча (не более `STATUS_BATCH`).

    Returns:
        Кортеж (проекции карточек по `task_id`, тексты ошибок по `task_id`): задача,
        для которой карточка не получена, попадает во второй словарь и не мешает
        остальным.
    """
    runtime = get_runtime()
    if runtime is None:
        return {}, dict.fromkeys(task_ids, "Адрес испытуемого сервера не задан")

    keys = tuple(key for key in (str(item).strip() for item in task_ids) if key)
    cards: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    def fetch(key: str) -> tuple[str, dict[str, Any] | None, str]:
        """Запрашивает карточку задачи, возвращая проекцию или текст ошибки."""
        try:
            task = runtime.apis.tasks.get_task(key)
        except ClientError as exc:
            return key, None, _error_text(exc)
        except httpx.HTTPError as exc:
            return key, None, f"Сеть недоступна: {exc}"
        return key, task_card(task), ""

    limited = keys[:STATUS_BATCH]
    contexts = [copy_context() for _ in limited]
    with ThreadPoolExecutor(max_workers=STATUS_FETCH_WORKERS) as pool:
        futures = [
            pool.submit(context.run, fetch, key)
            for context, key in zip(contexts, limited, strict=True)
        ]
        for future in futures:
            key, card, error = future.result()
            if card is None:
                errors[key] = error or "карточка не получена"
            else:
                cards[key] = card
    return cards, errors


def clear_task_cards() -> None:
    """Сбрасывает кэш карточек строк списка задач."""
    load_task_cards.clear()


def current_markup_stats() -> dict[str, dict[str, Any]]:
    """Рассчитанные характеристики разметки из текущей сессии (по `id` файла)."""
    session = current_session()
    if session is None:
        return {}
    return {str(key): dict(value) for key, value in session.markup_stats.items()}


def _error_text(exc: Exception) -> str:
    """Текст ошибки API, пригодный для показа оператору."""
    status = getattr(exc, "status_code", None)
    return f"[{status}] {exc}" if status else str(exc)
