"""Сервер пульта: тонкий JSON-API (20 маршрутов) и раздача статического интерфейса.

Замена Streamlit (`llm_tasks/31.4` §3): сервер отвечает на `/api/*` готовыми
view-моделями (`pult/view.py`), а браузер только рисует их — бизнес-логики в JS нет.

Правила, которые держат контракт:

* **единый конверт ответа**: `{"ok": true, "data": …}` либо
  `{"ok": false, "error": {"kind", "message", "exchange_seq"}}` (§4 ТЗ);
* **один процесс — один оператор**: httpx-клиент, журнал обмена и выбранная сессия
  живут в `pult.state.PultState` и переживают перезагрузку страницы;
* **прогон не параллелен**: тяжёлые действия («Следующая», «Пачка tech») держат
  замок `_run_lock`, поэтому случайный двойной клик не отправляет проверку дважды;
* **статику отдаём с проверкой пути**: запрос не может выйти за `pult/web`
  (`Path.resolve()` + проверка родителя), даже если в URL пришли `..`;
* **статика — для отображения, а не для скачивания**: страница и её подресурсы уходят
  без `Content-Disposition` (у `Raw` признак `download`), `attachment` остаётся только
  у выгрузок вроде CSV протокола — иначе браузер скачивает `index.html` вместо показа.

Запуск: `python -m pult.server` (по умолчанию `http://127.0.0.1:8502`).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from acceptance import api, run_card, run_state
from acceptance import dataset_composition as composition_api
from acceptance import programme as programme_api
from acceptance import queue as queue_api
from acceptance import records as records_api
from acceptance import report as report_api
from acceptance import results as results_api
from acceptance import runner as runner_api
from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.checks import catalog
from acceptance.exchange import execute_request
from acceptance.runner import (
    execute_call,
    execute_next,
    next_label,
    run_batch_tech,
)
from acceptance.session import TestSession
from acceptance.tasks_monitor import TASK_COMMANDS, TaskMonitor
from client.errors import ClientError
from pult import jobs as jobs_api
from pult import view
from pult.state import READ_ERRORS, STAND_NOT_CONFIGURED, TASK_LIMIT, PultState

#: Порт по умолчанию: 8502 — тот же, что был у Streamlit-пульта (привычка оператора).
DEFAULT_PORT = 8502

#: Узел прослушивания: пульт локальный, наружу не публикуется (`NFR-P-7`).
DEFAULT_HOST = "127.0.0.1"

#: Типы файлов статики: без него браузер скачает JS как текст и не выполнит.
MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".md": "text/markdown; charset=utf-8",
}

#: Ограничение размера тела запроса интерфейса (правка вызова, карточка запуска).
MAX_BODY = 1 << 20

LOG = logging.getLogger("pult.server")


@dataclass
class DataFilterState:
    """Состояние фильтра «Данных стенда»: отбор строк и факты для блока `filter`.

    Собирается в `PultApp._filter_state` (состав выбранного датасета или объединение
    составов для режима «вне датасетов»), а в вид попадает после расчёта страниц:
    подпись фильтра говорит, сколько строк показано из скольких (`llm_tasks/49`, 1а).
    """

    selection: view.RowSelection = view.NO_SELECTION
    options: list[dict[str, str]] = field(default_factory=list)
    mode: str = view.FILTER_OFF
    dataset_id: str = ""
    name: str = ""
    composition: Any = None
    applied: bool = False
    unavailable: list[str] = field(default_factory=list)
    note: str = ""
    counted: int = 0


class ApiError(Exception):
    """Ошибка API: браузеру уходит код ответа и вид ошибки (`kind`)."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "validation",
        status: int = 400,
        exchange_seq: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.kind = str(kind)
        self.status = int(status)
        self.exchange_seq = exchange_seq


@dataclass
class Raw:
    """Ответ не в JSON: файл интерфейса, CSV-выгрузка или файл отчёта.

    Важное различие: `download=True` — файл отдаётся **вложением**
    (`Content-Disposition: attachment`), так уходит только выгрузка (CSV протокола).
    Статика интерфейса (`index.html`, `app.js`, `styles.css`) отдаётся без этого
    заголовка: иначе браузер скачивает страницу вместо того, чтобы её показать,
    а подресурсы (стили, скрипт) не применяются.

    Тело хранится **байтами**: статика — это файлы, и не всякий из них текст
    (`favicon.ico`, PNG, шрифты), поэтому чтение «как текста» здесь неуместно.
    """

    data: bytes
    content_type: str = "text/plain; charset=utf-8"
    filename: str = ""
    download: bool = False

    @property
    def text(self) -> str:
        """Тело как текст UTF-8: удобно для CSV, HTML и проверок в тестах."""
        return self.data.decode("utf-8")

    @classmethod
    def of_text(
        cls,
        text: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
        filename: str = "",
        download: bool = False,
    ) -> Raw:
        """Текстовый ответ: кодирование в UTF-8 собрано в одном месте."""
        return cls(text.encode("utf-8"), content_type, filename, download)


@dataclass
class PultApp:
    """Маршрутизация и операции пульта: один экземпляр на процесс сервера."""

    state: PultState = field(default_factory=PultState)
    web_dir: Path | None = None
    run_lock: threading.Lock = field(default_factory=threading.Lock)
    run_cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Выбор данных и модели для проверок без карточки (`ml-params`, решение 2 = а):
    #: живёт в процессе, как и карточки запуска, и уходит в `params` при запуске.
    run_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_run: dict[str, Any] = field(default_factory=dict)
    #: Фоновые прогоны проверок (панель «Задачи», шаг 4 плана M2).
    jobs: jobs_api.JobRunner = field(default_factory=jobs_api.JobRunner)
    #: Монитор задач стенда: наблюдения, опрос, команды FSM-1 (`FR-T11`, `UC-26…UC-28`).
    monitor: Any = None

    def __post_init__(self) -> None:
        if self.web_dir is None:
            self.web_dir = Path(__file__).resolve().parent / "web"
        # фоновые задания фиксируют диапазон журнала: он идёт в доказательства прогона
        self.jobs.set_journal_seq(self.journal_seq)

    # -- задачи стенда -------------------------------------------------------
    def task_monitor(self) -> Any:
        """Монитор задач стенда; None — если адрес стенда не задан (`FR-P-25`)."""
        runtime = self.state.runtime()
        if runtime is None:
            return None
        interval = float(runtime.config.poll_interval or 0) or None
        if self.monitor is None:
            self.monitor = TaskMonitor(
                runtime.apis.tasks, journal=runtime.journal, interval=interval
            )
        return self.monitor

    def journal_seq(self) -> int | None:
        """Номер последней записи журнала обмена (None — записей нет)."""
        runtime = self.state.runtime()
        if runtime is None or not runtime.journal.records:
            return None
        return int(runtime.journal.records[-1].seq)

    # -- маршрутизация -------------------------------------------------------
    def dispatch(
        self, method: str, path: str, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> Any:
        """Обработчик по методу и пути; всё, что не `/api/*`, — статика интерфейса."""
        verb = str(method).upper()
        if path.startswith("/api/"):
            for route_method, pattern, name in COMPILED:
                if route_method != verb:
                    continue
                match = pattern.match(path)
                if match is None:
                    continue
                handler: Callable[..., Any] = getattr(self, name)
                return handler(query=dict(query), payload=dict(payload), **match.groupdict())
            raise ApiError(f"маршрут {verb} {path} не найден", kind="http", status=404)
        if verb != "GET":
            raise ApiError(f"метод {verb} для статики не поддерживается", kind="http", status=405)
        return self.static(path)

    def static(self, path: str) -> Raw:
        """Файл интерфейса из `pult/web`; выйти за пределы каталога нельзя.

        Отдаётся файлом для **отображения** (`download=False`): страница и её
        подресурсы должны открыться в браузере, а не скачаться (`IR-P-4`).
        """
        root = Path(self.web_dir or Path.cwd()).resolve()
        relative = unquote(path).lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            raise ApiError("путь за пределы каталога интерфейса", kind="http", status=403)
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            raise ApiError(f"файл {relative} не найден", kind="http", status=404)
        mime = MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        return Raw(candidate.read_bytes(), content_type=mime)

    # -- общие помощники -----------------------------------------------------
    def session(self) -> TestSession:
        """Текущая сессия или ошибка «сессия не выбрана» (`IR-P-8`)."""
        found = self.state.current_session()
        if found is None:
            raise ApiError("Сессия испытаний не выбрана", kind="validation", status=409)
        return found

    def runtime(self) -> Any:
        """Runtime стенда или ошибка «адрес стенда не задан»."""
        found = self.state.runtime()
        if found is None:
            raise ApiError(STAND_NOT_CONFIGURED, kind="validation", status=409)
        return found

    def programme(self, session: TestSession) -> programme_api.Programme:
        """Программа сессии (пустая — если сессия без программы)."""
        return programme_api.load_programme(session)

    def queue(
        self, session: TestSession, *, run_mode: str = "", queue_mode: str = ""
    ) -> queue_api.Queue:
        """Очередь прогона по ревизии сессии с восстановлением состояния (схема v8).

        Args:
            run_mode: режим прогона («only-remaining» / «all»); пусто — из сессии
                (`session.skip_done`, решение 7 ТЗ).
            queue_mode: режим исполнения («сценарием» / «по вызовам», задел M1/M2).
        """
        programme = self.programme(session)
        return run_state.run_queue(
            session,
            programme,
            self.state.journal_records(),
            mode=queue_mode or None,
            run_mode=run_mode or None,
        )

    def stand_snapshot(self) -> dict[str, Any]:
        """Снимок реестров стенда для сессии (`DR-P-8`)."""
        return dict(api.take_stand_snapshot(self.runtime().apis))

    def save(self, session: TestSession) -> TestSession:
        """Сохраняет сессию на диск: действие оператора переживает перезапуск пульта."""
        return self.state.store_session(session)

    def confirm_requests(self, session: TestSession) -> list[str]:
        """Закрывает проверки, подтверждённые запросами этой сессии (`UC-03`/`UC-04`).

        Вызывается после чтения реестров и после повторного вызова: если проверка
        уже подтверждена успешным обменом, она получает результат с пометкой
        «выполнено по факту запроса» (`run_state.CONFIRMED_NOTE`).
        """
        programme = self.programme(session)
        if not programme.size:
            return []
        written = run_state.confirm_by_request(
            session,
            programme,
            self.state.journal_records(),
            author=session.info.operator_fio,
        )
        if written:
            self.save(session)
        return written

    def exchange_seq(self) -> int | None:
        """Номер последней записи журнала: с ним ошибка связывается с обменом (`FR-P-22`)."""
        records = self.state.journal_records()
        if not records:
            return None
        last = records[-1]
        number = getattr(last, "seq", None)
        return int(number) if isinstance(number, int) else None

    def fail(self, exc: Exception, *, kind: str = "client") -> ApiError:
        """Превращает ошибку клиента или сети в ответ API с номером обмена."""
        message = str(exc).strip() or exc.__class__.__name__
        return ApiError(message, kind=kind, status=502, exchange_seq=self.exchange_seq())

    # -- шапка и сессии ------------------------------------------------------
    def api_state(self, *, query: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        """`GET /api/state` — шапка: стенд, сборка, сессия, программа, фаза, «что дальше»."""
        runtime = self.state.runtime()
        session = self.state.current_session()
        programme = self.programme(session) if session is not None else programme_api.Programme()
        records = self.state.journal_records()
        queue = (
            run_state.run_queue(session, programme, records)
            if session is not None
            else queue_api.queue_for_revision(programme)
        )
        return view.header_view(
            stand_configured=runtime is not None,
            base_url="" if runtime is None else runtime.settings.base_url,
            session=session,
            programme=programme,
            queue=queue,
            results=results_api.state(session) if session is not None else {},
            stand_status=self.state.stand_status(),
            log_level=self.state.log_level(),
        )

    def api_sessions(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/sessions` — сессии на диске и выбранная сейчас (`FR-P-4`)."""
        current = self.state.session_id()
        return {
            "current": current or "",
            "sessions": view.sessions_view(self.state.sessions(), current),
        }

    def api_session_create(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/sessions` — создать черновик сессии (`FR-P-3`) или выбрать её (`FR-P-4`)."""
        action = str(payload.get("action") or "create").strip()
        if action == "select":
            return self._select_session(str(payload.get("session_id") or ""))
        if action != "create":
            raise ApiError(f"неизвестное действие с сессиями: {action}", status=422)
        info = session_api.SessionInfo.from_dict(dict(payload.get("info") or {}))
        if not str(info.title).strip() or not str(info.object_of_test).strip():
            raise ApiError("Укажите наименование испытаний и объект испытаний", status=422)
        runtime = self.state.runtime()
        session = session_api.new_session(
            base_url="" if runtime is None else runtime.settings.base_url
        )
        artifacts = self.state.ensure_logging(session.session_id)
        session.logs = {
            "app_log": artifacts.app_log.as_posix(),
            "session_log": artifacts.session_log.as_posix(),
        }
        if runtime is not None:
            try:
                session.server_version = dict(runtime.apis.system.version() or {})
            except (ClientError, httpx.HTTPError) as exc:
                LOG.warning("версия стенда не получена: %s", exc)
        session.info = info
        self.save(session)
        self.state.set_session(session.session_id)
        return view.session_form_view(session)

    def api_session_start(
        self, session_id: str, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/sessions/{id}/start` — сверка со стендом, снимок «Начало», старт."""
        session = self._selected(session_id)
        snapshot = self.stand_snapshot()
        session_api.set_snapshot(session, session_api.SNAP_START, snapshot)
        self.state.set_stand_status(snapshot)
        session_api.start_session(session)
        self.save(session)
        return {
            "session": view.session_form_view(session),
            "snapshots": view.snapshot_view(session),
            "stand": {"build": view.build_label(snapshot.get("version"))},
            "problem": view.error_text(snapshot),
        }

    def api_session_snapshot(
        self, session_id: str, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/sessions/{id}/snapshot` — переснять снимок «Начало»/«Окончание»."""
        session = self._selected(session_id)
        phase = str(payload.get("phase") or session_api.SNAP_START).strip()
        if phase not in view.SNAPSHOT_LABELS:
            raise ApiError(f"неизвестная фаза снимка: {phase}", status=422)
        snapshot = self.stand_snapshot()
        session_api.set_snapshot(session, phase, snapshot)
        self.state.set_stand_status(snapshot)
        self.save(session)
        return {
            "phase": phase,
            "snapshots": view.snapshot_view(session),
            "problem": view.error_text(snapshot),
        }

    def api_session_finish(
        self, session_id: str, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/sessions/{id}/finish` — снимок «Окончание», готовность, завершение."""
        session = self._selected(session_id)
        snapshot = self.stand_snapshot()
        session_api.set_snapshot(session, session_api.SNAP_END, snapshot)
        self.state.set_stand_status(snapshot)
        self.save(session)
        prepared = view.report_view(
            report_api.readiness(session), snapshots=view.snapshot_view(session)
        )
        conclusion = str(payload.get("conclusion") or session.info.conclusion or "").strip()
        closed = bool(payload.get("close", True))
        if closed and not conclusion:
            raise ApiError(
                "Укажите итоговое решение сессии (годен / годен с замечаниями / …): "
                "без него отчёт неполон",
                status=422,
            )
        if closed:
            session_api.close_session(session, conclusion=conclusion or None)
        self.save(session)
        return {
            "session": view.session_form_view(session),
            "snapshots": view.snapshot_view(session),
            "readiness": prepared,
            "closed": closed,
        }

    def _select_session(self, session_id: str) -> dict[str, Any]:
        """Делает сессию текущей и отдаёт её реквизиты со списком сессий (`FR-P-4`)."""
        found = self._selected(session_id)
        return {
            "session": view.session_form_view(found),
            "sessions": self.api_sessions(query={}, payload={}),
        }

    def _selected(self, session_id: str) -> TestSession:
        """Сессия по идентификатору из пути и она же становится текущей."""
        try:
            found = session_api.load_session(session_id)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ApiError(f"сессию {session_id} прочитать не удалось: {exc}", status=404) from exc
        self.state.set_session(found.session_id)
        return found

    # -- планирование: каталог, наборы, программа ----------------------------
    def api_catalog(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/catalog` — каталог проверок со статусом сессии и фильтрами (`SCR-101`)."""
        session = self.state.current_session()
        programme = self.programme(session) if session is not None else programme_api.Programme()
        library = self.state.sets_library()
        rows = view.catalog_rows(
            results=results_api.state(session) if session is not None else {},
            in_set=self._set_scope(library, session),
            in_programme=programme.check_ids,
            only_class=str(query.get("class") or ""),
            only_section=str(query.get("section") or ""),
        )
        return {
            "summary": catalog.catalog_summary(),
            "sections": [{"key": group.key, "title": group.title} for group in catalog.groups()],
            "classes": view.class_options(),
            "statuses": list(view.status_labels()),
            "total": len(rows),
            "rows": view.filter_rows(rows, query=str(query.get("q") or "")),
        }

    def _set_scope(self, library: Any, session: TestSession | None) -> set[str]:
        """Состав наборов, выбранных для программы сессии (отметка «в наборе», `SCR-101`)."""
        if session is None:
            return set()
        scope: set[str] = set()
        for set_id in self.programme(session).selected_set_ids():
            found = library.find(set_id)
            if found is not None:
                scope.update(str(item.check_id) for item in found.items)
        return scope

    def api_sets(self, *, query: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        """`GET /api/sets` — библиотека наборов: состав, ревизия, статус, участие в программе."""
        library = self.state.sets_library(refresh=bool(query.get("refresh")))
        session = self.state.current_session()
        programme = self.programme(session) if session is not None else None
        rows = view.sets_rows(library, programme=programme)
        return {
            "summary": library.summary(),
            "sections": library.section_rows(),
            "rows": view.filter_rows(
                rows,
                section=str(query.get("section") or ""),
                query=str(query.get("q") or ""),
            ),
        }

    def api_sets_action(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/sets` — действия с библиотекой наборов (`FR-P-65`…`FR-P-67`).

        Кроме `save|approve|delete|import` поддерживаются правки состава и ревизии:
        `remove|reorder|move|mandatory|revise|restore`. Правка утверждённого набора
        запрещена правилом `FR-P-66`: сервер отвечает `409` и подсказывает выпустить
        новую ревизию, а не меняет документ молча.
        """
        action = str(payload.get("action") or "").strip()
        library = self.state.sets_library(refresh=True)
        author = str(payload.get("author") or self._author()).strip()
        comment = str(payload.get("comment") or "")
        try:
            if action == "save":
                self._save_set(library, payload, author=author)
            elif action == "approve":
                self._set_of(library, payload).approve(author=author, comment=comment)
            elif action == "delete":
                removed = library.remove(
                    str(payload.get("set_id") or ""), force=bool(payload.get("force"))
                )
                if not removed:
                    raise ApiError("набор не найден в библиотеке", status=404)
            elif action == "import":
                imported = sets_api.SetsLibrary.from_dict(dict(payload.get("library") or {}))
                for item in imported.sets:
                    library.upsert(item)
            elif action == "remove":
                self._set_of(library, payload).remove(self._check_list(payload))
            elif action == "reorder":
                self._set_of(library, payload).reorder(self._check_list(payload))
            elif action == "move":
                self._set_of(library, payload).move(
                    str(payload.get("check_id") or ""), self._delta(payload)
                )
            elif action == "mandatory":
                self._set_of(library, payload).set_mandatory(
                    str(payload.get("check_id") or ""), bool(payload.get("mandatory", True))
                )
            elif action == "revise":
                self._set_of(library, payload).revise(author=author, comment=comment)
            elif action == "restore":
                self._set_of(library, payload).restore_revision(
                    self._revision(payload), author=author, comment=comment
                )
            else:
                raise ApiError(f"неизвестное действие с наборами: {action}", status=422)
        except ValueError as exc:
            raise ApiError(str(exc), status=409) from exc
        path = self.state.save_sets_library(library)
        return {
            "saved": action,
            "path": path.as_posix(),
            "library": self.api_sets(query={}, payload={}),
        }

    def _set_of(self, library: Any, payload: Mapping[str, Any]) -> Any:
        """Набор из запроса или ошибка `404` (действия библиотеки адресуются `set_id`)."""
        item = library.find(str(payload.get("set_id") or ""))
        if item is None:
            raise ApiError("набор не найден в библиотеке", status=404)
        return item

    @staticmethod
    def _check_list(payload: Mapping[str, Any], key: str = "check_ids") -> list[str]:
        """Список идентификаторов проверок из запроса: строка — один элемент, иначе пусто."""
        value = payload.get(key)
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item).strip()]
        return []

    @staticmethod
    def _delta(payload: Mapping[str, Any]) -> int:
        """Сдвиг порядка из запроса: пустое или нечисловое значение — 0 (сдвига нет)."""
        try:
            return int(payload.get("delta") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _revision(payload: Mapping[str, Any]) -> int:
        """Номер ревизии из запроса: нечисловое значение — 0 («ревизия не найдена»)."""
        try:
            return int(payload.get("revision") or 0)
        except (TypeError, ValueError):
            return 0

    def _save_set(self, library: Any, payload: Mapping[str, Any], *, author: str) -> Any:
        """Создаёт набор или дополняет существующий составом из запроса (`FR-P-65`)."""
        check_ids = self._check_list(payload)
        set_id = str(payload.get("set_id") or "").strip()
        found = library.find(set_id) if set_id else None
        if found is not None:
            found.add(check_ids, mandatory=payload.get("mandatory"), section=found.section)
            return found
        if not check_ids:
            raise ApiError("набор без проверок сохранить нельзя", status=422)
        return library.create(
            title=str(payload.get("title") or set_id or "новый набор"),
            section=str(payload.get("section") or ""),
            check_ids=check_ids,
            author=author,
            note=str(payload.get("note") or ""),
            mandatory=bool(payload.get("mandatory")),
        )

    def api_programme(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/programme` — состав, покрытие, предупреждения, ревизии, дельта (`SCR-102`)."""
        session = self.state.current_session()
        sets = self.api_sets(query={}, payload={})["rows"]
        if session is None:
            return {
                "session": "",
                "programme": view.programme_view(programme_api.Programme(), {}),
                "sets": sets,
            }
        return {
            "session": session.session_id,
            "programme": view.programme_view(self.programme(session), results_api.state(session)),
            "sets": sets,
        }

    def api_programme_action(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/programme` — `rebuild|approve|revise|restore|reorder|move|mandatory`.

        Правки порядка и обязательности (`reorder|move|mandatory`) доступны только в
        черновике: утверждённая ревизия — документ испытаний, её меняют новой ревизией
        (`FR-P-13`), поэтому сервер отвечает `409` с подсказкой, а не правит молча.
        """
        action = str(payload.get("action") or "").strip()
        session = self.session()
        library = self.state.sets_library()
        programme = self.programme(session)
        author = str(payload.get("author") or self._author()).strip()
        comment = str(payload.get("comment") or "").strip()
        try:
            if action == "rebuild":
                set_ids = [
                    str(item) for item in (payload.get("set_ids") or programme.selected_set_ids())
                ]
                if not set_ids:
                    raise ApiError(
                        "не выбран ни один набор: программу не из чего собрать", status=422
                    )
                result = programme.rebuild(library, set_ids)
                self.state.store_programme(
                    programme,
                    event="programme_rebuilt",
                    message=str(result.get("summary") or "программа пересобрана"),
                )
            elif action == "approve":
                revision = programme.approve(
                    by=author, comment=comment, reduction=payload.get("reduction")
                )
                # прогон идёт по утверждённой ревизии: номер фиксируется в сессии (`FR-P-13`)
                session.run_revision = int(revision.revision or programme.revision)
                self.state.store_programme(
                    programme,
                    event="programme_approved",
                    message=f"ревизия {programme.revision} утверждена",
                )
            elif action == "revise":
                programme.revise(author=author, comment=comment)
                self.state.store_programme(programme, event="programme_revised", message=comment)
            elif action == "restore":
                number = self._revision(payload)
                programme.restore_revision(number, author=author, comment=comment)
                self.state.store_programme(
                    programme, event="programme_restored", message=f"ревизия {number}"
                )
            elif action == "reorder":
                programme.reorder(self._check_list(payload))
                self.state.store_programme(
                    programme, event="programme_reordered", message="порядок пунктов задан вручную"
                )
            elif action == "move":
                if not programme.move(str(payload.get("check_id") or ""), self._delta(payload)):
                    raise ApiError(
                        "пункт не сдвинут: он на границе порядка (обязательные идут первыми)",
                        status=409,
                    )
                self.state.store_programme(
                    programme, event="programme_reordered", message="пункт сдвинут вручную"
                )
            elif action == "mandatory":
                if not programme.set_mandatory(
                    str(payload.get("check_id") or ""), bool(payload.get("mandatory", True))
                ):
                    raise ApiError("пункта нет в программе", status=404)
                self.state.store_programme(
                    programme, event="programme_mandatory", message="признак «обязательная» изменён"
                )
            else:
                raise ApiError(f"неизвестное действие с программой: {action}", status=422)
        except ValueError as exc:
            raise ApiError(str(exc), status=409) from exc
        return self.api_programme(query={}, payload={})

    def _author(self) -> str:
        """Автор действия: оператор текущей сессии (пусто — если сессии нет)."""
        session = self.state.current_session()
        return "" if session is None else str(session.info.operator_fio or "")

    def _mark_values(self, payload: Mapping[str, Any], *, check_id: str) -> dict[str, Any]:
        """Поля ручной отметки: статус, заключение и вердикт (`FR-P-32`).

        Заключение обязательно: «выполнена вручную» — это суждение о соответствии
        требованиям, и без текста его нельзя проверить ни в протоколе, ни в отчёте
        (`FR-P-32`, `AC-P-13`). Статус ограничен списком отметок: «успех» и «отказ»
        ставятся либо прогоном, либо явной отметкой оператора с заключением.
        """
        note = str(payload.get("note") or "").strip()
        if not note:
            raise ApiError("Отметка не принята: нужно заключение оператора (`FR-P-32`)", status=422)
        status = str(payload.get("status") or "").strip() or view.MARK_STATUSES[0]
        if status not in {str(value) for value in view.MARK_STATUSES}:
            raise ApiError(f"статус отметки «{status}» не поддерживается", status=422)
        verdict = str(payload.get("verdict") or "").strip() or f"отметка оператора: {status}"
        return {"check_id": check_id, "status": status, "verdict": verdict, "operator_note": note}

    # -- прогон --------------------------------------------------------------
    def api_run(self, *, query: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        """`GET /api/run` — пункты ревизии: маркер, состояние, результат, «что дальше» (`SCR-301`).

        Чтение состояния прогона закрывает проверки, которые уже подтверждены успешными
        запросами этой сессии (`run_state.confirm_by_request`): запись идемпотентна и
        повторяется только для новых подтверждений — иначе пульт заставлял бы оператора
        запускать то, что уже проверено чтением реестров (`UC-03`/`UC-04`).
        """
        session = self.state.current_session()
        if session is None:
            return self._empty_run()
        return self._run_view(session)

    def _ml_values(self, check_id: str, card: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Выбранные данные и модель пункта: карточка запуска главнее панели параметров.

        Проверки `live`/`heavy` хранят выбор в карточке (решение 4 = а), проверки без
        карточки — в `run_params` (решение 2 = а). Оба источника нужны для подстановки
        в вызов (`llm_tasks/49`, задача 1б).
        """
        saved = dict(self.run_params.get(check_id) or {})
        evidence = dict(card if card is not None else self.run_cards.get(check_id) or {})
        for key in (*runner_api.ML_PARAMS, "dataset_label", "model_label"):
            value = str(evidence.get(key) or "").strip()
            if value:
                saved[key] = value
        return {str(key): value for key, value in saved.items() if str(value or "").strip()}

    def _ml_required_fields(self, check_id: str) -> list[str]:
        """Поля датасета и модели, обязательные для этой проверки (`llm_tasks/49`, 1б).

        Нужны и карточке запуска (`validate(required_extra=…)`), и отказу при вызове:
        список обязательных полей живёт в одном месте и не расходится между формой
        и API.
        """
        spec = catalog.find(check_id)
        if spec is None:
            return []
        needs = runner_api.ml_params_required(spec)
        return [
            field
            for field, required in (
                (runner_api.ML_DATASET_FIELD, needs["dataset"]),
                (runner_api.ML_MODEL_PARAM, needs["model"]),
            )
            if required
        ]

    def _missing_ml(self, check_id: str, values: Mapping[str, Any]) -> list[str]:
        """Какие обязательные для проверки параметры не выбраны (`llm_tasks/49`, 1б).

        Правило берётся из спецификации (`runner.ml_params_required`), поэтому пульт не
        требует датасет у проверки, которая его не использует, и не молчит там, где
        без датасета вызов ушёл бы с заглушкой `"<uuid>"`.
        """
        spec = catalog.find(check_id)
        if spec is None:
            return []
        needs = runner_api.ml_params_required(spec)
        missing: list[str] = []
        if needs["dataset"] and not str(values.get(runner_api.ML_DATASET_FIELD) or "").strip():
            missing.append(run_card.FIELD_LABELS[runner_api.ML_DATASET_FIELD])
        if needs["model"] and not str(values.get(runner_api.ML_MODEL_PARAM) or "").strip():
            missing.append(run_card.FIELD_LABELS[runner_api.ML_MODEL_PARAM])
        return missing

    def _composition_of(
        self, session: TestSession | None, dataset: Any, files: Sequence[Any] | None = None
    ) -> Any:
        """Состав датасета из кэша: нужен для подписи «датасет «X» (104 файла)» и панели.

        Читается только для того датасета, который оператор выбрал для проверки: на
        отрисовку экрана прогона нельзя читать составы всех датасетов — за каждым стоит
        обращение к `$Задачам` стенда (`llm_tasks/49`, задача 1б).
        """
        runtime = self.state.runtime()
        dataset_id = str(getattr(dataset, "id", "") or "")
        if session is None or runtime is None or not dataset_id:
            return None

        def load() -> Any:
            return composition_api.read_composition(
                session,
                runtime.apis,
                dataset_id,
                name=str(getattr(dataset, "name", "") or ""),
                dataset_type=str(getattr(dataset, "type", "") or ""),
                files=files,
            )

        return self.state.composition_cache(load, key=f"composition|{dataset_id}")

    def _empty_run(self) -> dict[str, Any]:
        """Вид прогона, когда сессия не выбрана: экран показывает «нет сессии» (`IR-P-8`)."""
        return {
            "session": {"id": "", "status": ""},
            "revision": 0,
            "mode": "",
            "mode_label": "",
            "pause": 0.0,
            "skip_done": True,
            "progress": {"total": 0, "done": 0, "pending": 0, "next": "", "stop_reason": ""},
            "next": {
                "check_id": "",
                "label": "",
                "runnable": False,
                "reason": "Сессия испытаний не выбрана",
                "confirm_required": False,
                "confirm_note": "",
            },
            "card_ready": False,
            "rows": [],
            "written": [],
            "last_run": dict(self.last_run),
        }

    def _run_view(
        self, session: TestSession, *, written: Sequence[str] | None = None, mode: str = ""
    ) -> dict[str, Any]:
        """Готовый вид прогона: строки, прогресс, «▶ Следующая» и требования пункта.

        `mode` — режим прогона («только невыполненное» / «всё подряд», `IR-P-8`); режим
        исполнения очереди («сценарием» / «по вызовам») живёт отдельно (`queue_mode`).
        """
        written_ids = [str(check_id) for check_id in (written or self.confirm_requests(session))]
        programme = self.programme(session)
        queue = self.queue(session, run_mode=mode)
        results = results_api.state(session)
        ready = view.readiness_view(session, programme, queue)
        following = queue.next_item()
        next_id = "" if following is None else following.check_id
        item = programme.find(next_id) if next_id else None
        card = self.run_cards.get(next_id) if next_id else None
        # Выбор данных и модели: карточка (`live`/`heavy`) или панель параметров (решение 2 = а).
        ml_values = self._ml_values(next_id, card) if next_id else {}
        datasets, _datasets_error = self.state.datasets() if item is not None else ([], None)
        models, _models_error = self.state.models() if item is not None else ([], None)
        chosen_dataset = next(
            (
                row
                for row in datasets
                if str(getattr(row, "id", "") or "") == ml_values.get("dataset_id")
            ),
            None,
        )
        composition = (
            self._composition_of(session, chosen_dataset) if chosen_dataset is not None else None
        )
        known_compositions = (
            {str(getattr(chosen_dataset, "id", "")): composition}
            if chosen_dataset is not None and composition is not None
            else {}
        )
        ml = view.ml_view(
            item,
            datasets=datasets,
            compositions=known_compositions,
            models=models,
            values=ml_values,
        )
        card_options = {
            str(field["name"]): list(field.get("options") or [])
            for field in ml.get("fields", [])
            if field.get("options")
        }
        extra_required = [
            str(field["name"]) for field in ml.get("fields", []) if field.get("required")
        ]
        modes = view.modes_view(session)
        return {
            "session": {"id": session.session_id, "status": session.status},
            "revision": queue.revision,
            "mode": modes["current"],
            "mode_label": modes["current_label"],
            "modes": modes,
            "queue_mode": queue.mode,
            "queue_mode_label": queue.mode_label,
            "pause": queue.pause_seconds,
            "skip_done": bool(session.skip_done),
            "progress": run_state.progress(queue, results),
            "readiness": ready,
            "next": {
                "check_id": next_id,
                "label": next_label(queue),
                "runnable": bool(ready["runnable"]),
                "reason": str(ready["reason"]),
                "blocked_by": str(ready["blocked_by"]),
                "fix": dict(ready["fix"]),
                "done": next_label(queue),
                # проверка без сценария (группы T6–T9): предупреждение видно до нажатия
                "notice": view.scenario_note(next_id) if next_id else "",
                "item": view.run_next_view(
                    item,
                    values={**(card or {}), **ml.get("values", {})},
                    session=session,
                    author=self._author(),
                    ml=ml,
                    card_options=card_options,
                    extra_required=extra_required,
                    data_hint=str(ml.get("data_hint") or ""),
                ),
                "ml": ml,
            },
            "batch": view.batch_view(queue, programme),
            "card_ready": bool(card),
            "card": view.run_card_view(
                item,
                values={**(card or {}), **ml.get("values", {})},
                session_id=session.session_id,
                author=self._author(),
                options=card_options,
                extra_required=extra_required,
                data_hint=str(ml.get("data_hint") or ""),
            )
            if item is not None
            else {},
            "ml": ml,
            "ml_values": ml_values,
            "rows": view.run_rows(session, programme, queue),
            "written": written_ids,
            "last_run": dict(self.last_run),
        }

    def api_run_action(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/run` — `next|batch-tech|call|mode` (`IR-P-4`, `A4`).

        Замок `run_lock` не даёт двум прогонам идти одновременно: случайный двойной клик
        получает «прогон уже идёт», а не второй вызов проверки на стенд (`NFR-P-5`).

        Перед запуском проверяется **то же** правило готовности, что включает кнопку в
        интерфейсе (`view.readiness_view`): API не запускает пункт, который оператору
        недоступен, — иначе причина отказа и выключенная кнопка разошлись бы.
        """
        action = str(payload.get("action") or "next").strip()
        session = self.session()
        runtime = self.runtime()
        programme = self.programme(session)
        run_mode = str(payload.get("mode") or "").strip()
        queue_mode = str(payload.get("queue_mode") or "").strip()
        if "skip_done" in payload and not run_mode:
            run_mode = (
                run_state.RUN_ONLY_REMAINING
                if bool(payload.get("skip_done"))
                else run_state.RUN_ALL
            )
        queue = self.queue(session, run_mode=run_mode, queue_mode=queue_mode)
        ready = view.readiness_view(session, programme, queue)
        if action == "mode":
            chosen = run_state.set_run_mode(session, run_mode or run_state.RUN_ONLY_REMAINING)
            self.save(session)
            return self._run_view(session, written=[], mode=chosen)
        if action not in ("next", "batch-tech", "call"):
            raise ApiError(f"неизвестное действие прогона: {action}", status=422)
        if not ready["runnable"]:
            raise ApiError(str(ready["reason"]), status=409)
        check_id = str(payload.get("check_id") or "").strip() or None
        following = queue.next_item()
        target = check_id or ("" if following is None else following.check_id)
        card = self.run_cards.get(target) if target else None
        # Параметры запуска: выбранные данные и модель (карточка или панель) + правки оператора.
        # Правило «JS ничего не решает» сохранено: значения собирает сервер (`llm_tasks/49`, 1б).
        params = {
            **self._ml_values(target, card),
            **{str(key): value for key, value in dict(payload.get("params") or {}).items()},
        }
        if action == "call" and target:
            missing = self._missing_ml(target, params)
            if missing:
                raise ApiError(
                    f"{target}: не выбрано — "
                    + ", ".join(missing)
                    + ". Выберите данные и модель на странице «Прогон» "
                    "(для `live`/`heavy` — в карточке запуска, иначе — в параметрах проверки)",
                    status=422,
                )
        author = self._author()
        client = self.state.console_client()

        def work() -> dict[str, Any]:
            """Один прогон: строки результатов и диапазон журнала (идёт в фоне или сразу)."""
            if not self.run_lock.acquire(blocking=False):
                raise ApiError("Прогон уже идёт: дождитесь окончания текущего пункта", status=409)
            journal_from = self.journal_seq()
            try:
                if action == "next":
                    outcomes = [
                        execute_next(
                            queue,
                            programme,
                            session,
                            runtime,
                            check_id=check_id,
                            client=client,
                            params=params,
                            run_evidence=card,
                            author=author,
                        )
                    ]
                elif action == "batch-tech":
                    limit = payload.get("limit")
                    outcomes = run_batch_tech(
                        queue,
                        programme,
                        session,
                        runtime,
                        limit=None if limit in (None, "") else int(limit),
                        client=client,
                        params=params,
                        run_evidence=card,
                        author=author,
                    )
                else:
                    outcomes = [
                        execute_call(
                            queue,
                            programme,
                            session,
                            runtime,
                            check_id=check_id,
                            client=client,
                            params=params,
                            run_evidence=card,
                        )
                    ]
            finally:
                self.run_lock.release()
            self.save(session)
            rows = view.outcome_rows(outcomes)
            self.last_run = {
                "action": action,
                "at": view.short_time(session_api.now_iso(), "%H:%M:%S"),
                "rows": rows,
            }
            return {
                "rows": rows,
                "verdict": jobs_api.verdict_of(rows),
                "journal_from": journal_from,
                "journal_to": self.journal_seq(),
            }

        if _flag(payload.get("background"), False):
            # фоновый прогон: ответ приходит сразу, прогресс — в панели «Задачи»
            try:
                job = self.jobs.start(
                    check_id=target,
                    action=action,
                    work=work,
                    label=self._check_label(target),
                    params=view.mask_params(params),
                )
            except RuntimeError as exc:
                raise ApiError(str(exc), status=409) from exc
            data = self._run_view(session, written=[], mode=run_mode)
            data["job"] = job.as_dict()
            return data

        result = work()
        data = self._run_view(session, written=[], mode=run_mode)
        data["outcomes"] = result["rows"]
        return data

    def _check_label(self, check_id: str) -> str:
        """Подпись пункта для панели «Задачи»: «TC-TR-02: Запуск обучения»."""
        spec = catalog.find(str(check_id or ""))
        if spec is None:
            return str(check_id or "")
        return f"{spec.check_id}: {spec.title}"

    # -- задания, задачи стенда ---------------------------------------------
    def api_jobs(self, *, query: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        """`GET /api/jobs` — фоновые прогоны и наблюдаемые задачи стенда (`FR-T11`)."""
        session = self.state.current_session()
        jobs = self.jobs.jobs()
        active = self.jobs.active()
        return {
            "jobs": [job.as_dict() for job in jobs],
            "jobs_view": view.jobs_view(jobs, active=active.as_dict() if active else None),
            "active": active.as_dict() if active else None,
            "tasks": self._tasks_view(session),
        }

    def api_job(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any], job_id: str
    ) -> dict[str, Any]:
        """`GET /api/jobs/{job_id}` — одно задание: статус, вердикт, диапазон журнала."""
        job = self.jobs.get(str(job_id))
        if job is None:
            raise ApiError(f"задание {job_id} не найдено", kind="http", status=404)
        return {"job": job.as_dict()}

    def _tasks_view(self, session: TestSession | None) -> dict[str, Any]:
        """Данные панели «Задачи»: наблюдения сессии, KPI и список задач стенда.

        Список задач сервер отдаёт **без статуса** (подтверждённый дефект API), поэтому
        статус строки берётся из наблюдений монитора; неизвестный статус помечается
        источником «нет данных» (`FR-T11`, `TC-TASK-02`).
        """
        runtime = self.state.runtime()
        monitor = self.task_monitor()
        data: dict[str, Any] = {
            "configured": bool(runtime is not None and monitor is not None),
            "observed": [],
            "kpi": {},
            "list": [],
            "list_error": "",
            "note": "",
            "commands": [str(name) for name in TASK_COMMANDS],
        }
        if runtime is None or monitor is None:
            data["note"] = STAND_NOT_CONFIGURED
            return data
        if session is None:
            data["note"] = "Сессия испытаний не выбрана: наблюдения недоступны"
        else:
            try:
                monitor.adopt_from_session(session)
                data["observed"] = view.task_rows(monitor, session)
                data["kpi"] = monitor.kpi(session)
            except READ_ERRORS as exc:
                data["note"] = f"наблюдения не прочитаны: {exc}"
        try:
            tasks = monitor.find_recent(limit=TASK_LIMIT)
            data["list"] = view.task_list_rows(monitor, session, tasks)
        except READ_ERRORS as exc:
            data["list_error"] = f"список задач не получен: {exc}"
        return data

    def api_tasks(self, *, query: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        """`GET /api/tasks` — панель «Задачи»: наблюдения, KPI и список задач (`FR-T11`)."""
        return self._tasks_view(self.state.current_session())

    def api_task_action(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any], task_id: str
    ) -> dict[str, Any]:
        """`POST /api/tasks/{task_id}` — наблюдение и команды FSM-1 (`UC-26…UC-28`).

        Действия: `observe` (поставить на наблюдение, в том числе внешнюю задачу),
        `unobserve`, `poll` (внеочередной опрос), `attach` (привязать к проверке) и
        команды FSM-1 (`pause`, `resume`, `interrupt`). Команда, запрещённая текущим
        статусом, не отправляется — оператор видит причину (`BR-R3`, FSM-1).
        """
        action = str(payload.get("action") or "").strip().lower()
        session = self.state.current_session()
        if session is None:
            raise ApiError("Сессия испытаний не выбрана: работа с задачами недоступна", status=409)
        monitor = self.task_monitor()
        if monitor is None:
            raise ApiError(STAND_NOT_CONFIGURED, status=409)
        key = str(task_id).strip()

        if action == "observe":
            record = monitor.register(
                session,
                task_id=key,
                task_type=str(payload.get("task_type") or ""),
                name=str(payload.get("name") or ""),
                check_id=str(payload.get("check_id") or ""),
                note="поставлена на наблюдение оператором",
            )
            self.save(session)
            return {
                "action": action,
                "task_id": key,
                "record": record,
                "tasks": self._tasks_view(session),
            }
        if action == "unobserve":
            removed = monitor.unregister(session, key)
            self.save(session)
            return {
                "action": action,
                "task_id": key,
                "removed": removed,
                "tasks": self._tasks_view(session),
            }
        if action == "poll":
            poll = monitor.poll_once(session, key)
            self.save(session)
            return {
                "action": action,
                "task_id": key,
                "poll": poll.as_dict(),
                "tasks": self._tasks_view(session),
            }
        if action == "attach":
            check_id = str(payload.get("check_id") or "").strip()
            if not check_id:
                raise ApiError("Не указана проверка для привязки задачи", status=422)
            record = monitor.attach_to_check(session, key, check_id)
            self.save(session)
            return {
                "action": action,
                "task_id": key,
                "check_id": check_id,
                "record": record,
                "tasks": self._tasks_view(session),
            }
        if action in TASK_COMMANDS:
            command = monitor.run_command(
                session, key, action, force=_flag(payload.get("force"), False)
            )
            self.save(session)
            return {
                "action": action,
                "task_id": key,
                "command": command.as_dict(),
                "allowed": bool(command.allowed),
                "tasks": self._tasks_view(session),
            }
        raise ApiError(f"неизвестное действие задачи: {action or '—'}", status=422)

    # -- действия по пункту, лента обмена ------------------------------------
    def api_check_action(
        self, check_id: str, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/checks/{id}` — `annotate|mark|suspend|resume|run-card` (`FR-P-15`, `FR-P-32`)."""
        action = str(payload.get("action") or "").strip()
        session = self.session()
        programme = self.programme(session)
        author = self._author()
        item = programme.find(check_id)
        if action == "suspend":
            reason = str(payload.get("reason") or "").strip()
            if not reason:
                raise ApiError("Снятие пункта требует причины (`FR-P-15`)", status=422)
            results_api.suspend(
                session, check_id, reason, author=author, revision=programme.revision
            )
        elif action == "resume":
            if not results_api.resume(session, check_id, author=author):
                raise ApiError(f"пункт {check_id} не снят: возобновлять нечего", status=409)
        elif action == "annotate":
            values = {str(key): value for key, value in dict(payload.get("values") or {}).items()}
            values["check_id"] = check_id
            results_api.annotate(
                session,
                values,
                origin=results_api.ORIGIN_MANUAL,
                author=author,
                revision=programme.revision,
            )
        elif action == "mark":
            if item is None:
                raise ApiError(f"проверка {check_id} не входит в программу сессии", status=404)
            results_api.annotate(
                session,
                self._mark_values(payload, check_id=check_id),
                origin=results_api.ORIGIN_MANUAL,
                author=author,
                revision=programme.revision,
            )
        elif action == "run-card":
            if item is None:
                raise ApiError(f"проверка {check_id} не входит в программу сессии", status=404)
            values = {str(key): value for key, value in dict(payload.get("values") or {}).items()}
            extra = self._ml_required_fields(check_id)
            missing = run_card.validate(values, required_extra=extra)
            if missing:
                raise ApiError(
                    "Карточка запуска заполнена не полностью: " + ", ".join(missing), status=422
                )
            self.run_cards[check_id] = run_card.evidence(values, author=author)
            session.add_history(
                "run_card_filled",
                f"Карточка запуска для {check_id} заполнена",
                check_id=check_id,
            )
        elif action == "ml-params":
            # Проверки без карточки (`TC-INF-02/03/04`, `TC-TR-01`): выбор данных и модели
            # хранится отдельно и уходит в `params` при запуске (решение заказчика 2 = а).
            if item is None:
                raise ApiError(f"проверка {check_id} не входит в программу сессии", status=404)
            values = {
                str(key): str(value)
                for key, value in dict(payload.get("values") or {}).items()
                if str(value or "").strip()
            }
            self.run_params[check_id] = values
            session.add_history(
                "ml_params_set",
                f"Выбраны данные для {check_id}: "
                + (", ".join(f"{key}={value}" for key, value in values.items()) or "нет"),
                check_id=check_id,
            )
        else:
            raise ApiError(f"неизвестное действие по проверке: {action}", status=422)
        self.save(session)
        return {
            "check_id": check_id,
            "action": action,
            "card": dict(self.run_cards.get(check_id) or {}),
            "run": self._run_view(session, written=[]),
        }

    def api_journal(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/journal` — лента обмена: №, метод, путь, статус, тело и `curl` (`FR-P-21`).

        Подписи «почему тела нет» тоже считает сервер: он один знает настройки
        журналирования (`PULT_LOG_BODIES`, лимит тела) и не может ошибиться в отличие
        от браузера, который видит только пустую строку.
        """
        records = self.state.journal_records()
        journal = self.state.journal()
        base_url = self._base_url()
        limit = int(query.get("limit") or 50)
        runtime = self.state.runtime()
        rows = view.journal_rows(
            records,
            base_url=base_url,
            limit=limit,
            errors_only=_flag(query.get("errors_only"), False),
            label=str(query.get("label") or ""),
            detail_request=_flag(query.get("request"), True),
            detail_response=_flag(query.get("response"), True),
            bodies_logged=True if runtime is None else runtime.config.log_bodies,
            body_limit=0 if runtime is None else runtime.config.body_limit,
        )
        return {
            "rows": rows,
            "summary": journal.summary(),
            "levels": ["DEBUG", "INFO", "WARNING", "ERROR"],
            "log_level": self.state.log_level(),
            "base_url": base_url,
            "bodies_logged": True if runtime is None else runtime.config.log_bodies,
            "labels": sorted({str(record.label) for record in records if record.label}),
            "total": len(records),
        }

    def api_journal_repeat(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/journal/repeat` — повтор вызова с правками (`FR-P-24`)."""
        client = self.state.console_client()
        if client is None:
            raise ApiError(STAND_NOT_CONFIGURED, status=409)
        method = str(payload.get("method") or "GET").strip().upper()
        path = str(payload.get("path") or "").strip()
        if not path.startswith("/"):
            raise ApiError("путь вызова должен начинаться с «/»", status=422)
        body = dict(payload.get("json") or {})
        try:
            result = execute_request(
                client,
                method=method,
                path=path,
                query=dict(payload.get("query") or {}) or None,
                json_body=body or None,
                label=str(payload.get("label") or "повтор вызова"),
                journal=self.state.journal(),
            )
        except ClientError as exc:
            raise self.fail(exc) from exc
        except httpx.HTTPError as exc:
            raise self.fail(exc, kind="timeout") from exc
        session = self.state.current_session()
        written = [] if session is None else self.confirm_requests(session)
        last = self.state.journal_records()[-1:]
        rows = view.journal_rows(
            last, base_url=self._base_url(), detail_request=True, detail_response=True
        )
        return {
            "status": int(getattr(result, "status", 0) or 0),
            "row": rows[0] if rows else None,
            "written": written,
        }

    def _base_url(self) -> str:
        """Адрес стенда для команд `curl` (пусто — стенд не настроен)."""
        runtime = self.state.runtime()
        return "" if runtime is None else runtime.settings.base_url

    # -- данные стенда, протокол, отчёт -------------------------------------
    def api_data(self, *, query: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        """`GET /api/data` — файлы, записи RAW+markup, `$Нагрузки`, `$Датасеты`, `$Модели`.

        Составы датасетов читаются только по запросу (`id=…` для одного или
        `compositions=1` для всех): за каждым стоит обращение к `$Задачам` стенда
        (`GET /api/tasks/?task_type=dataset-fill`), а реестр должен открываться быстро.
        Проверки, подтверждённые этим чтением, закрываются по факту запроса (`UC-03`).

        `counts` — сколько сущностей в реестрах стенда, `pages` — сколько строк показано
        из скольких (`limit`, по умолчанию 200; `limit=0` — все). Числа считает сервер:
        интерфейс печатает готовые подписи и не решает, что считать полным списком.
        """
        refresh = _flag(query.get("refresh"), False)
        limit = _row_limit(query.get("limit"))
        files, files_error = self.state.files(refresh=refresh)
        loads, loads_error = self.state.loads(refresh=refresh)
        datasets, datasets_error = self.state.datasets(refresh=refresh)
        models, models_error = self.state.models(refresh=refresh)
        overview = None
        records_error = ""
        if files:
            try:
                overview = records_api.build_records(files)
            except (ValueError, TypeError, AttributeError) as exc:
                records_error = f"объединение в записи не выполнено: {exc}"
                LOG.warning(records_error)
        # `dataset=<id>|none` — фильтр «Данных стенда» по составу датасета (или «вне
        # датасетов»); при недоступном составе фильтр не применяется (см. `_filter_state`).
        session = self.state.current_session()
        written = [] if session is None else self.confirm_requests(session)
        state = self._filter_state(session, datasets, files, query)
        file_rows, file_page = view.page_rows(
            view.file_rows(files, selection=state.selection), limit=limit
        )
        record_rows, record_page = view.page_rows(
            [] if overview is None else view.record_rows(overview, selection=state.selection),
            limit=limit,
        )
        # Состав выбранного в фильтре датасета уже прочитан — отдаём его и в таблицу
        # `$Датасеты`, чтобы панель состава открывалась без второго обращения к стенду.
        compositions = self._compositions(session, datasets, files, query)
        if state.dataset_id and state.composition is not None:
            compositions.setdefault(state.dataset_id, state.composition)
        data: dict[str, Any] = {
            "files": file_rows,
            "records": record_rows,
            "records_stats": {} if overview is None else overview.stats.to_dict(),
            "records_attention": 0 if overview is None else len(overview.requires_attention),
            "loads": view.load_rows(loads),
            "datasets": view.dataset_rows(datasets, compositions=compositions),
            "models": view.model_rows(models),
            "limit": int(limit),
            "counts": {
                "files": len(files),
                "records": 0 if overview is None else len(overview.manifest_rows()),
                "loads": len(loads),
                "datasets": len(datasets),
                "models": len(models),
            },
            "pages": {"files": file_page, "records": record_page},
            "errors": {
                "files": files_error or "",
                "records": records_error,
                "loads": loads_error or "",
                "datasets": datasets_error or "",
                "models": models_error or "",
            },
            "written": written,
        }
        data["filter"] = view.dataset_filter_view(
            options=state.options,
            mode=state.mode,
            dataset_id=state.dataset_id,
            name=state.name,
            composition=state.composition,
            applied=state.applied,
            unavailable=state.unavailable,
            note=state.note,
            counted=state.counted,
            registry_files=len(files),
            shown_files=len(file_rows),
            shown_records=len(record_rows),
        )
        data["filter_label"] = data["filter"]["label"]
        data["sections"] = view.data_sections(data)
        return data

    def _filter_state(
        self,
        session: TestSession | None,
        datasets: Sequence[Any],
        files: Sequence[Any],
        query: Mapping[str, str],
    ) -> DataFilterState:
        """Отбор строк по датасету и факты для блока `filter` (`llm_tasks/49`, задача 1а).

        Решения по честности:

        * `dataset=<id>` — состав читается **только** для выбранного датасета, источник
          можно задать принудительно (`source=server|server_task|local|heuristic`);
        * `dataset=none` — «файлы вне датасетов»: нужны составы всех датасетов, они
          берутся из 60-секундного кэша (`PultState.composition_cache`);
        * состав недоступен или пуст → фильтр **не применяется** и об этом сказано
          прямо: показать пустой список вместо реестра было бы обманом (`FR-P-25`).
        """
        runtime = self.state.runtime()
        options = view.dataset_filter_options(datasets, registry_files=len(files))
        state = DataFilterState(options=options)
        wanted = str(query.get("dataset") or "").strip()
        if not wanted or session is None or runtime is None:
            return state
        mode = str(query.get("source") or "").strip() or composition_api.MODE_AUTO
        if wanted == view.OUTSIDE_VALUE:
            return self._outside_filter(state, session, datasets, files, mode)
        found = next(
            (item for item in datasets if str(getattr(item, "id", "") or "") == wanted), None
        )
        if found is None:
            raise ApiError(f"датасет {wanted} не найден в реестре стенда", status=404)
        composition = composition_api.read_composition(
            session,
            runtime.apis,
            wanted,
            mode=mode,
            name=str(getattr(found, "name", "") or ""),
            dataset_type=str(getattr(found, "type", "") or ""),
            files=files,
        )
        applied = composition.files_count > 0
        state.mode = view.FILTER_DATASET
        state.dataset_id = wanted
        state.name = str(getattr(found, "name", "") or "")
        state.composition = composition
        state.applied = applied
        # Подпись варианта обновляется уже прочитанным составом: оператор видит, что
        # фильтр по этому датасету сработал («файлов 3 · RAW 2 / markup 1 · достоверно»).
        state.options = view.dataset_filter_options(
            datasets, {wanted: composition}, registry_files=len(files)
        )
        state.selection = view.RowSelection(
            keys=composition_api.FileKeys(
                ids=frozenset((*composition.raw_ids, *composition.markup_ids)),
                names=frozenset(
                    str(item.file_name).strip().lower()
                    for item in composition.files
                    if item.file_name
                ),
            ),
            applied=applied,
        )
        if not applied:
            state.note = str(composition.note or "") or view.UNKNOWN_COMPOSITION_NOTE
        return state

    def _outside_filter(
        self,
        state: DataFilterState,
        session: TestSession,
        datasets: Sequence[Any],
        files: Sequence[Any],
        mode: str,
    ) -> DataFilterState:
        """«Файлы вне датасетов»: составы всех датасетов из кэша (решение 3 = а)."""

        def load_all() -> dict[str, Any]:
            found: dict[str, Any] = {}
            runtime = self.state.runtime()
            if runtime is None:
                return found
            for item in datasets:
                dataset_id = str(getattr(item, "id", "") or "")
                if not dataset_id:
                    continue
                found[dataset_id] = composition_api.read_composition(
                    session,
                    runtime.apis,
                    dataset_id,
                    mode=mode,
                    name=str(getattr(item, "name", "") or ""),
                    dataset_type=str(getattr(item, "type", "") or ""),
                    files=files,
                )
            return found

        compositions = dict(self.state.composition_cache(load_all))
        keys = composition_api.file_keys(list(compositions.values()))
        state.mode = view.FILTER_OUTSIDE
        state.applied = not keys.is_empty
        state.counted = len(compositions)
        state.options = view.dataset_filter_options(
            datasets, compositions, registry_files=len(files)
        )
        state.unavailable = [
            str(composition.name or dataset_id)
            for dataset_id, composition in compositions.items()
            if int(composition.files_count) == 0
        ]
        state.selection = view.RowSelection(keys=keys, outside=True, applied=state.applied)
        if state.unavailable:
            state.note = "состав не прочитан у " + ", ".join(state.unavailable)
        return state

    def _compositions(
        self,
        session: TestSession | None,
        datasets: Sequence[Any],
        files: Sequence[Any],
        query: Mapping[str, str],
    ) -> dict[str, Any]:
        """Составы датасетов: один (`id`) или все (`compositions=1`); иначе — не читаем."""
        runtime = self.state.runtime()
        wanted = str(query.get("id") or "").strip()
        if (
            session is None
            or runtime is None
            or not (wanted or _flag(query.get("compositions"), False))
        ):
            return {}
        found: dict[str, Any] = {}
        for item in datasets:
            dataset_id = str(getattr(item, "id", "") or "")
            if wanted and dataset_id != wanted:
                continue
            found[dataset_id] = composition_api.read_composition(
                session,
                runtime.apis,
                dataset_id,
                name=str(getattr(item, "name", "") or ""),
                dataset_type=str(getattr(item, "type", "") or ""),
                files=files,
            )
        return found

    def api_protocol(self, *, query: Mapping[str, str], payload: Mapping[str, Any]) -> Any:
        """`GET /api/protocol` — строки протокола и KPI; `?format=csv` — выгрузка (`SCR-401`)."""
        session = self.session()
        programme = self.programme(session)
        rows = view.protocol_rows(session, programme)
        filtered = view.filter_rows(
            rows,
            status=str(query.get("status") or ""),
            section=str(query.get("section") or ""),
            query=str(query.get("q") or ""),
        )
        if str(query.get("format") or "").lower() == "csv":
            return Raw.of_text(
                view.protocol_csv(filtered),
                content_type="text/csv; charset=utf-8",
                filename="protocol.csv",
                download=True,
            )
        return {
            "session": {"id": session.session_id, "status": session.status},
            "summary": view.protocol_summary(session, programme),
            "statuses": view.status_options(),
            "sections": view.option_rows(
                sorted({str(row["section"]) for row in rows if row.get("section")})
            ),
            "status": str(query.get("status") or ""),
            "section": str(query.get("section") or ""),
            "query": str(query.get("q") or ""),
            "total": len(rows),
            "rows": filtered,
        }

    def api_results(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/results` — страница «Результаты» одним заходом (`UC-03`, `UC-19`, `SCR-401`).

        Три зоны сразу: данные стенда (файлы, записи RAW+markup, `$Нагрузки`, `$Датасеты`,
        `$Модели`), протокол проверок с KPI и готовность отчёта со снимками
        «Начало»/«Окончание». Фильтры протокола — те же, что у `GET /api/protocol`.
        Один запрос на страницу: интерфейс не собирает её из четырёх вызовов (`NFR-P-5`).
        """
        session = self.session()
        programme = self.programme(session)
        data = self.api_data(query=query, payload=payload)
        rows = view.protocol_rows(session, programme)
        snapshots = view.snapshot_view(session)
        return {
            "session": view.session_form_view(session),
            "data": data,
            "data_summary": view.data_summary(data),
            "protocol": {
                "summary": view.protocol_summary(session, programme),
                "statuses": view.status_options(),
                "sections": view.option_rows(
                    sorted({str(row["section"]) for row in rows if row.get("section")})
                ),
                "status": str(query.get("status") or ""),
                "section": str(query.get("section") or ""),
                "query": str(query.get("q") or ""),
                "total": len(rows),
                "rows": view.filter_rows(
                    rows,
                    status=str(query.get("status") or ""),
                    section=str(query.get("section") or ""),
                    query=str(query.get("q") or ""),
                ),
            },
            "report": view.report_view(report_api.readiness(session), snapshots=snapshots),
            "snapshots": snapshots,
        }

    def api_report_readiness(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/report/readiness` — полнота сессии и «чего не хватает» (`FR-P-48`)."""
        session = self.session()
        return view.report_view(
            report_api.readiness(session), snapshots=view.snapshot_view(session)
        )

    def api_report_build(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/report/build` — комплект отчёта без обращений к стенду (`FR-P-50`)."""
        session = self.session()
        shots = view.snapshot_view(session)
        if not bool(payload.get("build", True)):
            return {"readiness": view.report_view(report_api.readiness(session), snapshots=shots)}
        try:
            paths = report_api.save_report(session, journal_records=self.state.journal_records())
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ApiError(f"комплект отчёта собрать не удалось: {exc}", status=500) from exc
        annexes, missing = report_api.manifest_annexes(session)
        return {
            "paths": {str(key): path.as_posix() for key, path in paths.items()},
            "annexes": sorted(str(key) for key in annexes),
            "missing": [str(item) for item in missing],
            "readiness": view.report_view(report_api.readiness(session), snapshots=shots),
        }


def _row_limit(value: Any, default: int = view.DEFAULT_ROW_LIMIT) -> int:
    """Лимит строк подразделов «Данных стенда»: пусто — по умолчанию, «0»/«все» — все.

    Ошибка ввода не роняет страницу: 422 с понятным текстом, а не обрезанный список
    «как получится» (`FR-P-25`).
    """
    text = str(value or "").strip().lower()
    if not text:
        return int(default)
    if text in ("0", "all", "все"):
        return 0
    try:
        return max(0, int(text))
    except (TypeError, ValueError) as exc:
        raise ApiError(f"лимит строк должен быть числом: {value!r}", status=422) from exc


def _flag(value: Any, default: bool) -> bool:
    """Флаг из строки запроса: пусто — значение по умолчанию, «0/нет» — False."""
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "нет", "no", "off"}


class PultHandler(BaseHTTPRequestHandler):
    """Обработчик HTTP: конверт ответа, статика и разбор ошибок в одном месте."""

    server: PultHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        """GET: `/api/...` или файл интерфейса."""
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        """POST: действие оператора с JSON-телом."""
        self._serve("POST")

    def _serve(self, method: str) -> None:
        """Разбирает запрос, вызывает приложение и отправляет ответ."""
        parts = urlsplit(self.path)
        try:
            payload = self._body() if method == "POST" else {}
            query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
            result = self.server.app.dispatch(method, parts.path, query, payload)
        except ApiError as exc:
            self._error(exc)
            return
        except Exception as exc:  # защитный контур: интерфейс не падает молча
            LOG.exception("необработанная ошибка API %s %s", method, parts.path)
            self._error(ApiError(str(exc) or exc.__class__.__name__, kind="server", status=500))
            return
        self._ok(result)

    def _body(self) -> dict[str, Any]:
        """Тело POST: JSON-объект (пустое тело — пустой словарь)."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("тело запроса слишком большое", kind="http", status=413)
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(
                f"тело запроса не разобрать как JSON: {exc}", kind="http", status=400
            ) from exc
        if not isinstance(data, dict):
            raise ApiError("тело запроса должно быть JSON-объектом", kind="http", status=400)
        return {str(key): value for key, value in data.items()}

    def _ok(self, result: Any) -> None:
        """Ответ: `Raw` отдаётся как есть, остальное — в конверте `{ok, data}`.

        `Content-Disposition: attachment` ставится **только** для скачиваемых файлов
        (`Raw.download`): иначе браузер сохраняет страницу интерфейса вместо показа.
        """
        if isinstance(result, Raw):
            self.send_response(200)
            self.send_header("Content-Type", result.content_type)
            self.send_header("Content-Length", str(len(result.data)))
            if result.download and result.filename:
                self.send_header("Content-Disposition", f'attachment; filename="{result.filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(result.data)
            return
        self._json(200, {"ok": True, "data": result})

    def _error(self, exc: ApiError) -> None:
        """Ошибка в конверте `{ok: false, error}` (§4 ТЗ)."""
        payload: dict[str, Any] = {"kind": exc.kind, "message": exc.message}
        if exc.exchange_seq is not None:
            payload["exchange_seq"] = exc.exchange_seq
        self._json(exc.status, {"ok": False, "error": payload})

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        """Отправляет JSON-ответ с длиной тела и запретом кэширования."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Сообщения http.server — в журнал модуля, а не в stderr."""
        LOG.debug("%s - %s", self.address_string(), format % args)


class PultHTTPServer(ThreadingHTTPServer):
    """HTTP-сервер пульта: своё соединение — свой поток, интерфейс живёт параллельно."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: PultApp) -> None:
        self.app = app
        super().__init__(address, PultHandler)


def serve(
    app: PultApp | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> PultHTTPServer:
    """Поднимает сервер (без `serve_forever`) — используется тестами и `main()`."""
    application = app or PultApp()
    server = PultHTTPServer((host, int(port)), application)
    if open_browser:
        webbrowser.open(f"http://{host}:{server.server_port}/")
    return server


#: Таблица маршрутов: метод, шаблон пути, имя обработчика (`{имя}` — часть пути).
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/api/state", "api_state"),
    ("GET", "/api/sessions", "api_sessions"),
    ("POST", "/api/sessions", "api_session_create"),
    ("POST", "/api/sessions/{session_id}/start", "api_session_start"),
    ("POST", "/api/sessions/{session_id}/snapshot", "api_session_snapshot"),
    ("POST", "/api/sessions/{session_id}/finish", "api_session_finish"),
    ("GET", "/api/catalog", "api_catalog"),
    ("GET", "/api/sets", "api_sets"),
    ("POST", "/api/sets", "api_sets_action"),
    ("GET", "/api/programme", "api_programme"),
    ("POST", "/api/programme", "api_programme_action"),
    ("GET", "/api/run", "api_run"),
    ("POST", "/api/run", "api_run_action"),
    ("GET", "/api/jobs", "api_jobs"),
    ("GET", "/api/jobs/{job_id}", "api_job"),
    ("GET", "/api/tasks", "api_tasks"),
    ("POST", "/api/tasks/{task_id}", "api_task_action"),
    ("POST", "/api/checks/{check_id}", "api_check_action"),
    ("GET", "/api/journal", "api_journal"),
    ("POST", "/api/journal/repeat", "api_journal_repeat"),
    ("GET", "/api/protocol", "api_protocol"),
    ("GET", "/api/results", "api_results"),
    ("GET", "/api/data", "api_data"),
    ("GET", "/api/report/readiness", "api_report_readiness"),
    ("POST", "/api/report/build", "api_report_build"),
)

_PART = re.compile(r"(\{[a-zA-Z_]+\})")


def compile_route(pattern: str) -> re.Pattern[str]:
    """`/api/checks/{check_id}` → регулярное выражение с именованной группой."""
    parts: list[str] = []
    for chunk in _PART.split(pattern):
        if chunk.startswith("{") and chunk.endswith("}"):
            parts.append(f"(?P<{chunk[1:-1]}>[^/]+)")
        else:
            parts.append(re.escape(chunk))
    return re.compile("^" + "".join(parts) + "$")


#: Скомпилированные маршруты: (метод, шаблон, имя обработчика).
COMPILED: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (method, compile_route(pattern), name) for method, pattern, name in ROUTES
)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа `python -m pult.server`: узел, порт и запуск сервера.

    Одна команда запуска (`IR-P-4`): оператор не собирает стенд команд — он открывает
    страницу пульта. Порт по умолчанию 8502 — тот, к которому привыкли на Streamlit.
    """
    parser = argparse.ArgumentParser(description="Пульт испытаний: сервер интерфейса")
    parser.add_argument("--host", default=DEFAULT_HOST, help="узел прослушивания (127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт (8502)")
    parser.add_argument("--browser", action="store_true", help="открыть браузер после запуска")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    server = serve(host=str(args.host), port=int(args.port), open_browser=bool(args.browser))
    LOG.info("пульт: http://%s:%s (Ctrl+C — остановить)", args.host, server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("остановка пульта")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
