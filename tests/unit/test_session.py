"""Тесты сессии испытаний (FR-T5): поля, жизненный цикл, персистентность."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acceptance.checks.registry import CheckResult, CheckStatus
from acceptance.notes import new_note
from acceptance.session import (
    ENTITY_CREATED,
    ENTITY_DELETED,
    ORIGIN_EXTERNAL,
    ORIGIN_PULT,
    SCHEMA_VERSION,
    SNAP_END,
    SNAP_START,
    STATUS_CLOSED,
    STATUS_DRAFT,
    STATUS_RUNNING,
    TEST_ENTITIES_KEY,
    SessionInfo,
    add_artifact,
    add_console_call,
    add_note_to_session,
    add_task,
    add_test_entity,
    close_session,
    collect_tool_version,
    console_calls_by_label,
    console_calls_summary,
    console_task_ids,
    delete_session,
    find_task,
    list_sessions,
    load_session,
    load_session_from_text,
    mark_test_entity_deleted,
    new_session,
    pending_test_entities,
    register_task_from_console,
    reopen_session,
    save_session,
    session_json,
    session_meta,
    set_markup_stats,
    set_snapshot,
    set_task_check,
    start_session,
    task_history,
    tasks_summary,
    update_task,
)
from acceptance.session import (
    TestSession as SessionModel,
)
from acceptance.session import (
    test_entities as session_test_entities,
)


def _session(directory: Path, **overrides):
    """Черновик сессии с заполненным обязательным ядром."""
    session = new_session(
        base_url="http://test.local",
        server_version={"branch": "dev", "revision": "58ac72f12345"},
        tool_version={"tool_version": "0.1.0", "git_commit": "deadbeef"},
        logs={"session_log": "logs/session_test.jsonl"},
        **overrides,
    )
    session.info.title = "Испытания сервера обучения (смоук)"
    session.info.object_of_test = "dev@58ac72f12345"
    session.info.operator_fio = "Иванов И.И."
    return session


def test_new_session_is_draft_with_history():
    session = new_session(base_url="http://test.local")

    assert session.status == STATUS_DRAFT
    assert session.is_open
    assert session.started_at is None
    assert session.history[0]["event"] == "session_created"


def test_start_and_close_session_fix_time_and_conclusion():
    session = _session(Path("."))

    start_session(session)
    assert session.status == STATUS_RUNNING
    assert session.started_at is not None

    close_session(session, conclusion="годен с замечаниями")
    assert session.status == STATUS_CLOSED
    assert session.ended_at is not None
    assert not session.is_open
    assert session.info.conclusion == "годен с замечаниями"
    assert session.duration_seconds is not None

    reopen_session(session)
    assert session.status == STATUS_RUNNING
    assert session.ended_at is None


def test_save_and_load_round_trip(tmp_path: Path):
    session = _session(tmp_path)
    session.checks.append(
        CheckResult(check_id="TC-SYS-01", status=CheckStatus.PASSED, verdict="ок").to_dict()
    )
    session.info.commission = "Комиссия: Петров П.П."
    set_snapshot(session, SNAP_START, {"counts": {"files": 265}})
    save_session(session, tmp_path)

    restored = load_session(session.session_id, tmp_path)

    assert restored.session_id == session.session_id
    assert restored.info.commission == "Комиссия: Петров П.П."
    assert restored.snapshots[SNAP_START]["counts"]["files"] == 265
    assert restored.checks[0]["status"] == str(CheckStatus.PASSED)
    assert restored.server_build == "dev@58ac72f12345"


def test_session_json_text_round_trip():
    session = _session(Path("."))

    restored = load_session_from_text(session_json(session))

    assert restored.session_id == session.session_id
    assert restored.info.title == session.info.title
    assert restored.logs["session_log"].endswith("session_test.jsonl")


def test_add_note_to_session_deduplicates_by_title():
    session = _session(Path("."))
    note = new_note("Дефект: нет состава датасета и агрегатов", module="Datasets", priority="P1")

    add_note_to_session(session, note)
    add_note_to_session(
        session,
        new_note("Дефект: нет состава датасета и агрегатов", module="Datasets", priority="P1"),
    )

    assert len(session.notes) == 1
    assert any(item["event"] == "api_note" for item in session.history)


def test_snapshots_and_diff_data_are_stored():
    session = _session(Path("."))

    set_snapshot(session, SNAP_START, {"counts": {"files": 265}})
    set_snapshot(session, SNAP_END, {"counts": {"files": 267}})

    assert session.snapshots[SNAP_END]["counts"]["files"] == 267
    assert all("at" in session.snapshots[phase] for phase in (SNAP_START, SNAP_END))


def test_list_sessions_returns_meta_and_skips_broken_files(tmp_path: Path):
    session = _session(tmp_path)
    save_session(session, tmp_path)
    (tmp_path / "broken.json").write_text("{ это не JSON", encoding="utf-8")

    metas = list_sessions(tmp_path)

    assert [meta["session_id"] for meta in metas] == [session.session_id]
    assert metas[0]["title"] == "Испытания сервера обучения (смоук)"
    assert metas[0]["server_build"] == "dev@58ac72f12345"


def test_delete_session_removes_file(tmp_path: Path):
    session = _session(tmp_path)
    save_session(session, tmp_path)

    assert delete_session(session.session_id, tmp_path) is True
    assert delete_session(session.session_id, tmp_path) is False


def test_session_meta_contains_report_fields():
    session = _session(Path("."))
    start_session(session)
    close_session(session, conclusion="годен")

    meta = session_meta(session)

    assert meta["conclusion"] == "годен"
    assert meta["started_at"] and meta["ended_at"]
    assert meta["base_url"] == "http://test.local"
    assert json.dumps(meta, ensure_ascii=False)


def test_tool_version_contains_environment_info():
    version = collect_tool_version()

    for key in ("tool", "tool_version", "git_commit", "python", "platform", "root", "collected_at"):
        assert key in version


def test_session_info_ignores_unknown_keys():
    info = SessionInfo.from_dict({"title": "Наименование", "unknown": "x"})

    assert info.title == "Наименование"
    assert not hasattr(info, "unknown")
    assert not info.is_filled


# ---------------------------------------------------------------------------
# Схема v2: артефакты и характеристики разметки (этап T1)
# ---------------------------------------------------------------------------
def test_artifacts_and_markup_stats_round_trip(tmp_path: Path):
    session = _session(tmp_path)
    artifact = tmp_path / "records_manifest_20260915-101500.csv"
    artifact.write_text("row_kind,record_id\nзапись,abc\n", encoding="utf-8-sig")

    add_artifact(
        session,
        kind="records_manifest (CSV)",
        path=artifact,
        note="объединение в записи",
    )
    set_markup_stats(
        session,
        "22222222-2222-4222-8222-222222222222",
        records=64,
        chunks=8,
        status="рассчитано (клиентская оценка)",
        note="получено 1.2 КБ",
    )
    save_session(session, tmp_path)
    restored = load_session(session.session_id, tmp_path)

    assert restored.artifacts[0]["name"] == artifact.name
    assert restored.artifacts[0]["size_bytes"] == artifact.stat().st_size
    assert restored.artifacts[0]["kind"] == "records_manifest (CSV)"
    assert restored.markup_stats["22222222-2222-4222-8222-222222222222"]["records"] == 64
    assert restored.markup_stats["22222222-2222-4222-8222-222222222222"]["chunks"] == 8


def test_add_artifact_and_set_markup_stats_write_history():
    session = _session(Path("."))

    add_artifact(session, kind="выгрузка", path=Path("artifacts/pair.zip"), size_bytes=2048)
    set_markup_stats(session, "file-1", records=10, chunks=2, status="рассчитано", note="—")

    events = [item["event"] for item in session.history]

    assert "artifact_saved" in events
    assert "markup_stats" in events
    assert session.artifacts[0]["path"] == "artifacts/pair.zip"


def test_schema_v1_file_is_read_with_defaults():
    """Файл сессии, записанный до этапа T1 (schema v1), читается без ошибок."""
    payload = _session(Path(".")).to_dict()
    payload["schema_version"] = 1
    payload.pop("artifacts")
    payload.pop("markup_stats")

    restored = SessionModel.from_dict(payload)

    assert restored.schema_version == 1
    assert restored.artifacts == []
    assert restored.markup_stats == {}


def test_current_schema_version_is_five():
    assert SCHEMA_VERSION == 5
    assert _session(Path(".")).to_dict()["schema_version"] == 5


# ---------------------------------------------------------------------------
# Схема v3: ручные вызовы консоли запросов (этап T2)
# ---------------------------------------------------------------------------
def test_console_calls_round_trip_and_history(tmp_path: Path):
    """Вызовы консоли сохраняются в сессии вместе с меткой и номером журнала."""
    session = _session(tmp_path)
    add_console_call(
        session,
        label="TC-FILE-01",
        method="get",
        path="/api/data/files",
        status=200,
        duration_ms=12.34,
        journal_seq=7,
        operation="get /api/data/files",
        safety="read",
        note="сверка количества файлов",
    )
    add_console_call(
        session,
        label="TC-TR-01",
        method="post",
        path="/api/ml_models/models/m-1/train",
        status=202,
        duration_ms=120.0,
        journal_seq=8,
        operation="post /api/ml_models/models/{model_id}/train",
        safety="heavy",
        task_id="task-77",
        note="карточка запуска: цель «проверить обучение»",
    )
    add_console_call(
        session,
        label="TC-TASK-01",
        method="GET",
        path="/api/tasks/2f1b",
        duration_ms=5.0,
        journal_seq=9,
        operation="get /api/tasks/{task_id}",
        safety="read",
        error="[404] задача не найдена",
    )
    save_session(session, tmp_path)
    restored = load_session(session.session_id, tmp_path)

    first, second, third = restored.console_calls
    assert first["method"] == "GET"  # регистр приводится к верхнему
    assert first["status"] == 200
    assert first["duration_ms"] == 12.3
    assert first["journal_seq"] == 7
    assert first["label"] == "TC-FILE-01"
    assert second["task_id"] == "task-77"
    assert third["status"] is None
    assert "404" in third["error"]

    events = [item["event"] for item in restored.history]
    assert events.count("console_call") == 3
    assert "console_task_started" in events


def test_console_calls_grouping_and_summary():
    """Сводка вызовов консоли: по метке, классам статусов и операциям."""
    session = _session(Path("."))
    for label, status in (("TC-SYS-01", 200), ("TC-SYS-01", 404), ("TC-FILE-02", 201)):
        add_console_call(
            session,
            label=label,
            method="GET",
            path="/health",
            status=status,
            operation="get /health",
            safety="read",
            error="" if status < 400 else "нет",
        )

    grouped = console_calls_by_label(session)
    summary = console_calls_summary(session)

    assert sorted(grouped) == ["TC-FILE-02", "TC-SYS-01"]
    assert len(grouped["TC-SYS-01"]) == 2
    assert summary["total"] == 3
    assert summary["errors"] == 1
    assert summary["labels"] == 2
    assert summary["tasks"] == 0
    assert summary["by_status_class"] == {"2xx": 2, "4xx": 1}
    assert summary["by_operation"] == {"get /health": 3}


def test_schema_v2_file_is_read_with_defaults():
    """Файл сессии этапа T1 (schema v2) читается без ошибок."""
    payload = _session(Path(".")).to_dict()
    payload["schema_version"] = 2
    payload.pop("console_calls", None)

    restored = SessionModel.from_dict(payload)

    assert restored.schema_version == 2
    assert restored.console_calls == []


# ---------------------------------------------------------------------------
# Схема v4: наблюдаемые задачи — монитор задач (этап T4)
# ---------------------------------------------------------------------------
def test_task_registration_updates_and_round_trips(tmp_path: Path):
    """Задача ставится на наблюдение, переходы FSM-1 попадают в историю и в файл."""
    session = _session(tmp_path)
    add_task(
        session,
        task_id="task-1",
        task_type="celery-test",
        name="probe",
        check_id="TC-TASK-04",
        journal_seq=3,
    )
    update_task(session, "task-1", status="running", polled=True, journal_seq=4)
    update_task(session, "task-1", status="paused", polled=True, journal_seq=6)
    update_task(session, "task-1", error="ReadTimeout: сервер недоступен")
    save_session(session, tmp_path)
    restored = load_session(session.session_id, tmp_path)

    task = find_task(restored, "task-1")
    assert task is not None
    assert task["origin"] == ORIGIN_PULT
    assert task["status"] == "paused"
    assert task["check_id"] == "TC-TASK-04"
    assert task["journal_from"] == 4  # первая запись журнала наблюдения за задачей
    assert task["journal_seq"] == 6
    assert task["poll"]["polls"] == 2
    assert task["poll"]["last_poll"]
    assert task["errors"][-1]["error"].startswith("ReadTimeout")

    history = task_history(restored, "task-1")
    assert [(item["previous"], item["status"]) for item in history[:3]] == [
        (None, ""),
        ("", "running"),
        ("running", "paused"),
    ]
    events = [item["event"] for item in restored.history]
    assert "task_observed" in events
    assert events.count("task_status_changed") == 2
    assert "task_poll_error" in events


def test_task_registration_is_idempotent():
    """Повторная постановка на наблюдение обновляет запись, а не дублирует её."""
    session = _session(Path("."))
    add_task(session, task_id="task-2", task_type="training")
    add_task(session, task_id="task-2", name="learning", check_id="TC-TR-01")

    assert len(session.tasks) == 1
    task = find_task(session, "task-2")
    assert task is not None
    assert task["name"] == "learning"
    assert task["check_id"] == "TC-TR-01"
    assert task["type"] == "training"


def test_add_task_requires_identifier():
    """Пустой `task_id` — ошибка вызывающего кода, а не «задача без имени»."""
    with pytest.raises(ValueError):
        add_task(_session(Path(".")), task_id="  ")


def test_task_statuses_and_check_attachment():
    """Привязка задачи к проверке и смена поллинга фиксируются в истории сессии."""
    session = _session(Path("."))
    add_task(session, task_id="task-3", origin=ORIGIN_EXTERNAL)
    set_task_check(session, "task-3", "TC-TASK-08")
    update_task(session, "task-3", active=False, interval=5.0)

    task = find_task(session, "task-3")
    assert task is not None
    assert task["origin"] == ORIGIN_EXTERNAL
    assert task["check_id"] == "TC-TASK-08"
    assert task["poll"]["active"] is False
    assert task["poll"]["interval"] == 5.0
    assert [item["event"] for item in session.history].count("task_attached") == 1


def test_tasks_summary_counts_statuses_and_origins():
    """KPI монитора: наблюдаемых, активных, завершённых, прерванных, внешних."""
    session = _session(Path("."))
    add_task(session, task_id="a", task_type="celery-test")
    update_task(session, "a", status="running")
    add_task(session, task_id="b", task_type="training")
    update_task(session, "b", status="completed")
    add_task(session, task_id="c", origin=ORIGIN_EXTERNAL)
    update_task(session, "c", status="not_found")
    update_task(session, "c", active=False)

    summary = tasks_summary(session)

    assert summary["total"] == 3
    assert summary["active"] == 1
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["external"] == 1
    assert summary["watching"] == 2
    assert summary["by_origin"][ORIGIN_EXTERNAL] == 1
    assert summary["by_status"]["running"] == 1


def test_console_task_ids_and_registration_from_console():
    """Задачи из ответов консоли подхватываются монитором (FR-T3 → T4)."""
    session = _session(Path("."))
    for task_id, label in (("task-10", "TC-TR-01"), ("task-10", "TC-TR-01"), ("", "TC-DS-05")):
        add_console_call(
            session,
            label=label,
            method="POST",
            path="/api/ml_models/models/m-1/train",
            status=202,
            journal_seq=5,
            operation="post /api/ml_models/models/{model_id}/train",
            safety="heavy",
            task_id=task_id,
        )

    found = console_task_ids(session)

    assert [item["task_id"] for item in found] == ["task-10"]
    assert found[0]["label"] == "TC-TR-01"

    register_task_from_console(
        session,
        task_id="task-11",
        label="TC-DS-05",
        operation="post /api/datasets/fill/{dataset_id}",
    )
    task = find_task(session, "task-11")
    assert task is not None
    assert task["origin"] == ORIGIN_PULT
    assert task["check_id"] == "TC-DS-05"
    assert "консоли" in task["note"]


def test_schema_v3_file_is_read_with_defaults():
    """Файл сессии этапа T2 (schema v3) читается без ошибок, задачи пусты."""
    payload = _session(Path(".")).to_dict()
    payload["schema_version"] = 3
    payload.pop("tasks", None)

    restored = SessionModel.from_dict(payload)

    assert restored.schema_version == 3
    assert restored.tasks == []
    assert session_meta(restored)["tasks_total"] == 0


def test_test_entities_are_tracked_for_cleanup():
    """Учёт `__TEST__`-сущностей (NFR-T4): создание, удаление, «не удалено» и схема v4."""
    session = _session(Path("."))
    created = add_test_entity(
        session, entity_id="__TEST__file-1", entity_type="файл RAW", check_id="TC-FILE-12"
    )

    assert created["action"] == ENTITY_CREATED
    assert created["id"] == "__TEST__file-1"
    assert created["at"]
    assert session.counters[TEST_ENTITIES_KEY] == [created]
    assert [item["action"] for item in session_test_entities(session)] == [ENTITY_CREATED]
    assert [item["id"] for item in pending_test_entities(session)] == ["__TEST__file-1"]

    deleted = mark_test_entity_deleted(
        session, "__TEST__file-1", check_id="TC-FILE-13", note="самоочистка"
    )

    assert deleted is not None
    assert deleted["action"] == ENTITY_DELETED
    assert deleted["check_id"] == "TC-FILE-13"
    assert deleted["type"] == "файл RAW"  # тип берётся из записи о создании
    assert pending_test_entities(session) == []
    assert mark_test_entity_deleted(session, "__TEST__unknown") is None

    events = [item["event"] for item in session.history]
    assert "test_entity_created" in events
    assert "test_entity_deleted" in events

    restored = load_session_from_text(session_json(session))
    assert restored.schema_version == SCHEMA_VERSION
    assert [item["id"] for item in session_test_entities(restored)] == [
        "__TEST__file-1",
        "__TEST__file-1",
    ]
    assert pending_test_entities(restored) == []
