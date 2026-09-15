"""Тесты сессии испытаний (FR-T5): поля, жизненный цикл, персистентность."""

from __future__ import annotations

import json
from pathlib import Path

from acceptance.checks.registry import CheckResult, CheckStatus
from acceptance.notes import new_note
from acceptance.session import (
    SCHEMA_VERSION,
    SNAP_END,
    SNAP_START,
    STATUS_CLOSED,
    STATUS_DRAFT,
    STATUS_RUNNING,
    SessionInfo,
    add_artifact,
    add_note_to_session,
    close_session,
    collect_tool_version,
    delete_session,
    list_sessions,
    load_session,
    load_session_from_text,
    new_session,
    reopen_session,
    save_session,
    session_json,
    session_meta,
    set_markup_stats,
    set_snapshot,
    start_session,
)
from acceptance.session import (
    TestSession as SessionModel,
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
    note = new_note("Дефект: нет phase_connection", module="Loads", priority="P0")

    add_note_to_session(session, note)
    add_note_to_session(
        session, new_note("Дефект: нет phase_connection", module="Loads", priority="P0")
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


def test_current_schema_version_is_two():
    assert SCHEMA_VERSION == 2
    assert _session(Path(".")).to_dict()["schema_version"] == 2
