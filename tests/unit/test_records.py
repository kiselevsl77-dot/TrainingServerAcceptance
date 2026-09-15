"""Тесты объединения файлов реестра в записи (FR-T2, этап T1)."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from acceptance.overrides import (
    ACTION_CURRENT,
    ACTION_EXCLUDE,
    ACTION_LINK,
    ACTION_UNLINK,
    Overrides,
)
from acceptance.records import (
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_MANUAL,
    CONFIDENCE_NONE,
    CONFIDENCE_WEAK,
    MANIFEST_COLUMNS,
    ROW_OTHER,
    ROW_RECORD,
    ROW_UNPAIRED_MARKUP,
    MarkupStatsEntry,
    build_records,
    record_id_for,
    save_manifest,
)
from client.schemas import FileMetadataResponse

BASE_TIME = datetime(2026, 9, 9, 13, 53, 56)


def _file(
    name: str,
    *,
    uid: int,
    size: int = 1000,
    minutes: float = 0.0,
    file_type: str = "RAW",
) -> FileMetadataResponse:
    """Файл реестра с заданным id, размером и временем импорта."""
    return FileMetadataResponse(
        id=UUID(f"{uid:08d}-0000-4000-8000-000000000000"),
        file_name=name,
        size=size,
        s3_path=f"bucket/{name}",
        import_date=BASE_TIME + timedelta(minutes=minutes),
        file_type=file_type,
    )


def _raw(uid: int, *, size: int = 1000, minutes: float = 0.0, name: str = "Antminer_S19.raw.csv"):
    return _file(name, uid=uid, size=size, minutes=minutes, file_type="RAW")


def _markup(
    uid: int, *, size: int = 100, minutes: float = 1.0, name: str = "Antminer_S19.markup.csv"
):
    return _file(name, uid=uid, size=size, minutes=minutes, file_type="LOADS")


def _pair_override(raw_id: str, markup_id: str, *, at: str = "2026-09-15T10:00:00") -> Overrides:
    """Набор решений с одной привязкой разметки (время решения задаётся явно)."""
    overrides = Overrides()
    entry = overrides.add(
        action=ACTION_LINK,
        base_name="Antminer_S19",
        raw_id=raw_id,
        markup_id=markup_id,
        comment="ручная привязка",
    )
    entry.at = at
    return overrides


def test_pair_by_import_time_is_confirmed():
    overview = build_records([_raw(1), _markup(2)])

    record = overview.records[0]

    assert len(overview.records) == 1
    assert record.record_id == record_id_for("Antminer_S19", _raw(1).id)
    assert record.has_markup
    assert record.confidence == CONFIDENCE_CONFIRMED
    assert record.rule == "имя + время импорта"
    assert record.delta_seconds == 60.0
    assert "позже" in record.delta_note
    assert overview.stats.confirmed == 1
    assert overview.stats.coverage == 100.0


def test_weak_pair_is_marked_and_requires_attention():
    overview = build_records([_raw(1), _markup(2, minutes=180)])

    record = overview.records[0]

    assert record.confidence == CONFIDENCE_WEAK
    assert record.needs_attention
    assert overview.stats.weak == 1
    assert record in overview.requires_attention


def test_duplicates_are_all_visible_and_latest_is_current():
    overview = build_records(
        [
            _raw(1, size=1000, minutes=0),
            _raw(2, size=2222, minutes=1),
            _markup(3, size=100, minutes=1),
        ]
    )

    first, second = overview.records

    assert len(overview.records) == 2
    assert first.group_size == second.group_size == 2
    assert first.version == 1 and second.version == 2
    assert second.is_current and not first.is_current
    assert first.duplicate and second.duplicate
    assert overview.stats.duplicates == 2
    assert overview.stats.duplicate_bases == 1
    assert overview.duplicate_bases == ("Antminer_S19",)
    assert [file["raw_size"] for file in overview.manifest_rows()] == [1000, 2222]


def test_raw_without_markup_stays_a_record_without_markup():
    overview = build_records([_raw(1)])

    record = overview.records[0]

    assert not record.has_markup
    assert record.confidence == CONFIDENCE_NONE
    assert record.rule == "имя (разметка не найдена)"
    assert record.delta_seconds is None
    assert overview.stats.records == 1
    assert overview.stats.without_markup == 1
    assert overview.unpaired_raw == (record.raw,)
    assert overview.unpaired_markup == ()
    assert overview.unpaired_files == 1
    assert overview.requires_attention == (record,)


def test_markup_without_raw_is_unpaired():
    overview = build_records([_markup(2)])

    assert overview.records == ()
    assert len(overview.unpaired_markup) == 1
    assert overview.stats.unpaired_markup == 1
    assert overview.stats.records == 0
    assert overview.manifest_rows()[0]["row_kind"] == ROW_UNPAIRED_MARKUP


def test_other_files_are_not_records():
    model = _file("model_1.h5", uid=9, file_type="H5")

    overview = build_records([model, _raw(1), _markup(2)])

    assert overview.other_files == (model,)
    assert overview.stats.other == 1
    assert overview.manifest_rows()[-1]["row_kind"] == ROW_OTHER


def test_candidates_list_other_markup_of_same_base_by_time():
    overview = build_records([_raw(1), _markup(2, minutes=1), _markup(3, minutes=30)])

    record = overview.records[0]

    assert len(record.candidates) == 1
    assert str(record.candidates[0].id) == str(_markup(3).id)
    assert len(overview.unpaired_markup) == 1


def test_markup_stats_are_attached_by_file_id():
    markup = _markup(2)
    stats = {str(markup.id): MarkupStatsEntry(records=64, chunks=8, status="рассчитано")}

    overview = build_records([_raw(1), markup], markup_stats=stats)

    record = overview.records[0]
    assert record.stats.records == 64
    assert record.stats.chunks == 8
    assert record.stats.is_calculated


# ---------------------------------------------------------------------------
# Ручные решения оператора
# ---------------------------------------------------------------------------
def test_manual_link_wins_over_heuristic_and_takes_markup_from_other_record():
    raw_first, raw_second, markup = _raw(1), _raw(2, size=5000, minutes=1), _markup(3, minutes=2)
    overrides = _pair_override(str(raw_second.id), str(markup.id))

    overview = build_records([raw_first, raw_second, markup], overrides=overrides)

    first, second = overview.records
    assert second.has_markup and second.manual
    assert second.confidence == CONFIDENCE_MANUAL
    assert second.rule == "решение оператора"
    assert second.comment == "ручная привязка"
    assert not first.has_markup
    assert overview.stats.manual == 1
    assert overview.stats.without_markup == 1
    assert overview.unpaired_markup == ()


def test_manual_unlink_removes_heuristic_pair():
    raw, markup = _raw(1), _markup(2)
    overrides = Overrides()
    overrides.add(action=ACTION_UNLINK, base_name="Antminer_S19", raw_id=str(raw.id))

    overview = build_records([raw, markup], overrides=overrides)

    record = overview.records[0]
    assert not record.has_markup
    assert record.manual
    assert overview.stats.unpaired_markup == 1


def test_exclude_and_current_overrides_are_applied():
    raw_first, raw_second, markup = _raw(1), _raw(2, size=5000, minutes=1), _markup(3, minutes=2)
    overrides = Overrides()
    overrides.add(action=ACTION_EXCLUDE, base_name="Antminer_S19", raw_id=str(raw_first.id))
    overrides.add(action=ACTION_CURRENT, base_name="Antminer_S19", raw_id=str(raw_first.id))

    overview = build_records([raw_first, raw_second, markup], overrides=overrides)

    first, second = overview.records
    assert first.excluded
    assert first.is_current and not second.is_current
    assert overview.stats.excluded == 1


def test_last_decision_wins_for_the_same_raw():
    raw, markup_first, markup_second = _raw(1), _markup(2, minutes=1), _markup(3, minutes=2)
    overrides = Overrides()
    entry_link = overrides.add(
        action=ACTION_LINK,
        base_name="Antminer_S19",
        raw_id=str(raw.id),
        markup_id=str(markup_first.id),
    )
    entry_link.at = "2026-09-15T10:00:00"
    entry_unlink = overrides.add(action=ACTION_UNLINK, base_name="Antminer_S19", raw_id=str(raw.id))
    entry_unlink.at = "2026-09-15T11:00:00"

    overview = build_records([raw, markup_first, markup_second], overrides=overrides)

    assert not overview.records[0].has_markup
    assert overview.records[0].manual


def test_decision_for_unknown_file_is_ignored():
    raw, markup = _raw(1), _markup(2)
    overrides = Overrides()
    overrides.add(
        action=ACTION_LINK,
        base_name="Antminer_S19",
        raw_id=str(raw.id),
        markup_id="99999999-0000-4000-8000-000000000000",
    )

    overview = build_records([raw, markup], overrides=overrides)

    record = overview.records[0]
    assert record.has_markup
    assert not record.manual
    assert record.confidence == CONFIDENCE_CONFIRMED


def test_decision_for_other_base_does_not_affect_this_one():
    raw, markup = _raw(1), _markup(2)
    overrides = Overrides()
    overrides.add(action=ACTION_EXCLUDE, base_name="Other_Unit", raw_id=str(raw.id))

    overview = build_records([raw, markup], overrides=overrides)

    assert not overview.records[0].excluded


# ---------------------------------------------------------------------------
# Манифест
# ---------------------------------------------------------------------------
def test_manifest_contains_every_file_exactly_once():
    files = [
        _raw(1),
        _raw(2, size=2222, minutes=1),
        _markup(3, minutes=1),
        _markup(4, minutes=5, name="Antminer_S19.markup.csv"),
        _markup(5, name="Unknown_Unit.markup.csv", minutes=9),
        _file("model_1.h5", uid=6, file_type="H5"),
    ]

    overview = build_records(files)
    rows = overview.manifest_rows()
    kinds = [row["row_kind"] for row in rows]
    ids = overview.manifest_file_ids()

    assert kinds.count(ROW_RECORD) == 2
    assert kinds.count(ROW_UNPAIRED_MARKUP) == 1
    assert kinds.count(ROW_OTHER) == 1
    assert (
        len(rows) == overview.stats.records + overview.stats.unpaired_markup + overview.stats.other
    )
    assert len(ids) == len(set(ids)) == len(files) == 6
    assert set(ids) == {str(file.id) for file in files}
    assert overview.manifest_is_complete()


def test_manifest_csv_has_all_columns_and_is_parsable():
    overview = build_records([_raw(1), _markup(2)])

    parsed = list(csv.DictReader(io.StringIO(overview.manifest_csv())))

    assert list(parsed[0]) == list(MANIFEST_COLUMNS)
    assert parsed[0]["row_kind"] == ROW_RECORD
    assert parsed[0]["is_current"] == "да"
    assert parsed[0]["duplicate"] == "нет"


def test_manifest_json_contains_meta_and_stats():
    overview = build_records([_raw(1), _markup(2)])

    payload = json.loads(overview.manifest_json({"session_id": "20260915-101500-1a2b"}))

    assert payload["meta"]["session_id"] == "20260915-101500-1a2b"
    assert payload["stats"]["records"] == 1
    assert payload["rows"][0]["confidence"] == CONFIDENCE_CONFIRMED


def test_save_manifest_writes_csv_utf8_sig_and_json(tmp_path: Path):
    overview = build_records([_raw(1), _markup(2)])

    csv_path, json_path = save_manifest(overview, tmp_path, meta={"session_id": "s-1"})

    assert csv_path.exists() and json_path.exists()
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(json_path.read_text(encoding="utf-8"))["meta"]["session_id"] == "s-1"


def test_stats_rows_and_dict_are_consistent():
    overview = build_records([_raw(1), _markup(2), _markup(3, name="Loose.markup.csv")])

    stats = overview.stats.to_dict()

    assert (
        stats["files"]
        == stats["records"] + stats["attached_markup"] + stats["unpaired_markup"] + stats["other"]
    )
    assert stats["attached_markup"] == stats["records"] - stats["without_markup"] == 1
    assert stats["coverage_percent"] == 100.0
    assert stats["total_bytes"] == stats["raw_bytes"] + stats["markup_bytes"]
    assert any(row["metric"] == "Записей (RAW + markup)" for row in overview.stats_rows())


def test_record_id_is_stable_and_depends_on_raw():
    raw = _raw(1)

    assert record_id_for("Antminer_S19", raw.id) == record_id_for("Antminer_S19", raw.id)
    assert record_id_for("Antminer_S19", raw.id) != record_id_for("Other_Unit", raw.id)
    assert len(record_id_for("Antminer_S19", raw.id)) == 12
