"""Тесты ручных решений оператора по записям (FR-T2, этап T1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from acceptance.overrides import (
    ACTION_CURRENT,
    ACTION_EXCLUDE,
    ACTION_LINK,
    ACTION_UNLINK,
    OVERRIDES_FILENAME,
    Overrides,
    RecordOverride,
    load_overrides,
    overrides_path,
    save_overrides,
)


def _entry(
    raw_id: str, at: str, action: str = ACTION_LINK, base: str = "Antminer_S19"
) -> RecordOverride:
    """Решение оператора с заданным временем (для проверки порядка применения)."""
    return RecordOverride(
        base_name=base,
        raw_id=raw_id,
        action=action,
        markup_id="markup-1" if action == ACTION_LINK else None,
        comment=f"решение {at}",
        operator="Иванов И.И.",
        session_id="20260915-101500-1a2b",
        at=at,
    )


def test_add_requires_known_action():
    overrides = Overrides()

    with pytest.raises(ValueError):
        overrides.add(action="linkk", base_name="A", raw_id="1")

    entry = overrides.add(action=ACTION_LINK, base_name="A", raw_id="1", markup_id="m1")
    assert entry.action_label == "привязка markup к RAW"
    assert entry.markup_id == "m1"


def test_add_normalizes_fields_and_defaults_audit():
    overrides = Overrides()

    entry = overrides.add(
        action=ACTION_EXCLUDE,
        base_name="A",
        raw_id="1",
        comment="  не использовать  ",
        operator="  Петров П.П. ",
    )

    assert entry.comment == "не использовать"
    assert entry.operator == "Петров П.П."
    assert entry.at


def test_last_by_raw_keeps_latest_decision():
    overrides = Overrides(
        entries=[
            _entry("raw-1", "2026-09-15T10:00:00", ACTION_UNLINK),
            _entry("raw-1", "2026-09-15T11:00:00", ACTION_LINK),
            _entry("raw-2", "2026-09-15T09:00:00", ACTION_EXCLUDE),
        ]
    )

    last = overrides.last_by_raw

    assert set(last) == {"raw-1", "raw-2"}
    assert last["raw-1"].action == ACTION_LINK
    assert last["raw-2"].action == ACTION_EXCLUDE


def test_for_base_filters_and_sorts_by_time():
    overrides = Overrides(
        entries=[
            _entry("raw-2", "2026-09-15T11:00:00"),
            _entry("raw-1", "2026-09-15T10:00:00"),
            _entry("raw-3", "2026-09-15T12:00:00", base="Other_Unit"),
        ]
    )

    entries = overrides.for_base("Antminer_S19")

    assert [entry.raw_id for entry in entries] == ["raw-1", "raw-2"]


def test_history_is_sorted_and_serializable():
    overrides = Overrides(
        entries=[_entry("raw-2", "2026-09-15T11:00:00"), _entry("raw-1", "2026-09-15T10:00:00")]
    )

    history = overrides.history()

    assert [item["raw_id"] for item in history] == ["raw-1", "raw-2"]
    assert all(isinstance(item["comment"], str) for item in history)


def test_remove_for_raw_removes_all_decisions():
    overrides = Overrides(
        entries=[
            _entry("raw-1", "2026-09-15T10:00:00", ACTION_UNLINK),
            _entry("raw-1", "2026-09-15T11:00:00", ACTION_EXCLUDE),
            _entry("raw-2", "2026-09-15T12:00:00"),
        ]
    )

    removed = overrides.remove_for_raw("raw-1")

    assert removed == 2
    assert [entry.raw_id for entry in overrides.entries] == ["raw-2"]


def test_dict_round_trip_and_unknown_keys():
    overrides = Overrides(entries=[_entry("raw-1", "2026-09-15T10:00:00", ACTION_CURRENT)])

    restored = Overrides.from_dict(overrides.to_dict() | {"unknown": "x"})
    restored_entry = RecordOverride.from_dict(
        {"base_name": "A", "raw_id": "1", "action": ACTION_LINK, "extra": 1}
    )

    assert restored.entries[0].action == ACTION_CURRENT
    assert restored.schema_version == overrides.schema_version
    assert not hasattr(restored_entry, "extra")


def test_save_and_load_round_trip(tmp_path: Path):
    overrides = Overrides()
    overrides.add(
        action=ACTION_LINK,
        base_name="Antminer_S19",
        raw_id="raw-1",
        markup_id="markup-2",
        comment="Дубль: разметка от второй версии",
        operator="Иванов И.И.",
        session_id="20260915-101500-1a2b",
    )

    path = save_overrides(overrides, tmp_path)
    restored = load_overrides(tmp_path)

    assert path == overrides_path(tmp_path) == tmp_path / OVERRIDES_FILENAME
    assert restored.entries[0].markup_id == "markup-2"
    assert restored.entries[0].session_id == "20260915-101500-1a2b"


def test_load_missing_or_broken_file_returns_empty(tmp_path: Path):
    assert load_overrides(tmp_path).entries == []

    (tmp_path / OVERRIDES_FILENAME).write_text("{ это не JSON", encoding="utf-8")
    assert load_overrides(tmp_path).entries == []

    (tmp_path / OVERRIDES_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
    assert load_overrides(tmp_path).entries == []
