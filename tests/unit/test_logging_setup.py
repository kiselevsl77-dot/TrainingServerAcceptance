"""Тесты журналирования пульта (FR-T6): файлы, структура записей, контекст.

Проверяется, что:
    * создаются оба файла — текстовый журнал и структурный JSONL;
    * событие содержит `session_id`, `check_id`, `module`, `event` и `payload`;
    * контексты сессии и проверки подставляются во все записи внутри блока;
    * уровень журналирования фильтрует записи.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acceptance.logging_setup import (
    check_context,
    jsonable,
    list_session_logs,
    log_event,
    session_context,
    setup_logging,
    tail_log,
)


@pytest.fixture
def artifacts(tmp_path: Path):
    """Журналирование, настроенное на временные файлы (синхронная запись)."""
    return setup_logging(
        level="DEBUG",
        session_id="test-session",
        app_log=tmp_path / "app.log",
        session_log=tmp_path / "session_test.jsonl",
        console=False,
        enqueue=False,
    )


def _records(path: Path) -> list[dict]:
    """Разбирает структурный JSONL-журнал в список записей."""
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_setup_logging_creates_both_files(artifacts):
    log_event("unit_test", "Сообщение", module="tests", payload={"value": 1})

    assert artifacts.app_log.exists()
    assert artifacts.session_log.exists()
    assert "event=unit_test" in artifacts.app_log.read_text(encoding="utf-8")


def test_log_event_writes_structured_payload(artifacts):
    log_event(
        "check_finished",
        "Проверка завершена",
        module="checks",
        check_id="TC-FILE-01",
        payload={"status": "успех", "duration_ms": 12.5},
    )

    record = _records(artifacts.session_log)[-1]["record"]
    extra = record["extra"]
    assert extra["event"] == "check_finished"
    assert extra["module"] == "checks"
    assert extra["check_id"] == "TC-FILE-01"
    assert extra["payload"] == {"status": "успех", "duration_ms": 12.5}


def test_session_and_check_context_are_applied(artifacts):
    with session_context("20260915-101500-abcd"), check_context("TC-SYS-01"):
        log_event("inside", "Внутри контекста")

    extra = _records(artifacts.session_log)[-1]["record"]["extra"]
    assert extra["session_id"] == "20260915-101500-abcd"
    assert extra["check_id"] == "TC-SYS-01"


def test_level_filters_lower_priority_records(tmp_path: Path):
    artifacts = setup_logging(
        level="WARNING",
        session_id="filtered",
        app_log=tmp_path / "app.log",
        session_log=tmp_path / "session.jsonl",
        console=False,
        enqueue=False,
    )

    log_event("debug_event", "Отладка", level="DEBUG")
    log_event("warning_event", "Предупреждение", level="WARNING")

    events = [item["record"]["extra"]["event"] for item in _records(artifacts.session_log)]
    assert events == ["warning_event"]


def test_jsonable_converts_non_json_values(tmp_path: Path):
    assert jsonable({"path": tmp_path, "items": (1, 2)}) == {
        "path": tmp_path.as_posix(),
        "items": [1, 2],
    }


def test_tail_log_returns_last_lines(artifacts):
    for index in range(5):
        log_event("line", f"строка {index}", module="tests")

    lines = tail_log(artifacts.app_log, lines=2)
    assert len(lines) == 2
    assert "строка 4" in lines[-1]


def test_list_session_logs_finds_created_files(artifacts):
    assert artifacts.session_log in list_session_logs(artifacts.session_log.parent)
