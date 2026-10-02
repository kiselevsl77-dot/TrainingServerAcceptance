"""Сервер пульта: тонкий JSON-API (30 маршрутов) и раздача статического интерфейса.

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

from acceptance import api, paths, run_card, run_state
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
from acceptance.endpoints import TEST_PREFIX
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
from pult import artifacts as artifacts_api
from pult import jobs as jobs_api
from pult import models as models_api
from pult import uploads as uploads_api
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

    def dispatch_stream(self, path: str, query: Mapping[str, str], stream: Any, length: int) -> Any:
        """Маршрутизация запросов с **файловым** телом (без JSON-обёртки).

        Отдельно от `dispatch`, потому что тело такого запроса нельзя прочитать в
        память: обработчик получает поток и длину и решает, что с ними делать
        (`pult.uploads.save_stream`). Параметры — в query-строке (`llm_tasks/64` §2).
        """
        for pattern, name in COMPILED_STREAM:
            if pattern.match(path) is None:
                continue
            handler: Callable[..., Any] = getattr(self, name)
            return handler(query=dict(query), stream=stream, length=int(length))
        raise ApiError(f"маршрут загрузки файла {path} не найден", kind="http", status=404)

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

    # -- артефакты ресурсоёмких проверок -------------------------------------
    def api_artifacts(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/artifacts` — кандидаты и решения по артефактам (`FR-T10`).

        Кандидаты — объекты **стенда** (`__TEST__`-модели и файлы `.h5`/`.onnx`),
        решения — записи сессии: так оператор видит и то, что оставила его проверка,
        и чужие следы прошлых прогонов. Ошибка чтения реестра не ломает панель: текст
        уходит в `errors`, а не превращается в пустой список — иначе «кандидатов нет»
        и «реестр не прочитан» выглядели бы одинаково.
        """
        return self._artifacts_view(self.state.current_session())

    def _artifacts_view(self, session: TestSession | None) -> dict[str, Any]:
        """Данные панели «Артефакты»: кандидаты стенда, решения сессии, итог."""
        data: dict[str, Any] = {
            "configured": False,
            "rows": [],
            "summary": {},
            "view": {"rows": [], "summary": {}, "note": ""},
            "errors": [],
            "note": "",
        }
        runtime = self.state.runtime()
        if runtime is None:
            data["note"] = STAND_NOT_CONFIGURED
            return data
        session_id = "" if session is None else str(session.session_id or "")
        models: list[Any] = []
        files: list[Any] = []
        try:
            models = list(runtime.apis.models.list_models())
        except READ_ERRORS as exc:
            data["errors"].append(f"реестр моделей не прочитан: {exc}")
        try:
            files = list(runtime.apis.files.list_files().files)
        except READ_ERRORS as exc:
            data["errors"].append(f"реестр файлов не прочитан: {exc}")
        rows = artifacts_api.candidate_rows(models, files, session_id=session_id)
        records = [] if session is None else list(session.artifacts or [])
        data["configured"] = True
        data["rows"] = rows
        data["view"] = view.artifacts_view(rows, records)
        data["summary"] = dict(data["view"]["summary"])
        data["note"] = str(data["view"]["note"])
        if session is None:
            data["note"] = "Сессия испытаний не выбрана: решения по артефактам недоступны"
        return data

    def api_artifacts_action(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/artifacts` — решение по артефакту: `keep` или `delete` (`FR-T10`).

        `keep` фиксирует артефакт испытаний: модель остаётся **на стенде** (убирать
        обученный результат нечем — он стоил ресурсов), а по требованию (`download`)
        её ONNX выгружается в `acceptance_data/artifacts/` как доказательство.
        `delete` убирает объект со стенда (модель — `DELETE /api/ml_models/models/{id}`,
        файл — в корне `/api/{file_id}`) и отмечает `__TEST__`-сущность удалённой,
        чтобы самоочистка `TC-CLEAN-01` видела полную картину.

        Решение в обоих случаях записывается в артефакты сессии (`entity_id`, `action`)
        и попадает в раздел 8 отчёта — по нему оператор закрывает `TC-CLEAN-04`.
        """
        session = self.session()
        runtime = self.runtime()
        decision = artifacts_api.normalize_decision(payload.get("action"))
        if not decision:
            raise ApiError(
                "Действие по артефакту не распознано: ожидается "
                + " или ".join(f"`{name}`" for name in artifacts_api.DECISIONS),
                status=422,
            )
        kind = str(payload.get("kind") or "").strip()
        if kind not in artifacts_api.ENTITY_KINDS:
            raise ApiError(
                "Вид артефакта не распознан: ожидается "
                + " или ".join(f"`{name}`" for name in artifacts_api.ENTITY_KINDS),
                status=422,
            )
        entity_id = str(payload.get("id") or "").strip()
        if not entity_id:
            raise ApiError("Не указан объект: нужен `id` модели или файла стенда", status=422)
        check_id = str(payload.get("check_id") or "").strip()
        note = str(payload.get("note") or "").strip()
        download = _flag(payload.get("download"), False)

        if decision == artifacts_api.DECISION_KEEP:
            record = self._keep_artifact(
                session,
                runtime,
                kind=kind,
                entity_id=entity_id,
                check_id=check_id,
                note=note,
                download=download,
            )
        else:
            record = self._delete_artifact(
                session, runtime, kind=kind, entity_id=entity_id, check_id=check_id, note=note
            )
        self.save(session)
        panel = self._artifacts_view(session)
        return {
            "decision": decision,
            "artifact": record,
            "entity": {"kind": kind, "id": entity_id},
            "conclusion": str(panel.get("note") or ""),
            "artifacts": [dict(item) for item in (session.artifacts or [])],
            "view": panel.get("view") or {},
            "summary": panel.get("summary") or {},
        }

    def _keep_artifact(
        self,
        session: TestSession,
        runtime: Any,
        *,
        kind: str,
        entity_id: str,
        check_id: str,
        note: str,
        download: bool,
    ) -> dict[str, Any]:
        """Сохраняет артефакт: модель остаётся на стенде, файл при нужде выгружается.

        `download` для файла стенда обязателен по смыслу: «оставить» файл, лежащий
        на общем стенде, — это не доказательство, а надежда на то, что его не уберут.
        Поэтому `keep` без `download` оставляет файл на стенде и честно пишет об этом
        в примечании.
        """
        name = self._artifact_name(kind, entity_id)
        path: Path | None = None
        size: int | None = None
        if download:
            if kind == artifacts_api.ENTITY_MODEL:
                path, size = self._download_onnx(runtime, entity_id, name)
            else:
                path, size = self._download_file(runtime, entity_id, name)
        if kind == artifacts_api.ENTITY_MODEL:
            detail = f"{name} остаётся на стенде как артефакт испытаний"
        elif path is not None:
            detail = f"{name} выгружен в артефакты испытаний"
        else:
            detail = f"{name} оставлен на стенде (выгрузка не запрошена)"
        return session_api.add_artifact(
            session,
            kind=artifacts_api.KIND_LABELS[kind],
            name=name,
            path=path,
            size_bytes=size,
            entity_id=entity_id,
            action=session_api.ARTIFACT_KEPT,
            check_id=check_id,
            note="; ".join(part for part in (detail, note) if part),
        )

    def _delete_artifact(
        self,
        session: TestSession,
        runtime: Any,
        *,
        kind: str,
        entity_id: str,
        check_id: str,
        note: str,
    ) -> dict[str, Any]:
        """Убирает объект со стенда и фиксирует решение об удалении (`NFR-T4`)."""
        name = self._artifact_name(kind, entity_id)
        try:
            if kind == artifacts_api.ENTITY_MODEL:
                runtime.apis.models.delete_model(entity_id)
            else:
                runtime.apis.files.delete(entity_id)
        except ClientError as exc:
            raise self.fail(exc) from exc
        session_api.mark_test_entity_deleted(
            session, entity_id, check_id=check_id, note="решение оператора: удалить"
        )
        return session_api.add_artifact(
            session,
            kind=artifacts_api.KIND_LABELS[kind],
            name=name,
            entity_id=entity_id,
            action=session_api.ARTIFACT_DELETED,
            check_id=check_id,
            note="; ".join(part for part in (f"{name} удалён со стенда", note) if part),
        )

    def _artifact_name(self, kind: str, entity_id: str) -> str:
        """Имя объекта для записи об артефакте: из реестра, иначе — идентификатор."""
        runtime = self.state.runtime()
        if runtime is not None:
            try:
                if kind == artifacts_api.ENTITY_MODEL:
                    return str(runtime.apis.models.get_model(entity_id).name or entity_id)
                rows = runtime.apis.files.list_files(file_id=entity_id).files
                if rows:
                    return str(rows[0].file_name or entity_id)
            except READ_ERRORS:
                return entity_id
        return entity_id

    def _download_onnx(self, runtime: Any, entity_id: str, name: str) -> tuple[Path, int]:
        """Выгружает ONNX модели в артефакты и возвращает путь с размером."""
        try:
            payload = runtime.apis.models.download_onnx(entity_id)
        except ClientError as exc:
            raise self.fail(exc) from exc
        target = paths.ARTIFACT_DIR / f"{Path(name).stem or entity_id}.onnx"
        return self._write_artifact(target, payload.content)

    def _download_file(self, runtime: Any, entity_id: str, name: str) -> tuple[Path, int]:
        """Выгружает файл стенда в артефакты и возвращает путь с размером."""
        try:
            payload = runtime.apis.files.download(entity_id)
        except ClientError as exc:
            raise self.fail(exc) from exc
        target = paths.ARTIFACT_DIR / (Path(name).name or payload.file_name or entity_id)
        return self._write_artifact(target, payload.content)

    def _write_artifact(self, target: Path, content: bytes) -> tuple[Path, int]:
        """Пишет выгрузку в `acceptance_data/artifacts/` (каталог создаётся по требованию)."""
        paths.ensure_dirs()
        target.write_bytes(content)
        return target, len(content)

    # -- «Реальная тренировка»: карточка модели и профиль прогона (Э1 К2) -----
    def api_model(
        self, model_id: str, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/models/{model_id}` — карточка модели: свойства, сигналы, метрики.

        До этой правки свойства модели были видны только в ленте обмена, открыв ответ
        `GET /api/ml_models/models` (`llm_tasks/59`). Карточка берётся из реестра, а не
        отдельным запросом к стенду: списком пульт уже владеет, а лишнее обращение к
        общему стенду — расход, которого можно избежать. `?refresh=1` читает заново.
        """
        runtime = self.state.runtime()
        if runtime is None:
            raise ApiError(STAND_NOT_CONFIGURED, kind="state", status=409)
        models, error = self.state.models(refresh=_flag(query.get("refresh"), False))
        if error:
            raise ApiError(error, kind="client", status=502)
        found = next((item for item in models if str(item.id) == str(model_id)), None)
        if found is None:
            raise ApiError(
                f"модель {model_id} не найдена в реестре стенда", kind="http", status=404
            )
        files, _files_error = self.state.files()
        session = self.state.current_session()
        return {
            "card": view.model_card_view(found, session=session, files=files),
            "session_id": "" if session is None else str(session.session_id),
        }

    def _train_view(
        self, session: TestSession | None, query: Mapping[str, str], *, note: str = ""
    ) -> dict[str, Any]:
        """Экран «Реальная тренировка»: профиль сессии, отбор моделей и карточка.

        Отбор моделей идёт на стороне пульта: фильтр `device_id` у стенда отвечает `500`
        (`TC-MOD-05`), поэтому реестр читается целиком, а фильтры применяются здесь.
        """
        runtime = self.state.runtime()
        profile = dict(getattr(session, "ml_profile", None) or {}) if session is not None else {}
        session_id = "" if session is None else str(session.session_id)
        uploads = (
            list(getattr(session, "uploaded_files", None) or []) if session is not None else []
        )
        steps = list(getattr(session, "flow_steps", None) or []) if session is not None else []
        if runtime is None:
            return view.train_view(
                profile=profile,
                configured=False,
                session_id=session_id,
                note=STAND_NOT_CONFIGURED,
                uploads=uploads,
                steps=steps,
            )
        models, models_error = self.state.models(refresh=_flag(query.get("refresh"), False))
        files, _files_error = self.state.files()
        datasets, datasets_error = self.state.datasets()
        filters: dict[str, Any] = {
            "query": str(query.get("query") or ""),
            "architecture": str(query.get("architecture") or ""),
            "phase_type": str(query.get("phase_type") or ""),
            "output_size": str(query.get("output_size") or ""),
            "only_trained": _flag(query.get("only_trained"), False),
            "only_test": _flag(query.get("only_test"), False),
            "only_battle": _flag(query.get("only_battle"), False),
        }
        chosen = models_api.filter_models(
            models,
            query=filters["query"],
            architecture=filters["architecture"],
            phase_type=filters["phase_type"],
            output_size=filters["output_size"],
            only_trained=filters["only_trained"],
            only_test=filters["only_test"],
            only_battle=filters["only_battle"],
        )
        wanted = str(query.get("model") or "").strip() or str(profile.get("model_id") or "")
        card_model = next((item for item in models if str(item.id) == wanted), None)
        # Зона данных (Э2.1): паспорт сборки и состав берём из учёта пульта. Состав читается
        # в режиме `local`: страница не должна ждать стенд (`refresh` — отдельное действие).
        dataset_id = str(profile.get("dataset_id") or "")
        manifest = (
            session_api.dataset_manifest(session, dataset_id)
            if session is not None and dataset_id
            else None
        )
        composition = None
        if session is not None and dataset_id:
            composition = composition_api.read_composition(
                session,
                runtime.apis,
                dataset_id,
                mode=composition_api.MODE_LOCAL,
                name=str(profile.get("dataset_label") or ""),
                dataset_type="direct_fill",
                files=files,
                allow_heuristic=False,
            )
        notes = [
            part
            for part in (
                note,
                f"реестр моделей не прочитан: {models_error}" if models_error else "",
                f"реестр датасетов не прочитан: {datasets_error}" if datasets_error else "",
            )
            if part
        ]
        return view.train_view(
            profile=profile,
            models=chosen,
            files=files,
            datasets=datasets,
            filters=filters,
            options=models_api.filter_options(models),
            card=(
                view.model_card_view(card_model, files=files, session=session)
                if card_model is not None
                else {}
            ),
            session_id=session_id,
            note="; ".join(notes),
            uploads=uploads,
            manifest=manifest,
            composition=composition,
            steps=steps,
        )

    def api_train(self, *, query: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        """`GET /api/train` — состояние «Реальной тренировки» (`llm_tasks/59`)."""
        return self._train_view(self.state.current_session(), query)

    def api_train_action(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/train` — действия профиля: `save-profile`, `choose`, `clear`.

        Профиль живёт в сессии испытаний: без выбранной сессии действие отклоняется, иначе
        решение оператора ушло бы «в никуда» — не попало ни в отчёт, ни в протокол.
        Боевые имена в режиме полной нагрузки разрешены (решение заказчика 01.10.2026),
        но сама загрузка данных и создание сущностей — этап Э2: здесь фиксируется профиль.

        Идентификатор выбранной сущности не сверяется с реестром стенда намеренно: в Э3
        модель появляется **после** обучения, а реестр живёт в кэше 30 секунд — проверка
        «есть ли такая модель» отвергала бы только что созданную. Расхождение видно
        оператору честно: карточка по такому `id` отвечает 404 (`GET /api/models/{id}`).
        """
        session = self.session()
        action = str(payload.get("action") or "").strip()
        author = self._author()
        if action == "save-profile":
            session_api.update_ml_profile(
                session,
                object_of_test=str(payload.get("object_of_test") or "").strip(),
                goal=str(payload.get("goal") or "").strip(),
                responsible=str(payload.get("responsible") or "").strip() or author,
                full_load=_flag(payload.get("full_load"), False),
                dataset_validated=_flag(payload.get("dataset_validated"), False),
            )
        elif action == "choose":
            kind = str(payload.get("kind") or "").strip()
            entity_id = str(payload.get("id") or "").strip()
            label = str(payload.get("label") or "").strip()
            if kind == "model":
                if not entity_id:
                    raise ApiError("Не указана модель: нужен `id` модели стенда", status=422)
                session_api.update_ml_profile(
                    session, model_id=entity_id, model_label=label or entity_id
                )
            elif kind == "dataset":
                if not entity_id:
                    raise ApiError("Не указан датасет: нужен `id` датасета стенда", status=422)
                session_api.update_ml_profile(
                    session, dataset_id=entity_id, dataset_label=label or entity_id
                )
            else:
                raise ApiError(
                    f"неизвестный объект выбора: {kind or '—'}; ожидалось `model` или `dataset`",
                    status=422,
                )
        elif action == "clear":
            kind = str(payload.get("kind") or "").strip()
            keys: dict[str, dict[str, str]] = {
                "model": {"model_id": "", "model_label": ""},
                "dataset": {
                    "dataset_id": "",
                    "dataset_label": "",
                    "dataset_validated": "",
                },
                "profile": dict.fromkeys(session_api.ML_PROFILE_KEYS, ""),
            }
            if kind not in keys:
                raise ApiError(
                    "неизвестный объект сброса: "
                    f"{kind or '—'}; ожидалось " + ", ".join(f"`{name}`" for name in keys),
                    status=422,
                )
            session_api.update_ml_profile(session, **keys[kind])
        else:
            raise ApiError(
                f"неизвестное действие «Реальной тренировки»: {action or '—'}", status=422
            )
        self.save(session)
        answer = self._train_view(session, query)
        answer["profile"] = dict(session.ml_profile or {})
        return answer

    # -- данные «Реальной тренировки»: файлы и датасет (Э2.1 К2) -------------
    def _require_test_name(self, name: str, *, what: str, session: TestSession) -> str:
        """Проверяет правило имён §9 ТЗ: без режима полной нагрузки — только `__TEST__…`.

        На общем стенде пульт создаёт сущности только с префиксом `__TEST__`
        (`acceptance.endpoints.TEST_PREFIX`). Боевое имя разрешено лишь когда оператор
        уже подтвердил режим полной нагрузки — подтверждение с автором и временем
        лежит в профиле прогона (Э5 добавит двойное подтверждение).
        """
        value = str(name or "").strip()
        if not value:
            raise ApiError(f"не задано имя: {what} не может быть безымянным", status=422)
        if value.startswith(TEST_PREFIX):
            return value
        if (session.ml_profile or {}).get("full_load"):
            return value
        raise ApiError(
            f"имя {what} должно начинаться с `{TEST_PREFIX}` (§9 ТЗ): {value}. "
            "На общем стенде боевые имена разрешены только в режиме полной нагрузки "
            "(подтвердите режим в профиле прогона).",
            status=422,
        )

    def api_data_upload(
        self,
        *,
        query: Mapping[str, str],
        stream: Any = None,
        length: int = 0,
    ) -> dict[str, Any]:
        """`POST /api/data/files` — потоковая загрузка файла данных на стенд (Э2.1).

        Тело запроса — **файл**, а не JSON (`llm_tasks/64` §2): боевой RAW доходит до
        2,18 ГБ, поэтому пульт пишет поток во временный файл порциями, считая `sha256`,
        и отправляет его на стенд файловым объектом (`httpx` стримит). Разбор
        `multipart/form-data` не делается намеренно: оба конца наши, а параметры
        (`file_name`, `file_type`, `description`) идут в query-строке.

        Паспорт файла (имя, размер, `sha256`, `file_id` стенда) остаётся в сессии —
        это ответ отчёта на вопрос «что именно ушло в датасет».
        """
        session = self.session()
        runtime = self.runtime()
        file_name = str(query.get("file_name") or "").strip()
        file_type = str(query.get("file_type") or "RAW").strip().upper()
        description = str(query.get("description") or "").strip()
        roles = {key for key, _label in models_api.FILE_ROLES}
        if file_type not in roles:
            raise ApiError(
                f"неизвестный тип файла: {file_type or '—'}; ожидалось "
                + " или ".join(sorted(roles)),
                status=422,
            )
        name = self._require_test_name(file_name, what="файла", session=session)
        try:
            saved = uploads_api.save_stream(stream, int(length or 0), file_name=name)
        except uploads_api.UploadError as exc:
            raise ApiError(exc.message, kind="validation", status=exc.status) from exc
        try:
            meta = runtime.apis.files.upload(
                file_name=saved.name,
                path=saved.path,
                file_type=file_type,
                description=description,
            )
        except ClientError as exc:
            raise self.fail(exc) from exc
        finally:
            saved.discard()
        entry = session_api.add_uploaded_file(
            session,
            name=str(meta.file_name),
            size=int(meta.size),
            sha256=saved.sha256,
            file_type=file_type,
            file_id=str(meta.id),
            author=self._author(),
        )
        session_api.add_flow_step(
            session,
            step="files",
            status=f"загружено: {entry['name']}",
            note=f"{saved.size} Б, sha256 {saved.sha256[:12]}…",
            author=self._author(),
            file_id=str(meta.id),
            size=saved.size,
        )
        self.save(session)
        self.state.invalidate("files", "datasets", "tasks")
        answer = self._train_view(session, query)
        answer["file"] = {
            "id": str(meta.id),
            "file_name": str(meta.file_name),
            "size": int(meta.size),
            "size_text": models_api.human_size(int(meta.size)),
            "sha256": saved.sha256,
            "file_type": file_type,
        }
        return answer

    def api_datasets_action(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/datasets` — действия с датасетом К2: создать → выбрать → наполнить.

        Действия (по одному на кнопку интерфейса):

        * `create` — `POST /api/datasets/` с именем (§9: `__TEST__…` вне полной нагрузки);
        * `choose` — выбрать существующий датасет стенда (профиль прогона);
        * `fill` — наполнить выбранными файлами (`POST /api/datasets/fill/{id}`, 202).
          Факт «что именно отправлено» записывается **до** вызова: стенд не возвращает
          ни состав, ни `task_id`, поэтому только учёт пульта знает правду;
        * `refresh` — перечитать состав с учётом стенда (режим `auto`);
        * `validate` — отметить «датасет проверен вне пульта» и зафиксировать паспорт
          сборки (решение заказчика №6: валидация внешняя, пульт хранит факт);
        * `delete` — удалить датасет и снять выбор, если он был выбран.
        """
        session = self.session()
        runtime = self.runtime()
        action = str(payload.get("action") or "").strip()
        author = self._author()
        dataset_id = str(payload.get("dataset_id") or "").strip() or str(
            (session.ml_profile or {}).get("dataset_id") or ""
        )
        files, _files_error = self.state.files()
        answer: dict[str, Any] = {"action": action, "dataset_id": dataset_id}

        if action == "create":
            name = self._require_test_name(
                str(payload.get("name") or "").strip(), what="датасета", session=session
            )
            try:
                created = runtime.apis.datasets.create_dataset(
                    name=name,
                    description=str(payload.get("description") or "").strip() or None,
                    dataset_type="direct_fill",
                )
            except ClientError as exc:
                raise self.fail(exc) from exc
            dataset_id = str(created.id)
            session_api.update_ml_profile(
                session, dataset_id=dataset_id, dataset_label=str(created.name or name)
            )
            session_api.add_flow_step(
                session,
                step="dataset",
                status=f"создан: {created.name or name}",
                note=f"id {dataset_id}, тип {created.type or 'direct_fill'}",
                author=author,
            )
            answer["dataset"] = {
                "id": dataset_id,
                "name": str(created.name or name),
                "type": str(created.type or "direct_fill"),
            }
        elif action == "choose":
            if not dataset_id:
                raise ApiError("Не указан датасет: нужен `dataset_id`", status=422)
            label = str(payload.get("label") or "").strip() or str(
                payload.get("name") or dataset_id
            )
            session_api.update_ml_profile(session, dataset_id=dataset_id, dataset_label=label)
            session_api.add_flow_step(
                session, step="dataset", status=f"выбран: {label}", author=author
            )
        elif action == "fill":
            if not dataset_id:
                raise ApiError(
                    "Нечего наполнять: сначала создайте или выберите датасет", status=422
                )
            raw_ids = [str(item) for item in (payload.get("raw_file_ids") or [])]
            markup_ids = [str(item) for item in (payload.get("markup_file_ids") or [])]
            if not raw_ids and not markup_ids:
                raw_ids = self._uploaded_ids(session, payload, file_type="RAW")
                markup_ids = self._uploaded_ids(session, payload, file_type="LOADS")
            if not raw_ids:
                raise ApiError("Не выбраны файлы измерений (RAW): наполнять нечем", status=422)
            label = str((session.ml_profile or {}).get("dataset_label") or dataset_id)
            composition_api.record_fill(
                session,
                dataset_id=dataset_id,
                raw_file_ids=raw_ids,
                markup_file_ids=markup_ids,
                name=label,
                dataset_type="direct_fill",
                operator=author,
                note="наполнение из пульта (Э2.1)",
                files=files,
            )
            try:
                runtime.apis.datasets.fill_dataset(
                    dataset_id, raw_file_ids=raw_ids, markup_file_ids=markup_ids
                )
            except ClientError as exc:
                raise self.fail(exc) from exc
            task = self._newest_fill_task(dataset_id)
            session_api.add_dataset_manifest(
                session,
                dataset_id=dataset_id,
                dataset_label=label,
                files=self._manifest_files(session, raw_ids, markup_ids),
                note=str(payload.get("note") or "").strip(),
                author=author,
            )
            session_api.add_flow_step(
                session,
                step="fill",
                status="наполнение запущено" if task else "задача наполнения не видна",
                note=(
                    f"RAW: {len(raw_ids)}, разметка: {len(markup_ids)}"
                    + (
                        f"; задача {task['id']}"
                        if task
                        else "; стенд не отдал `task_id` (ответ 202)"
                    )
                ),
                author=author,
                task_id=str((task or {}).get("id") or ""),
                raw=len(raw_ids),
                markup=len(markup_ids),
            )
            answer["task"] = dict(task or {})
            answer["raw_file_ids"] = raw_ids
            answer["markup_file_ids"] = markup_ids
        elif action == "refresh":
            if not dataset_id:
                raise ApiError("Не выбран датасет: нечего перечитывать", status=422)
            label = str((session.ml_profile or {}).get("dataset_label") or dataset_id)
            composition = composition_api.read_composition(
                session,
                runtime.apis,
                dataset_id,
                mode=composition_api.MODE_AUTO,
                name=label,
                dataset_type="direct_fill",
                files=files,
            )
            record = composition_api.record_composition(session, composition)
            session_api.add_flow_step(
                session,
                step="fill",
                status=f"состав: {composition.source_label}",
                note=f"файлов {composition.files_count} ({composition.note or 'без пояснений'})",
                author=author,
                source=composition.source,
            )
            answer["composition"] = record
        elif action == "validate":
            if not dataset_id:
                raise ApiError("Не выбран датасет: отмечать нечего", status=422)
            manifest = session_api.dataset_manifest(session, dataset_id) or {
                "files": [],
                "file_count": 0,
            }
            session_api.update_ml_profile(session, dataset_validated=True)
            session_api.add_flow_step(
                session,
                step="validated",
                status="проверен вне пульта",
                note=f"отметка оператора; файлов в паспорте: {manifest.get('file_count') or 0}",
                author=author,
            )
            answer["manifest"] = dict(manifest)
        elif action == "delete":
            if not dataset_id:
                raise ApiError("Не указан датасет: нужен `dataset_id`", status=422)
            try:
                runtime.apis.datasets.delete_dataset(dataset_id)
            except ClientError as exc:
                raise self.fail(exc) from exc
            if str((session.ml_profile or {}).get("dataset_id") or "") == dataset_id:
                session_api.update_ml_profile(
                    session, dataset_id="", dataset_label="", dataset_validated=""
                )
            session_api.add_flow_step(
                session, step="dataset", status="удалён", note=f"id {dataset_id}", author=author
            )
            answer["deleted"] = dataset_id
        else:
            raise ApiError(
                f"неизвестное действие с датасетом: {action or '—'}; ожидалось "
                "`create`, `choose`, `fill`, `refresh`, `validate` или `delete`",
                status=422,
            )

        self.save(session)
        self.state.invalidate("datasets", "tasks")
        answer["view"] = self._train_view(session, query)
        return answer

    def api_dataset(
        self, dataset_id: str, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/datasets/{dataset_id}` — карточка данных: метаданные, паспорт, состав.

        Состав по умолчанию читается из учёта пульта (`local`) — страница не ждёт стенд;
        `?refresh=1` спрашивает стенд (режим `auto`): это отдельное действие оператора.
        """
        session = self.session()
        runtime = self.state.runtime()
        files, files_error = self.state.files()
        manifest = session_api.dataset_manifest(session, dataset_id)
        meta = next(
            (
                row
                for row in models_api.dataset_rows(self.state.datasets()[0])
                if row["id"] == dataset_id
            ),
            {},
        )
        if not meta and runtime is not None:
            try:
                found = runtime.apis.datasets.get_dataset(dataset_id)
            except READ_ERRORS as exc:
                meta = {
                    "id": dataset_id,
                    "name": "",
                    "error": str(exc).strip() or exc.__class__.__name__,
                }
            else:
                meta = models_api.dataset_rows([found])[0]
        composition = None
        if runtime is not None:
            composition = composition_api.read_composition(
                session,
                runtime.apis,
                dataset_id,
                mode=(
                    composition_api.MODE_AUTO
                    if _flag(query.get("refresh"), False)
                    else composition_api.MODE_LOCAL
                ),
                name=str(meta.get("name") or ""),
                dataset_type=str(meta.get("type") or "direct_fill"),
                files=files,
                allow_heuristic=False,
            )
        return {
            "dataset_id": str(dataset_id),
            "meta": meta,
            "manifest": dict(manifest or {}),
            "manifest_rows": models_api.manifest_summary(manifest),
            "manifest_files": models_api.upload_rows(dict(manifest or {}).get("files") or []),
            "composition": (
                composition_api.composition_rows([composition])[0] if composition else {}
            ),
            "note": f"реестр файлов не прочитан: {files_error}" if files_error else "",
        }

    def _uploaded_ids(
        self, session: TestSession, payload: Mapping[str, Any], *, file_type: str
    ) -> list[str]:
        """Идентификаторы выбранных файлов сессии по роли (`RAW`/`LOADS`).

        `selected` в запросе — имена файлов (оператор видит именно их), пустой список
        означает «все загруженные файлы этой роли»: обычно так и наполняют датасет.
        """
        wanted = {str(item) for item in (payload.get("selected") or [])}
        ids: list[str] = []
        for item in session.uploaded_files:
            role = str(item.get("role") or item.get("file_type") or "").upper()
            if role != file_type:
                continue
            if wanted and str(item.get("name") or "") not in wanted:
                continue
            file_id = str(item.get("file_id") or "")
            if file_id:
                ids.append(file_id)
        return ids

    def _newest_fill_task(self, dataset_id: str) -> dict[str, Any] | None:
        """Свежая задача `dataset-fill` по датасету (`202` не возвращает `task_id`).

        Стенд не связывает ответ `fill` с задачей, поэтому задача ищется в реестре
        `$Задач`; если воркер её не разбирает (дефект стенда), здесь будет `None`, и
        экран честно скажет «задача наполнения не видна».
        """
        tasks, _count, error = self.state.tasks(task_type="dataset-fill", refresh=True)
        if error:
            return None
        found = [item for item in tasks if composition_api.task_matches_dataset(item, dataset_id)]
        if not found:
            return None
        newest = max(found, key=lambda item: str(getattr(item, "created_at", "") or ""))
        return {
            "id": str(getattr(newest, "id", "") or ""),
            "type": str(getattr(newest, "task_type", "") or ""),
            "status": str(getattr(newest, "status", "") or ""),
            "created_at": str(getattr(newest, "created_at", "") or ""),
        }

    def _manifest_files(
        self, session: TestSession, raw_ids: Sequence[str], markup_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Строки паспорта сборки: паспорта файлов сессии, разложенные по ролям.

        Роль берётся из отправленного списка, а не из паспорта файла: она отвечает на
        вопрос «чем этот файл был в **этом** датасете».
        """
        roles = {**dict.fromkeys(raw_ids, "RAW"), **dict.fromkeys(markup_ids, "LOADS")}
        rows: list[dict[str, Any]] = []
        for item in session.uploaded_files:
            file_id = str(item.get("file_id") or "")
            if file_id not in roles:
                continue
            rows.append({**dict(item), "role": roles[file_id]})
        return rows

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
        """Разбирает запрос, вызывает приложение и отправляет ответ.

        Запрос с **файловым** телом (`STREAM_ROUTES`) уходит в `dispatch_stream`: тело
        такого запроса нельзя читать в память — боевой RAW доходит до 2,18 ГБ
        (`llm_tasks/64` §2), поэтому обработчик получает поток и длину.
        """
        parts = urlsplit(self.path)
        try:
            query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
            if (method, parts.path) in STREAM_PATHS:
                result = self.server.app.dispatch_stream(
                    parts.path, query, self.rfile, self._stream_length()
                )
            else:
                payload = self._body() if method == "POST" else {}
                result = self.server.app.dispatch(method, parts.path, query, payload)
        except ApiError as exc:
            self._error(exc)
            return
        except Exception as exc:  # защитный контур: интерфейс не падает молча
            LOG.exception("необработанная ошибка API %s %s", method, parts.path)
            self._error(ApiError(str(exc) or exc.__class__.__name__, kind="server", status=500))
            return
        self._ok(result)

    def _stream_length(self) -> int:
        """Длина файлового тела запроса; пустое тело — понятная ошибка, а не «файл 0 Б»."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError("тело запроса пустое: файл не передан", kind="http", status=400)
        return length

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
    ("GET", "/api/artifacts", "api_artifacts"),
    ("POST", "/api/artifacts", "api_artifacts_action"),
    ("POST", "/api/checks/{check_id}", "api_check_action"),
    ("GET", "/api/journal", "api_journal"),
    ("POST", "/api/journal/repeat", "api_journal_repeat"),
    ("GET", "/api/protocol", "api_protocol"),
    ("GET", "/api/results", "api_results"),
    ("GET", "/api/data", "api_data"),
    ("GET", "/api/models/{model_id}", "api_model"),
    ("GET", "/api/train", "api_train"),
    ("POST", "/api/train", "api_train_action"),
    ("POST", "/api/data/files", "api_data_upload"),
    ("GET", "/api/datasets/{dataset_id}", "api_dataset"),
    ("POST", "/api/datasets", "api_datasets_action"),
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

#: Маршруты, где тело запроса — **файл**, а не JSON (`llm_tasks/64` §2): параметры идут
#: в query-строке, тело пишется во временный файл порциями. Разбор `multipart/form-data`
#: не делается намеренно: оба конца (страница и сервер) наши.
STREAM_ROUTES: tuple[tuple[str, str], ...] = (("/api/data/files", "api_data_upload"),)

#: Скомпилированные «файловые» маршруты: (шаблон, имя обработчика).
COMPILED_STREAM: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (compile_route(pattern), name) for pattern, name in STREAM_ROUTES
)

#: Пары «метод + путь» для файловых запросов: их тело пишется на диск, а не разбирается.
STREAM_PATHS: frozenset[tuple[str, str]] = frozenset(
    ("POST", pattern) for pattern, _name in STREAM_ROUTES
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
