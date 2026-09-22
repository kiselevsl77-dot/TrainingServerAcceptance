"""Тесты экрана «Обзор испытаний» (`SCR-201`).

Обзор отвечает на вопрос «где я и что требует внимания» (`FR-P-11`, `FR-P-12`, `FR-P-48`):
фаза процесса, программа и покрытие, прогресс очереди, готовность отчёта, состояние стенда и
панель «Внимание».

Ключевое свойство: фаза и «Внимание» выводятся из состояния сессии, а не из отдельного поля,
поэтому их нельзя «переключить» вручную — пульт показывает, что действительно сделано.
Команд запуска у экрана нет (`IR-P-4`): он только показывает и ведёт.
"""

from __future__ import annotations

import pathlib

from acceptance import notes as notes_api
from acceptance import results as results_api
from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme
from acceptance.queue import build_queue
from acceptance.session import TestSession as SessionModel
from acceptance.session import add_test_entity, mark_test_entity_deleted, new_session, set_snapshot
from acceptance.ui.screens import scr201_overview

#: Команды запуска: в обзоре их быть не должно (`IR-P-4`).
RUN_COMMANDS = (
    "execute_next",
    "run_batch_tech",
    "auto_step",
    "execute_call",
    "execute_check",
    "from acceptance.runner",
)


def _programme() -> Programme:
    """Программа из смоук-набора (для блока «программа и объём»)."""
    library = sets_api.library_from_catalog()
    return Programme.from_sets(library, [sets_api.SMOKE_SET_ID])


def _empty() -> SessionModel:
    """Пустая сессия: ничего не сделано — фаза Ф0."""
    return new_session(base_url="http://test.local")


def _running() -> SessionModel:
    """Сессия с выполненной проверкой: прогон начат — фаза Ф2."""
    session = _empty()
    session.started_at = session_api.now_iso()
    results_api.record(
        session,
        {"check_id": "TC-SYS-01", "status": str(CheckStatus.PASSED), "verdict": "ок"},
    )
    return session


# ---------------------------------------------------------------------------
# Фаза процесса (`FR-P-11`)
# ---------------------------------------------------------------------------
def test_phase_follows_session_state():
    """Фаза выводится из состояния сессии: без сессии — Ф0, с прогоном — Ф2 (`FR-P-11`)."""
    assert scr201_overview.phase_index(None) == 0
    assert scr201_overview.phase_index(_empty()) == 0
    assert scr201_overview.phase_index(_running()) == 2

    session = _running()
    session.notes.append(notes_api.new_note("Дефект").to_dict())
    assert scr201_overview.phase_index(session) == 3

    session.info.conclusion = "годен"
    assert scr201_overview.phase_index(session) == 4


def test_phase_rows_mark_done_current_and_todo():
    """Полоса фаз помечает пройденные, текущую и предстоящие (`FR-P-11`)."""
    rows = scr201_overview.phase_rows(2)

    assert [row["phase"] for row in rows] == [code for code, _ in scr201_overview.PHASES]
    assert [row["mark"] for row in rows] == ["✅", "✅", "●", "○", "○"]
    assert "Ф2" in scr201_overview.phase_note(2)
    assert "финализация" in scr201_overview.phase_note(99), "индекс вне списка не ломает подпись"


# ---------------------------------------------------------------------------
# Прогресс очереди и покрытие (`FR-P-12`, `IR-P-19`)
# ---------------------------------------------------------------------------
def test_progress_rows_count_outcomes():
    """Прогресс очереди: сколько выполнено и с каким исходом (`FR-P-12`)."""
    programme = _programme()
    session = _running()
    for check_id in programme.check_ids[:3]:
        results_api.record(session, {"check_id": check_id, "status": str(CheckStatus.PASSED)})
    queue = build_queue(programme)
    summary = queue.summary(results_api.state(session))

    rows = scr201_overview.progress_rows(summary)

    assert rows[0]["what"] == "выполнено"
    assert f"из {summary['total']}" in rows[0]["value"]
    assert "успех" in rows[0]["detail"] and "осталось" in rows[0]["detail"]


def test_progress_rows_show_empty_queue():
    """Пустая очередь показывает «нет пунктов», а не выдуманный прогресс (`FR-P-12`)."""
    rows = scr201_overview.progress_rows({"total": 0})

    assert rows[0]["value"] == "нет пунктов"


def test_coverage_note_reports_sections():
    """Покрытие разделов: сколько разделов закрыто и что сокращено (`IR-P-19`)."""
    assert "Программа не собрана" in scr201_overview.coverage_note(Programme())

    note = scr201_overview.coverage_note(_programme())

    assert "Покрытие разделов:" in note
    assert "из" in note


# ---------------------------------------------------------------------------
# Панель «Внимание» (`FR-P-48`)
# ---------------------------------------------------------------------------
def test_attention_rows_show_missing_snapshot_and_notes():
    """«Внимание» показывает, что мешает подписанию: снимки, замечания, уборка (`FR-P-48`)."""
    session = _empty()
    rows = {row["what"]: row for row in scr201_overview.attention_rows(session)}

    assert rows["Снимок «Окончание»"]["ok"] is False
    assert rows["Открытые P0"]["value"] == 0
    assert "Снимок «Окончание»" in scr201_overview.attention_note(session)

    set_snapshot(session, session_api.SNAP_END, {"files": 1})
    session.notes.append(
        {**notes_api.new_note("Дефект", priority="P0").to_dict(), "reproduction": ""}
    )
    add_test_entity(session, entity_id="__TEST__f-1", entity_type="file", check_id="TC-SYS-01")
    rows = {row["what"]: row for row in scr201_overview.attention_rows(session)}

    assert rows["Снимок «Окончание»"]["ok"] is True
    assert rows["Открытые P0"]["value"] == 1
    assert rows["Замечания без воспроизведения"]["value"] == 1
    assert rows["`__TEST__`-сущности не удалены"]["value"] == 1

    mark_test_entity_deleted(session, "__TEST__f-1", check_id="TC-SYS-01")
    rows = {row["what"]: row for row in scr201_overview.attention_rows(session)}
    assert rows["`__TEST__`-сущности не удалены"]["ok"] is True


def test_attention_note_is_quiet_when_all_closed():
    """Когда всё закрыто, панель «Внимание» молчит, а не пугает оператора (`FR-P-48`)."""
    session = _empty()
    set_snapshot(session, session_api.SNAP_END, {"files": 1})

    assert scr201_overview.attention_note(session) == "Внимание: не требуется — все пункты закрыты"
    assert all(row["ok"] for row in scr201_overview.attention_rows(session))


def test_attention_table_marks_states():
    """Таблица «Внимание» помечает состояние пунктов (`IR-P-8`)."""
    session = _empty()
    rows = scr201_overview.attention_table(scr201_overview.attention_rows(session))

    assert {row["mark"] for row in rows} <= {scr201_overview.DONE, scr201_overview.MISSING}
    assert {row["state"] for row in rows} == {"выполнено", "требует внимания"}
    assert scr201_overview.status_mark(True) == scr201_overview.DONE
    assert scr201_overview.status_label(False) == "требует внимания"


# ---------------------------------------------------------------------------
# Шапка, стенд и запреты экрана
# ---------------------------------------------------------------------------
def test_session_and_stand_notes_describe_state():
    """Шапка и блок стенда называют сессию, реквизиты, сборку и объём обмена (`FR-P-12`)."""
    assert scr201_overview.session_note(None) == "сессия не выбрана"

    session = _running()
    session.info.title = "Приёмка API"
    session.server_version = {"branch": "dev", "revision": "83319ae1234"}

    header = scr201_overview.session_note(session)

    assert "Приёмка API" in header and "dev@83319ae123" in header
    assert "реквизиты не заполнены" in header
    assert "обменов за сессию: 0" in scr201_overview.stand_note(session, [])


def test_overview_screen_has_no_run_commands():
    """Обзор не запускает проверки: запуск один, на «Прогоне» (`IR-P-4`)."""
    source = pathlib.Path(str(scr201_overview.__file__)).read_text(encoding="utf-8")

    for command in RUN_COMMANDS:
        assert command not in source, f"в обзоре найдена команда запуска {command}"
    assert "queue.begin(" not in source
    assert "results_api.record(" not in source, "обзор не пишет результаты (`DR-P-5`)"
    assert "queue.finish(" not in source
