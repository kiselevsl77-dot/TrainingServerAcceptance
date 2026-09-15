"""Тесты настроек пульта (переменные окружения)."""

from __future__ import annotations

import pytest

from acceptance.config import (
    DEFAULT_BODY_LIMIT,
    DEFAULT_JOURNAL_MAX,
    DEFAULT_LOG_BODIES,
    DEFAULT_LOG_ENQUEUE,
    DEFAULT_LOG_LEVEL,
    DEFAULT_POLL_INTERVAL,
    load_config,
)

ENV_KEYS = (
    "PULT_LOG_LEVEL",
    "PULT_LOG_BODIES",
    "PULT_BODY_LIMIT",
    "PULT_JOURNAL_MAX",
    "PULT_POLL_INTERVAL",
    "PULT_LOG_ENQUEUE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Изолирует тест от значений окружения и от файла `.env` репозитория."""
    monkeypatch.chdir(tmp_path)
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_used_without_env():
    config = load_config()

    assert config.log_level == DEFAULT_LOG_LEVEL
    assert config.log_bodies is DEFAULT_LOG_BODIES
    assert config.body_limit == DEFAULT_BODY_LIMIT
    assert config.journal_max == DEFAULT_JOURNAL_MAX
    assert config.poll_interval == DEFAULT_POLL_INTERVAL
    assert config.log_enqueue is DEFAULT_LOG_ENQUEUE


def test_values_are_read_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PULT_LOG_LEVEL", "debug")
    monkeypatch.setenv("PULT_LOG_BODIES", "false")
    monkeypatch.setenv("PULT_BODY_LIMIT", "128")
    monkeypatch.setenv("PULT_JOURNAL_MAX", "10")
    monkeypatch.setenv("PULT_POLL_INTERVAL", "0.5")
    monkeypatch.setenv("PULT_LOG_ENQUEUE", "true")

    config = load_config()

    assert config.log_level == "DEBUG"
    assert config.log_bodies is False
    assert config.body_limit == 128
    assert config.journal_max == 10
    assert config.poll_interval == 0.5
    assert config.log_enqueue is True


def test_invalid_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PULT_LOG_LEVEL", "TRACE")
    monkeypatch.setenv("PULT_BODY_LIMIT", "abc")
    monkeypatch.setenv("PULT_JOURNAL_MAX", "-5")
    monkeypatch.setenv("PULT_POLL_INTERVAL", "0")

    config = load_config()

    assert config.log_level == DEFAULT_LOG_LEVEL
    assert config.body_limit == DEFAULT_BODY_LIMIT
    assert config.journal_max == DEFAULT_JOURNAL_MAX
    assert config.poll_interval == DEFAULT_POLL_INTERVAL
