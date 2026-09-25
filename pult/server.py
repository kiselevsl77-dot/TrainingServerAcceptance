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
from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.checks import catalog
from acceptance.exchange import execute_request
from acceptance.runner import (
    can_run,
    execute_call,
    execute_next,
    needs_confirmation,
    next_label,
    run_batch_tech,
)
from acceptance.session import TestSession
from client.errors import ClientError
from pult import view
from pult.state import STAND_NOT_CONFIGURED, PultState

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
    last_run: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.web_dir is None:
            self.web_dir = Path(__file__).resolve().parent / "web"

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

    def queue(self, session: TestSession, *, mode: str = "") -> queue_api.Queue:
        """Очередь прогона по ревизии сессии с восстановлением состояния (схема v8)."""
        programme = self.programme(session)
        return run_state.run_queue(
            session, programme, self.state.journal_records(), mode=mode or None
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
        prepared = view.report_view(report_api.readiness(session))
        conclusion = str(payload.get("conclusion") or session.info.conclusion or "").strip()
        closed = bool(payload.get("close", True))
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
        """Готовый вид прогона: строки, прогресс, «▶ Следующая» и требования пункта."""
        written_ids = [str(check_id) for check_id in (written or self.confirm_requests(session))]
        programme = self.programme(session)
        queue = self.queue(session, mode=mode)
        results = results_api.state(session)
        runnable, reason = can_run(queue, programme)
        following = queue.next_item()
        next_id = "" if following is None else following.check_id
        item = programme.find(next_id) if next_id else None
        confirm_required, confirm_note = (
            needs_confirmation(item) if item is not None else (False, "")
        )
        return {
            "session": {"id": session.session_id, "status": session.status},
            "revision": queue.revision,
            "mode": queue.mode,
            "mode_label": queue.mode_label,
            "pause": queue.pause_seconds,
            "skip_done": bool(session.skip_done),
            "progress": run_state.progress(queue, results),
            "next": {
                "check_id": next_id,
                "label": next_label(queue),
                "runnable": bool(runnable and following is not None),
                "reason": reason
                if following is not None
                else "Прогон завершён: все пункты закрыты",
                "confirm_required": bool(confirm_required),
                "confirm_note": str(confirm_note),
            },
            "card_ready": bool(self.run_cards.get(next_id)),
            "rows": view.run_rows(session, programme, queue),
            "written": written_ids,
            "last_run": dict(self.last_run),
        }

    def api_run_action(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/run` — `next|batch-tech|call` (`IR-P-4`, `A4`).

        Замок `run_lock` не даёт двум прогонам идти одновременно: случайный двойной клик
        получает «прогон уже идёт», а не второй вызов проверки на стенд (`NFR-P-5`).
        """
        action = str(payload.get("action") or "next").strip()
        session = self.session()
        runtime = self.runtime()
        programme = self.programme(session)
        mode = str(payload.get("mode") or "").strip()
        if "skip_done" in payload:
            session.skip_done = bool(payload.get("skip_done"))
            self.save(session)
        queue = self.queue(session, mode=mode)
        check_id = str(payload.get("check_id") or "").strip() or None
        following = queue.next_item()
        target = check_id or ("" if following is None else following.check_id)
        card = self.run_cards.get(target) if target else None
        params = {str(key): value for key, value in dict(payload.get("params") or {}).items()}
        author = self._author()
        client = self.state.console_client()
        if not self.run_lock.acquire(blocking=False):
            raise ApiError("Прогон уже идёт: дождитесь окончания текущего пункта", status=409)
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
            elif action == "call":
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
            else:
                raise ApiError(f"неизвестное действие прогона: {action}", status=422)
        finally:
            self.run_lock.release()
        self.save(session)
        rows = view.outcome_rows(outcomes)
        self.last_run = {
            "action": action,
            "at": view.short_time(session_api.now_iso(), "%H:%M:%S"),
            "rows": rows,
        }
        data = self._run_view(session, written=[], mode=mode)
        data["outcomes"] = rows
        return data

    # -- действия по пункту, лента обмена ------------------------------------
    def api_check_action(
        self, check_id: str, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/checks/{id}` — `annotate|suspend|resume|run-card` (`FR-P-15`, `FR-P-19`)."""
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
        elif action == "run-card":
            if item is None:
                raise ApiError(f"проверка {check_id} не входит в программу сессии", status=404)
            values = {str(key): value for key, value in dict(payload.get("values") or {}).items()}
            missing = run_card.validate(values)
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
        """`GET /api/journal` — лента обмена: №, метод, путь, статус, тело и `curl` (`FR-P-21`)."""
        records = self.state.journal_records()
        journal = self.state.journal()
        base_url = self._base_url()
        limit = int(query.get("limit") or 50)
        rows = view.journal_rows(
            records,
            base_url=base_url,
            limit=limit,
            errors_only=_flag(query.get("errors_only"), False),
            label=str(query.get("label") or ""),
            detail_request=_flag(query.get("request"), True),
            detail_response=_flag(query.get("response"), True),
        )
        return {
            "rows": rows,
            "summary": journal.summary(),
            "levels": ["DEBUG", "INFO", "WARNING", "ERROR"],
            "log_level": self.state.log_level(),
            "base_url": base_url,
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
        """`GET /api/data` — файлы, записи RAW+markup, `$Нагрузки`, `$Датасеты` с составом.

        Составы датасетов читаются только по запросу (`id=…` для одного или
        `compositions=1` для всех): за каждым стоит обращение к `$Задачам` стенда
        (`GET /api/tasks/?task_type=dataset-fill`), а реестр должен открываться быстро.
        Проверки, подтверждённые этим чтением, закрываются по факту запроса (`UC-03`).
        """
        refresh = _flag(query.get("refresh"), False)
        files, files_error = self.state.files(refresh=refresh)
        loads, loads_error = self.state.loads(refresh=refresh)
        datasets, datasets_error = self.state.datasets(refresh=refresh)
        overview = None
        records_error = ""
        if files:
            try:
                overview = records_api.build_records(files)
            except (ValueError, TypeError, AttributeError) as exc:
                records_error = f"объединение в записи не выполнено: {exc}"
                LOG.warning(records_error)
        session = self.state.current_session()
        written = [] if session is None else self.confirm_requests(session)
        return {
            "files": view.file_rows(files),
            "records": [] if overview is None else view.record_rows(overview),
            "records_stats": {} if overview is None else overview.stats.to_dict(),
            "records_attention": 0 if overview is None else len(overview.requires_attention),
            "loads": view.load_rows(loads),
            "datasets": view.dataset_rows(
                datasets, compositions=self._compositions(session, datasets, files, query)
            ),
            "errors": {
                "files": files_error or "",
                "records": records_error,
                "loads": loads_error or "",
                "datasets": datasets_error or "",
            },
            "written": written,
        }

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
            "statuses": list(view.status_labels()),
            "sections": sorted({str(row["section"]) for row in rows if row.get("section")}),
            "total": len(rows),
            "rows": filtered,
        }

    def api_report_readiness(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`GET /api/report/readiness` — полнота сессии и «чего не хватает» (`FR-P-48`)."""
        return view.report_view(report_api.readiness(self.session()))

    def api_report_build(
        self, *, query: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """`POST /api/report/build` — комплект отчёта без обращений к стенду (`FR-P-50`)."""
        session = self.session()
        if not bool(payload.get("build", True)):
            return {"readiness": view.report_view(report_api.readiness(session))}
        try:
            paths = report_api.save_report(session, journal_records=self.state.journal_records())
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ApiError(f"комплект отчёта собрать не удалось: {exc}", status=500) from exc
        annexes, missing = report_api.manifest_annexes(session)
        return {
            "paths": {str(key): path.as_posix() for key, path in paths.items()},
            "annexes": sorted(str(key) for key in annexes),
            "missing": [str(item) for item in missing],
            "readiness": view.report_view(report_api.readiness(session)),
        }


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
    ("POST", "/api/checks/{check_id}", "api_check_action"),
    ("GET", "/api/journal", "api_journal"),
    ("POST", "/api/journal/repeat", "api_journal_repeat"),
    ("GET", "/api/protocol", "api_protocol"),
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
