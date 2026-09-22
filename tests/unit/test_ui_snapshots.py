"""Тесты компонента снимков стенда (`acceptance.ui.components.snapshots`).

Снимок — это «фотография» реестров стенда на начало и конец сессии (`DR-P-8`), по которой
видно, сколько данных пульт создал и удалил (`FR-P-47`). Проверяется вся арифметика
сравнения: счётчики снимка строками, разница «до/после», CSV для приложения к отчёту и
выбор снимка для показа (снимок «Начало» важнее результата разовой проверки связи).
"""

from __future__ import annotations

from acceptance.session import new_session
from acceptance.ui.components import layout, snapshots, status

START_SNAPSHOT = {
    "at": "2026-09-21T10:12:00",
    "version": {"branch": "dev", "revision": "83319ae1234"},
    "counts": {"files": 265, "records": 129, "loads": 12, "datasets": 9, "models": 6, "tasks": 215},
    "files": {"total_bytes": 47_300_000_000, "duplicate_names": 1},
    "errors": {},
}

END_SNAPSHOT = {
    "at": "2026-09-21T18:40:00",
    "version": {"branch": "dev", "revision": "83319ae1234"},
    "counts": {
        "files": 267,
        "records": 130,
        "loads": 12,
        "datasets": 10,
        "models": 6,
        "tasks": 213,
    },
    "errors": {"datasets": "[500] Internal Server Error"},
}


def test_counters_rows_show_every_registry():
    """Счётчики снимка показываются по каждому реестру, а не только по непустым."""
    rows = snapshots.counters_rows(START_SNAPSHOT)
    values = {row["counter"]: row["value"] for row in rows}

    assert "265" in values["$Файл"]
    assert values["$Нагрузка"] == "12"
    assert values["$Задача"] == "215"
    assert any(row["counter"] == "Объём $Файл" and "47.30 ГБ" in row["value"] for row in rows)


def test_counters_rows_mark_missing_values():
    """Реестр, которого нет в снимке, честно помечается «нет данных»."""
    rows = snapshots.counters_rows({"counts": {"files": 1}, "files": {}})
    values = {row["counter"]: row["value"] for row in rows}

    assert values["$Файл"] == "1"
    assert values["$Датасет"] == "нет данных"


def test_delta_rows_count_change_per_registry():
    """Разница снимков считается по каждому реестру: +2 файла, +1 датасет, −2 задачи."""
    rows = snapshots.delta_rows(START_SNAPSHOT, END_SNAPSHOT)
    deltas = {row["counter"]: row["delta"] for row in rows}

    assert deltas["$Файл"] == "+2"
    assert deltas["$Датасет"] == "+1"
    assert deltas["$Задача"] == "-2"
    assert deltas["$Нагрузка"] == "0"


def test_delta_rows_report_missing_side():
    """Если счётчика нет в одном из снимков, разница не выдумывается."""
    rows = snapshots.delta_rows({"counts": {"files": 1}}, {"counts": {"files": 3, "loads": 1}})
    loads = next(row for row in rows if row["counter"] == "$Нагрузка")

    assert loads["before"] == "—"
    assert loads["delta"] == "нет данных"


def test_delta_csv_is_ready_for_report():
    """CSV сравнения снимков — приложение к отчёту: заголовок и разделитель «;»."""
    text = snapshots.delta_csv(snapshots.delta_rows(START_SNAPSHOT, END_SNAPSHOT))
    lines = text.strip().splitlines()

    assert lines[0] == "Реестр;Начало;Окончание;Разница"
    assert any(line.startswith("$Файл;265;267;+2") for line in lines)
    assert text.endswith("\n")


def test_current_prefers_session_start_snapshot():
    """Для показа берётся снимок «Начало» сессии, иначе — результат проверки связи."""
    session = new_session(base_url="http://test.local")
    assert snapshots.current(session, START_SNAPSHOT) == START_SNAPSHOT

    session.snapshots["start"] = START_SNAPSHOT
    assert snapshots.current(session, {"counts": {"files": 1}})["counts"]["files"] == 265
    assert snapshots.current(None, None) == {}


def test_labels_and_caption_name_snapshot_time_and_build():
    """Подписи снимков: что снято, когда и на какой сборке."""
    session = new_session(base_url="http://test.local")
    session.snapshots["start"] = START_SNAPSHOT

    assert snapshots.labels(session)[0] == ("Начало", "2026-09-21T10:12:00")
    assert snapshots.labels(session)[1] == ("Окончание", "")
    assert snapshots.caption(START_SNAPSHOT) == "снимок 21.09 10:12 · dev@83319ae1234"
    assert snapshots.caption({}) == ""


def test_error_text_reports_partial_snapshot():
    """Частичный снимок не молчит: видно, какой запрос не удался."""
    assert snapshots.error_text(START_SNAPSHOT) == ""
    assert "datasets" in snapshots.error_text(END_SNAPSHOT)


def test_build_warning_compares_session_and_stand():
    """Расхождение сборки стенда с реквизитами сессии даёт предупреждение (`AC-P-1`)."""
    session = new_session(
        base_url="http://test.local", server_version={"branch": "dev", "revision": "83319ae"}
    )

    assert status.build_warning(session, "dev@83319ae") == ""
    assert status.build_warning(session, "") == ""
    warning = status.build_warning(session, "main@1111111")

    assert "main@1111111" in warning
    assert "dev@83319ae" in warning


def test_short_time_formats_iso_and_keeps_unknown_text():
    """Короткое время: ISO-момент форматируется, непонятный текст не теряется."""
    assert layout.short_time("2026-09-22T10:11:12") == "22.09 10:11"
    assert layout.short_time("") == ""
    assert layout.short_time("не время") == "не время"
    assert layout.short_time(None) == ""
