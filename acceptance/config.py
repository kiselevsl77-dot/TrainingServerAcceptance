"""Настройки пульта испытаний (переменные окружения `.env`).

Дополнительно к настройкам подключения (`client.settings`) пульт читает
параметры журналирования и поллинга:

    PULT_LOG_LEVEL      — DEBUG | INFO | WARNING | ERROR (по умолчанию INFO);
    PULT_LOG_BODIES     — сохранять тела HTTP-запросов/ответов (по умолчанию true);
    PULT_BODY_LIMIT     — лимит сохраняемого тела, символов (по умолчанию 4096);
    PULT_JOURNAL_MAX    — максимум записей журнала HTTP в памяти (по умолчанию 5000);
    PULT_POLL_INTERVAL  — интервал поллинга задач, секунды (по умолчанию 2.0);
    PULT_LOG_ENQUEUE    — асинхронная запись журналов (по умолчанию **false**: журнал
                          испытаний — доказательство, он не должен теряться при
                          аварийном завершении процесса).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

LOG_LEVEL_ENV = "PULT_LOG_LEVEL"
LOG_BODIES_ENV = "PULT_LOG_BODIES"
BODY_LIMIT_ENV = "PULT_BODY_LIMIT"
JOURNAL_MAX_ENV = "PULT_JOURNAL_MAX"
POLL_INTERVAL_ENV = "PULT_POLL_INTERVAL"
LOG_ENQUEUE_ENV = "PULT_LOG_ENQUEUE"

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_BODIES = True
DEFAULT_BODY_LIMIT = 4096
DEFAULT_JOURNAL_MAX = 5000
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_LOG_ENQUEUE = False

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class PultConfig:
    """Настройки пульта испытаний."""

    log_level: str = DEFAULT_LOG_LEVEL
    log_bodies: bool = DEFAULT_LOG_BODIES
    body_limit: int = DEFAULT_BODY_LIMIT
    journal_max: int = DEFAULT_JOURNAL_MAX
    poll_interval: float = DEFAULT_POLL_INTERVAL
    log_enqueue: bool = DEFAULT_LOG_ENQUEUE


def _read_bool(env_name: str, default: bool) -> bool:
    raw = (os.getenv(env_name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _read_int(env_name: str, default: int) -> int:
    raw = (os.getenv(env_name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _read_float(env_name: str, default: float) -> float:
    raw = (os.getenv(env_name) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def load_config() -> PultConfig:
    """Читает настройки пульта из окружения (с учётом `.env`)."""
    load_dotenv()

    level = (os.getenv(LOG_LEVEL_ENV) or "").strip().upper()
    return PultConfig(
        log_level=level if level in LOG_LEVELS else DEFAULT_LOG_LEVEL,
        log_bodies=_read_bool(LOG_BODIES_ENV, DEFAULT_LOG_BODIES),
        body_limit=_read_int(BODY_LIMIT_ENV, DEFAULT_BODY_LIMIT),
        journal_max=_read_int(JOURNAL_MAX_ENV, DEFAULT_JOURNAL_MAX),
        poll_interval=_read_float(POLL_INTERVAL_ENV, DEFAULT_POLL_INTERVAL),
        log_enqueue=_read_bool(LOG_ENQUEUE_ENV, DEFAULT_LOG_ENQUEUE),
    )
