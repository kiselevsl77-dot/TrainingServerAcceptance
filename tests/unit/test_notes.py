"""Тесты реестра замечаний к API и перспективных требований (FR-T7)."""

from __future__ import annotations

from acceptance.notes import (
    KNOWN_DEFECTS,
    PRIORITIES,
    PROSPECTIVE_REQUIREMENTS,
    ApiNote,
    has_note,
    known_defect_notes,
    new_note,
    notes_by_priority,
    prospective_requirements,
    sorted_notes,
)


def test_new_note_normalizes_priority_and_ids():
    note = new_note("Дефект", priority="p0")

    assert note.priority == "P0"
    assert note.note_id and note.created_at
    assert note.source == "оператор"


def test_new_note_falls_back_to_p1_for_unknown_priority():
    assert new_note("Дефект", priority="срочно").priority == "P1"


def test_sorted_notes_orders_by_priority():
    notes = [
        new_note("Третье", priority="P2"),
        new_note("Первое", priority="P0"),
        new_note("Второе", priority="P1"),
    ]

    ordered = [note.title for note in sorted_notes(notes)]
    assert ordered == ["Первое", "Второе", "Третье"]


def test_sorted_notes_accepts_plain_dicts():
    ordered = sorted_notes([{"title": "A", "priority": "P2"}, {"title": "B", "priority": "P0"}])

    assert [note.title for note in ordered] == ["B", "A"]


def test_notes_by_priority_groups_all_priorities():
    grouped = notes_by_priority([new_note("A", priority="P0")])

    assert set(grouped) == set(PRIORITIES)
    assert len(grouped["P0"]) == 1
    assert grouped["P2"] == []


def test_has_note_matches_by_title_prefix():
    notes = [new_note("Скачивание не-ASCII не работает", priority="P0")]

    assert has_note(notes, "скачивание")
    assert not has_note(notes, "Range")


def test_known_defect_notes_are_ready_to_use():
    notes = known_defect_notes()

    assert len(notes) == len(KNOWN_DEFECTS)
    assert len(notes) >= 10
    assert all(note.source == "авто" and note.fact and note.expected for note in notes)
    assert {"P0", "P1", "P2"}.issubset({note.priority for note in notes})
    assert any("phase_connection" in note.title for note in notes)
    assert any("latin-1" in note.title for note in notes)


def test_prospective_requirements_include_subdatasets():
    requirements = prospective_requirements()

    assert len(requirements) == len(PROSPECTIVE_REQUIREMENTS)
    assert "субдатасет" in requirements[0]["title"].lower()
    assert all(item["detail"] and item["module"] for item in requirements)


def test_api_note_dict_round_trip():
    note = new_note("Заголовок", module="Datasets", endpoint="GET /api/datasets/", priority="P1")

    restored = ApiNote.from_dict(note.to_dict())

    assert restored == note


def test_api_note_from_dict_tolerates_missing_fields():
    restored = ApiNote.from_dict({"title": "Только заголовок"})

    assert restored.title == "Только заголовок"
    assert restored.priority == "P1"
    assert restored.created_at
