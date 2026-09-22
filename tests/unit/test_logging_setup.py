"""Тесты журналирования пульта (FR-T6): файлы, структура записей, контекст.

Проверяется, что:
    * создаются оба файла — текстовый журнал и структурный JSONL;
    * событие содержит `session_id`, `check_id`, `module`, `event` и `payload`;
    * контексты сессии и проверки подставляются во все записи внутри блока;
    * уровень журналирования фильтрует записи.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from acceptance.logging_setup import (
    RUN_ID,
    check_context,
    jsonable,
    list_app_logs,
    list_session_logs,
    log_event,
    session_context,
    setup_logging,
    tail_log,
)
from acceptance.paths import APP_LOG_DIR, APP_LOG_PATTERN, app_log_path


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


def test_app_log_path_names_file_by_run(tmp_path: Path) -> None:
    """Файл текстового журнала называется по запуску: `applog_<ГГГГММДД-ЧЧММСС>.log`."""
    path = app_log_path("20260921-101500")

    assert path.name == "applog_20260921-101500.log"
    assert path.parent == APP_LOG_DIR
    assert path.match(APP_LOG_PATTERN)


def test_run_id_looks_like_start_timestamp() -> None:
    """Идентификатор запуска — момент старта процесса (`ГГГГММДД-ЧЧММСС`)."""
    assert len(RUN_ID) == len("20260921-101500")
    assert RUN_ID[8] == "-"
    assert RUN_ID.replace("-", "").isdigit()


def test_default_app_log_is_the_run_file() -> None:
    """Журнал по умолчанию — файл текущего запуска, а не общая лента `app.log`."""
    from acceptance.logging_setup import DEFAULT_APP_LOG

    assert DEFAULT_APP_LOG == app_log_path(RUN_ID)
    assert DEFAULT_APP_LOG.parent == APP_LOG_DIR
    assert DEFAULT_APP_LOG.name != "app.log"


def test_list_app_logs_finds_previous_runs(tmp_path: Path) -> None:
    """Журналы прошлых запусков находятся по маске и сортируются «новые первыми»."""
    older = tmp_path / "applog_20260920-090000.log"
    newer = tmp_path / "applog_20260921-101500.log"
    other = tmp_path / "session_20260921-101500.jsonl"
    for path in (older, newer, other):
        path.write_text("строка", encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    found = list_app_logs(tmp_path)

    assert found == [newer, older]
    assert other not in found


def test_list_app_logs_returns_empty_for_missing_directory(tmp_path: Path) -> None:
    """Отсутствие каталога запусков не считается ошибкой (журналов ещё нет)."""
    assert list_app_logs(tmp_path / "нет-такого-каталога") == []
