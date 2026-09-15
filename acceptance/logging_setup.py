"""Настройка журналирования пульта испытаний (FR-T6).

Три назначения (sinks) loguru:
    * консоль — короткий формат для оператора;
    * `acceptance_data/logs/app.log` — текстовый журнал приложения
      (ротация 10 МБ, хранение 10 файлов, UTF-8);
    * `acceptance_data/logs/session_<id>.jsonl` — **структурный** журнал сессии
      (`serialize=True`): именно он служит машинным следом испытаний и основой
      для отчёта (воспроизводимость: по нему повторяется любой шаг).

Каждая запись снабжается контекстом (`extra`):
    session_id — идентификатор сессии испытаний;
    check_id   — проверка чек-листа, к которой относится событие;
    module     — модуль/экран пульта;
    event      — тип события (`app`, `http_request`, `http_response`, `http_error`, …);
    payload    — структурированные данные события.

Пример:
    >>> log_event("session_started", "Сессия начата", module="session",
    ...           payload={"session_id": "20260915-101500-1a2b"})
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from acceptance.config import DEFAULT_LOG_LEVEL
from acceptance.paths import DEFAULT_APP_LOG, LOG_DIR, ensure_dirs, session_log_path

CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "{extra[session_id]} | {extra[check_id]} | {extra[module]} | {extra[event]} | {message}"
)

FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | session={extra[session_id]} | "
    "check={extra[check_id]} | module={extra[module]} | event={extra[event]} | {message}"
)

ROTATION = "10 MB"
RETENTION = 10

EVENT_APP = "app"
EVENT_HTTP_REQUEST = "http_request"
EVENT_HTTP_RESPONSE = "http_response"
EVENT_HTTP_ERROR = "http_error"

DEFAULT_EXTRA: dict[str, Any] = {
    "session_id": "-",
    "check_id": "-",
    "module": "app",
    "event": EVENT_APP,
    "payload": {},
}


@dataclass(frozen=True)
class LoggingArtifacts:
    """Пути и уровень журналирования (для показа оператору и в отчёте)."""

    level: str
    app_log: Path
    session_log: Path


def jsonable(value: Any) -> Any:
    """Приводит значение к JSON-сериализуемому виду (для структурного журнала)."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)


def setup_logging(
    *,
    level: str = DEFAULT_LOG_LEVEL,
    session_id: str | None = None,
    app_log: Path | None = None,
    session_log: Path | None = None,
    console: bool = True,
    enqueue: bool = True,
) -> LoggingArtifacts:
    """(Пере)настраивает журналирование пульта.

    Args:
        level: уровень записи (`DEBUG`…`ERROR`) — задаётся оператором.
        session_id: сессия испытаний; для неё создаётся отдельный JSONL-журнал.
        app_log: путь текстового журнала (по умолчанию `logs/app.log`).
        session_log: путь структурного журнала (по умолчанию `logs/session_<id>.jsonl`).
        console: дублировать ли записи в консоль.
        enqueue: асинхронная запись в файлы (в тестах выключается для синхронности).

    Returns:
        Фактические пути журналов и уровень.
    """
    ensure_dirs()
    resolved_app_log = Path(app_log) if app_log is not None else DEFAULT_APP_LOG
    resolved_session_log = (
        Path(session_log)
        if session_log is not None
        else session_log_path(session_id or "unassigned")
    )

    logger.remove()
    logger.configure(extra=dict(DEFAULT_EXTRA))

    if console:
        logger.add(
            sys.stderr,
            level=level,
            format=CONSOLE_FORMAT,
            colorize=True,
            backtrace=False,
            diagnose=False,
            enqueue=False,
        )

    logger.add(
        resolved_app_log,
        level=level,
        format=FILE_FORMAT,
        rotation=ROTATION,
        retention=RETENTION,
        encoding="utf-8",
        enqueue=enqueue,
        backtrace=False,
        diagnose=False,
    )

    logger.add(
        resolved_session_log,
        level=level,
        serialize=True,
        encoding="utf-8",
        enqueue=enqueue,
        backtrace=False,
        diagnose=False,
    )

    return LoggingArtifacts(level=level, app_log=resolved_app_log, session_log=resolved_session_log)


def log_event(
    event: str,
    message: str = "",
    *,
    level: str = "INFO",
    module: str | None = None,
    check_id: str | None = None,
    payload: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Записывает структурированное событие в журнал.

    Args:
        event: тип события (`session_started`, `check_passed`, `api_note`, …).
        message: человекочитаемый текст (по умолчанию — сам `event`).
        level: уровень записи.
        module: модуль/экран пульта.
        check_id: проверка чек-листа, если событие относится к проверке.
        payload: структурированные данные события.
        **fields: дополнительные поля, добавляемые в `payload`.
    """
    data: dict[str, Any] = {}
    if payload:
        data.update(payload)
    if fields:
        data.update(fields)

    bound = logger.bind(event=event, payload=jsonable(data) or {})
    if module:
        bound = bound.bind(module=module)
    if check_id:
        bound = bound.bind(check_id=check_id)

    bound.log(level, message or event)


@contextmanager
def session_context(session_id: str) -> Iterator[None]:
    """Помечает все записи внутри блока идентификатором сессии."""
    with logger.contextualize(session_id=session_id):
        yield


@contextmanager
def check_context(check_id: str) -> Iterator[None]:
    """Помечает все записи внутри блока идентификатором проверки чек-листа."""
    with logger.contextualize(check_id=check_id):
        yield


def tail_log(path: Path, *, lines: int = 200) -> list[str]:
    """Возвращает последние `lines` строк текстового файла (для экрана «Журнал»)."""
    if not path.exists():
        return []

    collected: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            collected.append(line.rstrip("\n"))
            if len(collected) > lines:
                collected.pop(0)
    return collected


def list_session_logs(directory: Path | None = None) -> list[Path]:
    """Файлы структурных журналов сессий (новые — первыми)."""
    base = Path(directory) if directory is not None else LOG_DIR
    if not base.exists():
        return []
    return sorted(
        base.glob("session_*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
