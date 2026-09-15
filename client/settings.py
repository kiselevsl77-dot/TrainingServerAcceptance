"""Конфигурация подключения к серверу обучения.

В OpenAPI-спецификации `SOM1.json` отсутствует блок `servers`, поэтому base URL
задаётся вручную через переменные окружения (файл `.env`).

Переменные:
    TRAINING_SERVER_BASE_URL          — базовый URL внешнего сервиса (обязательно).
    TRAINING_SERVER_TIMEOUT           — таймаут обычных запросов, секунды (по умолчанию 15).
    TRAINING_SERVER_DOWNLOAD_TIMEOUT  — таймаут скачивания файлов, секунды (по умолчанию 600).
    TRAINING_SERVER_DOWNLOAD_LIMIT_MB — порог размера файла для скачивания, МБ (по умолчанию 200).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

BASE_URL_ENV = "TRAINING_SERVER_BASE_URL"
TIMEOUT_ENV = "TRAINING_SERVER_TIMEOUT"
DOWNLOAD_TIMEOUT_ENV = "TRAINING_SERVER_DOWNLOAD_TIMEOUT"
DOWNLOAD_LIMIT_MB_ENV = "TRAINING_SERVER_DOWNLOAD_LIMIT_MB"

DEFAULT_TIMEOUT = 15.0
DEFAULT_DOWNLOAD_TIMEOUT = 600.0
DEFAULT_DOWNLOAD_LIMIT_MB = 200

BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class TrainingServerSettings:
    """Параметры подключения к серверу обучения."""

    base_url: str
    timeout: float = DEFAULT_TIMEOUT
    download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT
    download_limit_mb: int = DEFAULT_DOWNLOAD_LIMIT_MB

    @property
    def is_configured(self) -> bool:
        """True, если задан непустой base URL."""
        return bool(self.base_url.strip())

    @property
    def download_limit_bytes(self) -> int:
        """Порог размера файла (байты), выше которого скачивание требует подтверждения."""
        return self.download_limit_mb * BYTES_PER_MB


def _read_float(env_name: str, default: float) -> float:
    raw = (os.getenv(env_name) or "").strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _read_int(env_name: str, default: int) -> int:
    raw = (os.getenv(env_name) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def load_settings() -> TrainingServerSettings:
    """Читает настройки из переменных окружения (с учётом `.env`)."""
    load_dotenv()

    return TrainingServerSettings(
        base_url=(os.getenv(BASE_URL_ENV) or "").strip(),
        timeout=_read_float(TIMEOUT_ENV, DEFAULT_TIMEOUT),
        download_timeout=_read_float(DOWNLOAD_TIMEOUT_ENV, DEFAULT_DOWNLOAD_TIMEOUT),
        download_limit_mb=_read_int(DOWNLOAD_LIMIT_MB_ENV, DEFAULT_DOWNLOAD_LIMIT_MB),
    )


@lru_cache(maxsize=1)
def get_settings() -> TrainingServerSettings:
    """Возвращает настройки с кэшированием (значения меняются редко)."""
    return load_settings()
