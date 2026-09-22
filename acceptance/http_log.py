"""Логирующий транспорт httpx и журнал обмена с испытуемым сервером (FR-T6).

`LoggingTransport` встраивается между httpx-клиентом и сетью (параметр `transport`
в `client.http.ApiHttpClient`), поэтому **любой** запрос пульта — из чек-листа,
из консоли запросов или из экранов — попадает в журнал в одном формате:

    * в память (`Journal`) — для экрана «Журнал» и сводок;
    * в структурный JSONL сессии — через `log_event` (см. `logging_setup`).

Фиксируются: метод, путь, query, назначение (метка проверки), статус, длительность,
размеры запроса/ответа, `Content-Type`, ошибка соединения, усечённые тела
(text/json/csv — с лимитом; бинарные данные не сохраняются, только размер).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

import httpx

from acceptance.config import DEFAULT_BODY_LIMIT, DEFAULT_JOURNAL_MAX
from acceptance.logging_setup import (
    EVENT_HTTP_ERROR,
    EVENT_HTTP_REQUEST,
    EVENT_HTTP_RESPONSE,
    log_event,
)

TEXT_CONTENT_MARKERS = ("json", "text", "csv", "xml", "javascript", "urlencoded")

#: Сколько заголовков запроса/ответа сохранять в записи журнала (защита от «простыней»).
MAX_HEADERS = 40

_check_label: ContextVar[str | None] = ContextVar("pult_check_label", default=None)


@contextmanager
def label_context(label: str) -> Iterator[None]:
    """Помечает все запросы внутри блока меткой (обычно — id проверки чек-листа)."""
    token = _check_label.set(label)
    try:
        yield
    finally:
        _check_label.reset(token)


def current_label() -> str | None:
    """Текущая метка запросов (None вне контекста проверки)."""
    return _check_label.get()


@dataclass(frozen=True)
class HttpExchange:
    """Один обмен с сервером: запрос + ответ (или ошибка соединения).

    Заголовки хранятся кортежами пар (запись неизменяемая и должна оставаться
    хешируемой); для показа и выгрузки есть словари `request_header_map` и
    `response_header_map`.
    """

    seq: int
    started_at: str
    method: str
    path: str
    query: str
    status: int | None
    duration_ms: float
    request_bytes: int | None
    response_bytes: int | None
    content_type: str | None
    error: str | None = None
    label: str | None = None
    request_body: str | None = None
    response_body: str | None = None
    body_truncated: bool = False
    request_headers: tuple[tuple[str, str], ...] = ()
    response_headers: tuple[tuple[str, str], ...] = ()

    @property
    def request_header_map(self) -> dict[str, str]:
        """Заголовки запроса словарём (для интерфейса и отчёта)."""
        return dict(self.request_headers)

    @property
    def response_header_map(self) -> dict[str, str]:
        """Заголовки ответа словарём (для интерфейса и отчёта)."""
        return dict(self.response_headers)

    @property
    def url(self) -> str:
        """Путь с query-строкой (без схемы и хоста)."""
        return f"{self.path}?{self.query}" if self.query else self.path

    @property
    def is_error(self) -> bool:
        """True для сетевых ошибок и ответов 4xx/5xx."""
        if self.error is not None:
            return True
        return self.status is not None and self.status >= 400

    def as_dict(self) -> dict[str, Any]:
        """Представление для журнала/отчёта."""
        return {
            "seq": self.seq,
            "started_at": self.started_at,
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "content_type": self.content_type,
            "error": self.error,
            "label": self.label,
            "request_body": self.request_body,
            "response_body": self.response_body,
            "body_truncated": self.body_truncated,
            "request_headers": self.request_header_map,
            "response_headers": self.response_header_map,
        }

    @property
    def one_line(self) -> str:
        """Краткая строка для таблицы журнала."""
        outcome = str(self.status) if self.status is not None else "нет ответа"
        if self.error:
            outcome = self.error
        return f"{self.method} {self.url} → {outcome} · {self.duration_ms:.0f} мс"


class Journal:
    """Журнал обмена с сервером в памяти процесса пульта (bounded, thread-safe)."""

    def __init__(self, *, max_records: int = DEFAULT_JOURNAL_MAX) -> None:
        self._records: deque[HttpExchange] = deque(maxlen=max_records)
        self._lock = Lock()
        self._seq = 0

    # -- запись ---------------------------------------------------------------
    def next_seq(self) -> int:
        """Следующий порядковый номер обмена (нумерация не сбрасывается)."""
        with self._lock:
            self._seq += 1
            return self._seq

    def peek_next_seq(self) -> int:
        """Номер, который получит следующий обмен (без его расходования).

        Нужен проверкам: перед запуском фиксируется `journal_from` — с какого
        номера начинается диапазон записей проверки.
        """
        with self._lock:
            return self._seq + 1

    def add(self, exchange: HttpExchange) -> HttpExchange:
        """Добавляет запись в журнал."""
        with self._lock:
            self._records.append(exchange)
        return exchange

    def clear(self) -> None:
        """Очищает журнал (нумерация сохраняется)."""
        with self._lock:
            self._records.clear()

    # -- чтение --------------------------------------------------------------
    @property
    def records(self) -> tuple[HttpExchange, ...]:
        """Все записи журнала (старые — первыми)."""
        with self._lock:
            return tuple(self._records)

    @property
    def errors(self) -> tuple[HttpExchange, ...]:
        """Записи с ошибкой соединения или статусом ≥ 400."""
        return tuple(record for record in self.records if record.is_error)

    def for_label(self, label: str) -> tuple[HttpExchange, ...]:
        """Записи, помеченные указанной меткой (например, id проверки)."""
        return tuple(record for record in self.records if record.label == label)

    def to_dicts(self) -> list[dict[str, Any]]:
        """Серия записей журнала (для выгрузки и отчёта)."""
        return [record.as_dict() for record in self.records]

    def summary(self) -> dict[str, Any]:
        """Сводка журнала: счётчики по методам/классам статусов, объёмы, ошибки."""
        records = self.records
        by_method: dict[str, int] = {}
        by_status: dict[str, int] = {}
        labels: dict[str, int] = {}
        request_bytes = 0
        response_bytes = 0

        for record in records:
            by_method[record.method] = by_method.get(record.method, 0) + 1
            if record.status is not None:
                status_class = f"{record.status // 100}xx"
                by_status[status_class] = by_status.get(status_class, 0) + 1
            else:
                by_status["нет ответа"] = by_status.get("нет ответа", 0) + 1
            if record.label:
                labels[record.label] = labels.get(record.label, 0) + 1
            request_bytes += record.request_bytes or 0
            response_bytes += record.response_bytes or 0

        slowest = max((record.duration_ms for record in records), default=0.0)
        return {
            "total": len(records),
            "errors": sum(1 for record in records if record.is_error),
            "by_method": by_method,
            "by_status": by_status,
            "labels": labels,
            "request_bytes": request_bytes,
            "response_bytes": response_bytes,
            "slowest_ms": round(slowest, 1),
            "first_at": records[0].started_at if records else None,
            "last_at": records[-1].started_at if records else None,
        }


class LoggingTransport(httpx.BaseTransport):
    """Транспорт httpx, записывающий каждый обмен в журнал и структурный лог."""

    def __init__(
        self,
        journal: Journal,
        inner: httpx.BaseTransport | None = None,
        *,
        body_limit: int = DEFAULT_BODY_LIMIT,
        log_bodies: bool = True,
    ) -> None:
        self._journal = journal
        self._inner = inner if inner is not None else httpx.HTTPTransport()
        self._body_limit = body_limit
        self._log_bodies = log_bodies

    # -- внутренние помощники -------------------------------------------------
    def _request_body(self, request: httpx.Request) -> tuple[int | None, str | None, bool]:
        """Размер и (если это осмысленно) усечённое тело запроса."""
        try:
            size = len(request.content)
        except Exception:  # noqa: BLE001 — тело может быть потоковым
            return None, None, False

        content_type = (request.headers.get("content-type") or "").lower()
        if not self._log_bodies or not size:
            return size, None, False
        if "multipart/form-data" in content_type:
            return size, f"<multipart/form-data: {size} байт>", False
        if not _is_text(content_type):
            return size, None, False

        text = request.content.decode("utf-8", errors="replace")
        truncated = len(text) > self._body_limit
        return size, text[: self._body_limit], truncated

    def _response_body(self, response: httpx.Response) -> tuple[int | None, str | None, bool]:
        """Размер и (если это осмысленно) усечённое тело ответа."""
        try:
            content = response.read()
        except Exception:  # noqa: BLE001 — тело может быть потоковым
            return None, None, False

        size = len(content)
        content_type = (response.headers.get("content-type") or "").lower()
        if not self._log_bodies or not size or not _is_text(content_type):
            return size, None, False

        text = content.decode("utf-8", errors="replace")
        truncated = len(text) > self._body_limit
        return size, text[: self._body_limit], truncated

    def _record(self, exchange: HttpExchange, level: str, event: str) -> None:
        self._journal.add(exchange)
        log_event(event, exchange.one_line, level=level, module="http", payload=exchange.as_dict())

    # -- httpx.BaseTransport --------------------------------------------------
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Выполняет запрос, фиксируя обмен в журнале (в т.ч. при ошибке соединения)."""
        seq = self._journal.next_seq()
        started_at = datetime.now().isoformat(timespec="milliseconds")
        started = time.perf_counter()
        label = current_label()
        path = request.url.path
        query = (
            request.url.query.decode()
            if isinstance(request.url.query, bytes)
            else str(request.url.query)
        )

        request_bytes, request_body, request_truncated = self._request_body(request)
        request_headers = _headers(request.headers)
        log_event(
            EVENT_HTTP_REQUEST,
            f"{request.method} {path}",
            level="DEBUG",
            module="http",
            check_id=label,
            payload={
                "seq": seq,
                "label": label,
                "method": request.method,
                "path": path,
                "query": query,
                "request_bytes": request_bytes,
                "request_body": request_body,
                "content_type": request.headers.get("content-type"),
                "headers": dict(request_headers),
            },
        )

        try:
            response = self._inner.handle_request(request)
        except httpx.HTTPError as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            self._record(
                HttpExchange(
                    seq=seq,
                    started_at=started_at,
                    method=request.method,
                    path=path,
                    query=query,
                    status=None,
                    duration_ms=duration_ms,
                    request_bytes=request_bytes,
                    response_bytes=None,
                    content_type=None,
                    error=f"{type(exc).__name__}: {exc}",
                    label=label,
                    request_body=request_body,
                    body_truncated=request_truncated,
                    request_headers=request_headers,
                ),
                level="ERROR",
                event=EVENT_HTTP_ERROR,
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response_bytes, response_body, response_truncated = self._response_body(response)
        response_headers = _headers(response.headers)
        self._record(
            HttpExchange(
                seq=seq,
                started_at=started_at,
                method=request.method,
                path=path,
                query=query,
                status=response.status_code,
                duration_ms=duration_ms,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                content_type=(response.headers.get("content-type") or "").split(";")[0].strip()
                or None,
                label=label,
                request_body=request_body,
                response_body=response_body,
                body_truncated=request_truncated or response_truncated,
                request_headers=request_headers,
                response_headers=response_headers,
            ),
            level="WARNING" if response.status_code >= 400 else "DEBUG",
            event=EVENT_HTTP_RESPONSE,
        )
        return response

    def close(self) -> None:
        """Закрывает вложенный транспорт."""
        self._inner.close()


def _headers(headers: httpx.Headers) -> tuple[tuple[str, str], ...]:
    """Снимок заголовков для записи журнала (не больше `MAX_HEADERS` пар)."""
    return tuple((str(key).lower(), str(value)) for key, value in headers.items())[:MAX_HEADERS]


def _is_text(content_type: str) -> bool:
    """True, если тело такого типа имеет смысл сохранять в журнал как текст."""
    if not content_type:
        return False
    return any(marker in content_type for marker in TEXT_CONTENT_MARKERS)
