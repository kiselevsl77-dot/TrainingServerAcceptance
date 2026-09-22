"""Тесты экрана «Отчёт испытаний» (`SCR-404`).

Отчёт — комплект для заказчика: шапка с воспроизводимостью (`FR-P-53`), готовность с перечнем
недостающего (`FR-P-48`), состав разделов и приложений (`FR-P-50`), контроль `__TEST__`-сущностей
(`FR-P-46`) и итоговое решение с подписями (`FR-P-51`).

Здесь держатся и запреты экрана: комплект собирается **по данным сессии** (`DR-P-7`) — стенд при
формировании не опрашивается, команд запуска у экрана нет (`IR-P-4`).
"""

from __future__ import annotations

import pathlib

from acceptance import report as report_api
from acceptance import results as results_api
from acceptance import session as session_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.session import TestSession as SessionModel
from acceptance.session import (
    add_test_entity,
    close_session,
    mark_test_entity_deleted,
    new_session,
    set_snapshot,
)
from acceptance.ui.screens import scr404_report

#: Команды запуска: в отчёте их быть не должно (`IR-P-4`).
RUN_COMMANDS = (
    "execute_next",
    "run_batch_tech",
    "auto_step",
    "execute_call",
    "execute_check",
    "from acceptance.runner",
)


def _session() -> SessionModel:
    """Сессия с реквизитами, снимками и одной выполненной проверкой."""
    session = new_session(base_url="http://test.local")
    session.info.title = "Приёмка API"
    session.info.object_of_test = "Стенд обучения"
    session.info.operator_fio = "Петров П.П."
    session.server_version = {"branch": "dev", "revision": "83319ae1234"}
    session.logs = {
        "level": "INFO",
        "app_log": "acceptance_data/logs/applog/applog_1.log",
        "session_log": "acceptance_data/logs/session_x.jsonl",
    }
    session.tasks = []
    set_snapshot(session, session_api.SNAP_START, {"files": 1})
    set_snapshot(session, session_api.SNAP_END, {"files": 2})
    session.checks.append(
        {"check_id": "TC-SYS-01", "status": str(CheckStatus.PASSED), "verdict": "соответствует"}
    )
    return session


def _complete_session() -> SessionModel:
    """Сессия, доведённая до подписания: все проверки выполнены, решение зафиксировано."""
    session = _session()
    for spec in catalog.CHECKS:
        results_api.record(
            session,
            {
                "check_id": spec.check_id,
                "status": str(CheckStatus.PASSED),
                "verdict": "соответствует",
            },
        )
    close_session(session, conclusion="годен")
    return session


# ---------------------------------------------------------------------------
# Шапка отчёта и трассируемость (`FR-P-53`)
# ---------------------------------------------------------------------------
def test_session_header_lists_traceability():
    """Шапка называет сессию, сборку, адрес, период, версию пульта и статус (`FR-P-53`)."""
    session = _session()

    header = scr404_report.session_header(session)

    assert session.session_id in header
    assert "dev@83319ae123" in header
    assert "http://test.local" in header
    assert "пульт" in header
    assert f"статус {session.status}" in header
    assert "не определена" in scr404_report.session_header(
        new_session(base_url="http://test.local")
    )


def test_log_rows_list_session_logs():
    """Файлы журналов сессии видны в шапке: комплект трассируем (`FR-P-53`)."""
    session = _session()

    rows = scr404_report.log_rows(session)

    assert [row["log"] for row in rows] == ["app_log", "session_log"]
    assert rows[0]["path"].endswith("applog_1.log")
    assert scr404_report.log_rows(new_session(base_url="http://test.local")) == []


# ---------------------------------------------------------------------------
# Готовность: перечень недостающего (`FR-P-48`)
# ---------------------------------------------------------------------------
def test_readiness_criteria_mark_missing_parts():
    """Пустая сессия: реквизиты, снимки и решение не выполнены, готовность ниже 100 %."""
    criteria = scr404_report.readiness_criteria(new_session(base_url="http://test.local"))
    by_name = {item["criterion"]: item for item in criteria}

    assert len(criteria) >= 5
    assert by_name["реквизиты сессии (наименование и объект испытаний)"]["state"] == (
        scr404_report.MISSING
    )
    assert by_name["снимок стенда «начало»"]["state"] == scr404_report.MISSING
    assert by_name["итоговое решение сессии"]["state"] == scr404_report.MISSING
    assert scr404_report.readiness_percent(criteria) < 100
    assert scr404_report.readiness_percent([]) == 0
    assert "Готовность отчёта" in scr404_report.readiness_note([])


def test_readiness_criteria_pass_on_complete_session():
    """Полная сессия: все пункты полноты выполнены, готовность 100 %."""
    session = _complete_session()

    criteria = scr404_report.readiness_criteria(session)
    failed = [item["criterion"] for item in criteria if not item["passed"]]

    assert not failed, failed
    assert scr404_report.readiness_percent(criteria) == 100
    assert f"({len(criteria)} из {len(criteria)})" in scr404_report.readiness_note(criteria)


def test_readiness_criteria_show_counts_for_operator():
    """Пункт полноты объясняет состояние числами: сколько проверок выполнено и т. п."""
    session = _complete_session()
    session.checks[-1]["status"] = str(CheckStatus.FAILED)

    criteria = {item["criterion"]: item for item in scr404_report.readiness_criteria(session)}

    assert criteria["выполнение проверок"]["passed"] is True
    assert "выполнено" in criteria["выполнение проверок"]["note"]
    assert criteria["разбор отказов и блокировок"]["passed"] is False
    assert "отказы 1" in criteria["разбор отказов и блокировок"]["note"]


def test_readiness_flags_notes_without_reproduction():
    """Замечание без воспроизведения мешает подписанию отчёта (`FR-P-48`)."""
    session = _session()
    session.notes.append(
        {
            "note_id": "n-1",
            "created_at": "2026-09-22T10:00:00",
            "title": "Без воспроизведения",
            "priority": "P0",
            "status": "открыто",
        }
    )

    ready = report_api.readiness(session)
    criteria = {item["criterion"]: item for item in scr404_report.readiness_criteria(session)}

    assert ready["notes"]["incomplete"] == 1
    assert ready["ready"] is False
    assert any("FR-P-48" in warning for warning in ready["warnings"])
    assert criteria["замечания к API без пробелов"]["passed"] is False


# ---------------------------------------------------------------------------
# Разделы, приложения, уборка и решение (`FR-P-46`, `FR-P-50`, `FR-P-51`)
# ---------------------------------------------------------------------------
def test_section_rows_number_sections_and_annexes():
    """Состав комплекта: разделы отчёта идут по номерам, приложения — списком (`FR-P-50`)."""
    rows = scr404_report.section_rows(["checks.csv", "notes.csv"])

    sections = [row for row in rows if row["kind"] == "раздел"]
    annexes = [row for row in rows if row["kind"] == "приложение"]

    assert [row["number"] for row in sections] == list(range(1, len(report_api.SECTION_TITLES) + 1))
    assert sections[0]["section"] == report_api.SECTION_TITLES[0]
    assert [row["section"] for row in annexes] == ["checks.csv", "notes.csv"]
    assert all(row["number"] == "" for row in annexes)


def test_annex_rows_describe_files(tmp_path: pathlib.Path):
    """Файлы комплекта показываются ролью, именем и путём (`FR-P-49`)."""
    paths = {"report_md": tmp_path / "report_1.md", "report_json": tmp_path / "report_1.json"}

    rows = scr404_report.annex_rows(paths)

    assert [row["role"] for row in rows] == ["report_json", "report_md"]
    assert rows[0]["name"] == "report_1.json"
    assert rows[0]["path"].endswith("report_1.json")


def test_pending_rows_list_unremoved_entities():
    """Контроль `__TEST__`: не удалённые сущности видны, после уборки список пуст (`FR-P-46`)."""
    session = _session()

    assert scr404_report.pending_rows(session) == []

    add_test_entity(session, entity_id="__TEST__file-1", entity_type="file", check_id="TC-SYS-01")
    rows = scr404_report.pending_rows(session)

    assert [row["id"] for row in rows] == ["__TEST__file-1"]
    assert rows[0]["check_id"] == "TC-SYS-01"

    mark_test_entity_deleted(session, "__TEST__file-1", check_id="TC-SYS-01")
    assert scr404_report.pending_rows(session) == []


def test_solution_note_reports_decision_and_signatures():
    """Решение и подписи видны одной строкой (`FR-P-51`)."""
    session = _session()

    assert "не зафиксировано" in scr404_report.solution_note(session)

    close_session(session, conclusion="годен с замечаниями")
    session.info.signatures = "Иванов И.И."

    note = scr404_report.solution_note(session)

    assert "годен с замечаниями" in note
    assert "Иванов И.И." in note
    assert scr404_report.CONCLUSIONS == session_api.CONCLUSIONS


# ---------------------------------------------------------------------------
# Запреты отчёта (`DR-P-7`, `IR-P-4`)
# ---------------------------------------------------------------------------
def test_report_screen_does_not_ask_the_stand():
    """Комплект собирается по сессии: стенд при формировании не опрашивается (`DR-P-7`)."""
    source = pathlib.Path(str(scr404_report.__file__)).read_text(encoding="utf-8")

    assert "state.get_runtime()" not in source, "экран отчёта не должен обращаться к стенду"
    assert "httpx" not in source
    assert "apis." not in source


def test_report_screen_has_no_run_commands():
    """Отчёт не запускает проверки: запуск один, на «Прогоне» (`IR-P-4`)."""
    source = pathlib.Path(str(scr404_report.__file__)).read_text(encoding="utf-8")

    for command in RUN_COMMANDS:
        assert command not in source, f"в отчёте найдена команда запуска {command}"
    assert "queue.begin(" not in source
    assert "results_api.record(" not in source, "отчёт не пишет результаты (`DR-P-5`)"
