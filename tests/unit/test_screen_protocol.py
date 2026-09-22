"""Тесты экрана «Протокол проверок» (`SCR-401`).

Протокол — свод результатов испытаний: он показывает состав программы с вердиктами, диапазоны
журнала, ручные отметки, снятие с причиной и результаты «вне программы». Здесь держатся
правила экрана:

    * выборка и KPI считаются по `results.py` (`DR-P-5`) — второй копии статуса нет;
    * результат, которого нет в программе, остаётся в протоколе пометкой «вне программы»
      (`FR-P-13`, `FR-P-69`), а снятые видны отдельно (`FR-P-35`);
    * экран **не** запускает проверки (`IR-P-4`) и **не** правит состав программы (`IR-P-18`).

Логика вынесена в чистые функции, поэтому проверяется без Streamlit и без стенда.
"""

from __future__ import annotations

import csv
import io
import pathlib
from pathlib import Path

from acceptance import results as results_api
from acceptance import sets as sets_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme
from acceptance.session import TestSession as SessionModel
from acceptance.session import new_session
from acceptance.ui.screens import scr401_protocol

#: Команды запуска и исполнитель прогона: в протоколе их быть не должно (`IR-P-4`).
RUN_COMMANDS = (
    "execute_next",
    "run_batch_tech",
    "auto_step",
    "execute_call",
    "execute_check",
    "from acceptance.runner",
)

#: Идентификаторы, которые нужны тестам: безопасная проверка и результат вне программы.
TECH_CHECK = "TC-SYS-01"
OUTSIDE_CHECK = "TC-FILE-10"


def _programme(*set_ids: str) -> Programme:
    """Программа из наборов библиотеки (объединение по ИЛИ)."""
    library = sets_api.library_from_catalog()
    ids = list(set_ids) or [sets_api.SMOKE_SET_ID]
    return Programme.from_sets(library, ids)


def _session_with_results(programme: Programme) -> SessionModel:
    """Сессия с результатами: успех в программе и отказ вне программы."""
    session = new_session(base_url="http://test.local")
    results_api.record(
        session,
        {
            "check_id": TECH_CHECK,
            "status": str(CheckStatus.PASSED),
            "verdict": "соответствует",
            "journal_from": 12,
            "journal_to": 13,
        },
    )
    results_api.record(
        session,
        {
            "check_id": OUTSIDE_CHECK,
            "status": str(CheckStatus.FAILED),
            "verdict": "405 вместо 404",
            "operator_note": "подтверждаю отказ",
        },
    )
    return session


# ---------------------------------------------------------------------------
# Строки протокола: состав программы, результаты и «вне программы»
# ---------------------------------------------------------------------------
def test_protocol_rows_follow_programme_order():
    """Строки протокола идут в порядке программы, пустой результат виден как «не выполнена»."""
    programme = _programme()
    rows = scr401_protocol.protocol_rows(new_session(base_url="http://test.local"), programme)

    assert [row["check_id"] for row in rows] == programme.check_ids
    first = rows[0]
    assert first["status"] == "⚪ не выполнена"
    assert first["status_raw"] == results_api.STATUS_NOT_RUN
    assert first["verdict"] == scr401_protocol.DASH
    assert first["has_result"] is False
    assert first["group"] == catalog.group_of(first["check_id"])
    assert first["programme_group"] == scr401_protocol.IN_PROGRAMME


def test_protocol_rows_show_result_and_journal():
    """Строка результата несёт статус, вердикт, диапазон журнала и источник значения."""
    programme = _programme()
    session = _session_with_results(programme)
    row = next(
        item
        for item in scr401_protocol.protocol_rows(session, programme)
        if item["check_id"] == TECH_CHECK
    )

    assert row["status"] == "✅ успех"
    assert row["verdict"] == "соответствует"
    assert row["journal_range"] == "#12–#13"
    assert row["origin"] == "пульт", "рядом видно, кто дал значение (`IR-P-7`)"
    assert row["programme"], "у строки программы виден набор-источник"
    assert row["mark"] == "", "у машинного результата заключение оператора не требуется"


def test_protocol_rows_keep_results_outside_programme():
    """Результат вне программы не исчезает из протокола (`FR-P-13`, `FR-P-69`)."""
    programme = _programme()
    session = _session_with_results(programme)
    rows = {row["check_id"]: row for row in scr401_protocol.protocol_rows(session, programme)}

    assert OUTSIDE_CHECK in rows
    outside = rows[OUTSIDE_CHECK]
    assert programme.find(OUTSIDE_CHECK) is None
    assert outside["programme"] == scr401_protocol.OUTSIDE_PROGRAMME
    assert outside["programme_group"] == scr401_protocol.OUTSIDE_PROGRAMME
    assert outside["verdict"] == "405 вместо 404"


def test_suspended_result_is_shown_separately():
    """Снятие с причиной видно отдельно от «вне программы» (`FR-P-15`, `FR-P-35`)."""
    programme = _programme()
    session = new_session(base_url="http://test.local")
    results_api.suspend(session, TECH_CHECK, "отказ стенда", author="Петров П.П.")
    row = next(
        item
        for item in scr401_protocol.protocol_rows(session, programme)
        if item["check_id"] == TECH_CHECK
    )

    assert row["suspended"] is True
    assert row["programme_group"] == scr401_protocol.SUSPENDED
    assert row["reason"] == "отказ стенда"
    assert row["status_raw"] == str(CheckStatus.SKIPPED)
    assert row["origin"] == "оператор"


def test_needs_conclusion_requires_operator_note():
    """Статус человека без заключения помечается «нужно заключение» (`FR-P-32`)."""
    manual = {
        "has_result": True,
        "status": str(CheckStatus.MANUAL_OK),
        "operator_note": "",
    }

    assert scr401_protocol.needs_conclusion(manual) is True
    assert scr401_protocol.needs_conclusion({**manual, "operator_note": "сверил"}) is False
    assert scr401_protocol.needs_conclusion({"has_result": False, "status": "пропущена"}) is False
    assert (
        scr401_protocol.needs_conclusion(
            {"has_result": True, "status": str(CheckStatus.PASSED), "operator_note": ""}
        )
        is False
    )


def test_journal_range_formats_bounds():
    """Диапазон журнала показывается компактно и не выдумывает отсутствующие границы."""
    assert scr401_protocol.journal_range({}) == ""
    assert scr401_protocol.journal_range({"journal_from": 12}) == "#12"
    assert scr401_protocol.journal_range({"journal_to": 13}) == "#13"
    assert scr401_protocol.journal_range({"journal_from": 12, "journal_to": 12}) == "#12"
    assert scr401_protocol.journal_range({"journal_from": 12, "journal_to": 13}) == "#12–#13"


# ---------------------------------------------------------------------------
# Фильтры, KPI и выгрузки выборки
# ---------------------------------------------------------------------------
def test_filters_narrow_the_selection():
    """Фильтры выборки: группа, статус, место в программе, поиск и «только невыполненные»."""
    programme = _programme()
    session = _session_with_results(programme)
    rows = scr401_protocol.protocol_rows(session, programme)

    by_status = scr401_protocol.filter_rows(rows, status_value=str(CheckStatus.FAILED))
    assert [row["check_id"] for row in by_status] == [OUTSIDE_CHECK]

    by_group = scr401_protocol.filter_rows(rows, group=catalog.group_of(TECH_CHECK))
    assert by_group and all(row["group"] == catalog.group_of(TECH_CHECK) for row in by_group)

    outside = scr401_protocol.filter_rows(rows, programme_value=scr401_protocol.OUTSIDE_PROGRAMME)
    assert [row["check_id"] for row in outside] == [OUTSIDE_CHECK]

    pending = scr401_protocol.filter_rows(rows, only_pending=True)
    assert pending and all(row["has_result"] is False for row in pending)
    assert len(pending) == len(rows) - 2

    found = scr401_protocol.filter_rows(rows, search="tc-sys-01")
    assert [row["check_id"] for row in found] == [TECH_CHECK]
    assert scr401_protocol.filter_rows(rows, search="нет-такой-проверки") == []
    assert len(scr401_protocol.filter_rows(rows, group=scr401_protocol.ANY)) == len(rows)


def test_filter_options_describe_selection():
    """Варианты фильтров начинаются с «все» и содержат только то, что есть в выборке."""
    programme = _programme()
    session = _session_with_results(programme)
    rows = scr401_protocol.protocol_rows(session, programme)

    groups = scr401_protocol.group_options(rows)
    classes = scr401_protocol.class_options(rows)
    statuses = scr401_protocol.status_filter_options(rows)

    assert groups[0] == scr401_protocol.ANY
    assert catalog.group_of(TECH_CHECK) in groups
    assert classes[0] == scr401_protocol.ANY and len(classes) > 1
    assert statuses[0] == scr401_protocol.ANY
    assert set(statuses[1:]) == {
        results_api.STATUS_NOT_RUN,
        str(CheckStatus.PASSED),
        str(CheckStatus.FAILED),
    }
    assert str(CheckStatus.INTERRUPTED) not in statuses
    assert scr401_protocol.PROGRAMME_CHOICES[0] == scr401_protocol.ANY


def test_kpi_and_readiness_count_selection():
    """KPI выборки и готовность считаются по результатам (`FR-P-64`)."""
    programme = _programme()
    session = _session_with_results(programme)
    rows = scr401_protocol.protocol_rows(session, programme)
    kpi = dict(scr401_protocol.kpi_items(rows))

    assert kpi["В выборке"] == len(rows)
    assert kpi["Выполнено"] == 2
    assert kpi["Успех"] == 1
    assert kpi["Отказ"] == 1
    assert kpi["Не выполнено"] == len(rows) - 2
    assert kpi["Снято"] == 0 and kpi["Нужно заключение"] == 0

    note = scr401_protocol.readiness_note(rows)
    assert f"2 из {len(rows)}" in note and "%" in note
    assert scr401_protocol.readiness_note([]) == "Готовность протокола: нет пунктов"


def test_conclusion_note_lists_rows_waiting_for_note():
    """Строки без заключения оператора перечисляются в подсказке (`FR-P-32`)."""
    rows = [
        {
            "check_id": TECH_CHECK,
            "mark": scr401_protocol.CONCLUSION_MARK,
            "has_result": True,
            "suspended": False,
            "repeats": 0,
            "status_raw": str(CheckStatus.MANUAL_OK),
            "programme_group": scr401_protocol.IN_PROGRAMME,
            "group": "",
            "check_class": "",
            "title": "",
            "requirement": "",
        }
    ]

    assert scr401_protocol.conclusion_note(rows).startswith(scr401_protocol.CONCLUSION_MARK)
    assert TECH_CHECK in scr401_protocol.conclusion_note(rows)
    assert scr401_protocol.conclusion_note([]) == ""


def test_protocol_csv_has_fixed_columns():
    """Выгрузка протокола — CSV с фиксированными колонками макета (`FR-P-34`)."""
    programme = _programme()
    session = _session_with_results(programme)
    rows = scr401_protocol.protocol_rows(session, programme)

    text = scr401_protocol.protocol_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text)))

    assert len(scr401_protocol.CSV_COLUMNS) == 14
    assert list(parsed[0]) == list(scr401_protocol.CSV_COLUMNS)
    assert len(parsed) == len(rows)
    exported = next(item for item in parsed if item["check_id"] == TECH_CHECK)
    assert exported["verdict"] == "соответствует"
    assert exported["journal_range"] == "#12–#13"
    assert exported["status"].endswith("успех"), "в CSV статус с пиктограммой — как в таблице"


def test_mark_payload_and_export_name():
    """Payload отметки несёт заключение оператора, имя выгрузки — номер сессии."""
    session = new_session(base_url="http://test.local")
    payload = scr401_protocol.mark_payload("tc-sys-06", str(CheckStatus.MANUAL_OK), "сверил")

    assert payload["check_id"] == "TC-SYS-06"
    assert payload["status"] == str(CheckStatus.MANUAL_OK)
    assert payload["operator_note"] == "сверил"
    assert scr401_protocol.export_name(session) == f"protocol_{session.session_id}.csv"


def test_save_selection_registers_artifact(tmp_path: Path, monkeypatch):
    """Сохранение выборки кладёт CSV в артефакты и регистрирует его в сессии (`FR-P-64`)."""
    programme = _programme()
    session = _session_with_results(programme)
    rows = scr401_protocol.protocol_rows(session, programme)
    monkeypatch.setattr(scr401_protocol, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(scr401_protocol, "ensure_dirs", lambda: None)

    path = scr401_protocol.save_selection(session, rows)

    assert path.parent == tmp_path
    assert path.exists()
    assert path.read_text(encoding="utf-8-sig").startswith("check_id,group,")
    assert session.artifacts, "выгрузка не попала в артефакты сессии"
    assert session.artifacts[-1]["kind"] == "protocol_csv"
    assert str(len(rows)) in session.artifacts[-1]["note"]


# ---------------------------------------------------------------------------
# Запреты протокола (`IR-P-4`, `IR-P-18`, `DR-P-5`)
# ---------------------------------------------------------------------------
def test_protocol_screen_has_no_run_commands():
    """Протокол не запускает проверки: команда одна, на «Прогоне» (`IR-P-4`)."""
    source = pathlib.Path(str(scr401_protocol.__file__)).read_text(encoding="utf-8")

    for command in RUN_COMMANDS:
        assert command not in source, f"в протоколе найдена команда запуска {command}"
    assert "queue.begin(" not in source
    assert "queue.finish(" not in source


def test_protocol_screen_does_not_edit_programme():
    """Протокол не правит состав программы: сокращение — решение на `SCR-102` (`IR-P-18`)."""
    source = pathlib.Path(str(scr401_protocol.__file__)).read_text(encoding="utf-8")

    for call in (
        "programme.exclude(",
        "programme.add_check(",
        "programme.rebuild(",
        "programme.move(",
        "programme.approve(",
        "programme.revise(",
    ):
        assert call not in source, f"протокол меняет состав программы: {call}"


def test_protocol_screen_writes_results_only_through_results_module():
    """Единственный источник статуса — `results.py` (`DR-P-5`)."""
    source = pathlib.Path(str(scr401_protocol.__file__)).read_text(encoding="utf-8")

    assert "results_api.record(" in source
    assert "session.checks.append" not in source
