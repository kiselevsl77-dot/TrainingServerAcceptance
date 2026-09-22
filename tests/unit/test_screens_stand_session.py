"""Тесты экранов «Сессия испытаний» (`SCR-203`) и «Стенд» (`SCR-202`).

Экраны подготовки отвечают за контекст испытаний: реквизиты, которые попадут в шапку
отчёта, жизненный цикл сессии (черновик → «идёт» → «завершена» → переоткрытие,
`FR-P-2…FR-P-5`), историю событий (`FR-P-55`), передачу файла сессии (`FR-P-52`) и
состояние сборки стенда (`FR-P-1`, `FR-P-6`). Логика экранов живёт в чистых функциях,
поэтому проверяется без Streamlit и без живого стенда.
"""

from __future__ import annotations

from acceptance.session import SessionInfo, new_session
from acceptance.ui.screens import scr202_stand, scr203_session


# ---------------------------------------------------------------------------
# SCR-203: реквизиты, жизненный цикл, история
# ---------------------------------------------------------------------------
def test_readiness_counts_required_fields():
    """Готовность реквизитов: чего не хватает и сколько процентов заполнено."""
    empty = scr203_session.readiness(SessionInfo())
    assert empty["percent"] == 0
    assert empty["complete"] is False
    assert empty["missing"] == ["title", "object_of_test", "program_doc"]

    filled = scr203_session.readiness(
        SessionInfo(title="Приёмка", object_of_test="dev@83319ae", program_doc="ПМИ-2026")
    )
    assert filled["complete"] is True
    assert filled["missing"] == []
    assert filled["percent"] > 0


def test_readiness_percent_grows_with_fields():
    """Каждое заполненное поле увеличивает готовность: реквизиты дозаполняются по ходу."""
    one = scr203_session.readiness(SessionInfo(title="Приёмка"))
    two = scr203_session.readiness(SessionInfo(title="Приёмка", customer="Энергомера"))

    assert 0 < one["percent"] < two["percent"] < 100


def test_actions_follow_session_status():
    """Черновик можно начать и завершить, идущую — завершить, завершённую — переоткрыть."""
    assert scr203_session.actions_for("черновик") == ("start", "close")
    assert scr203_session.actions_for("идёт") == ("close",)
    assert scr203_session.actions_for("завершена") == ("reopen",)
    assert scr203_session.actions_for("неизвестный статус") == ("start", "close")


def test_history_rows_show_newest_first_and_filter():
    """История событий: свежие сверху, фильтр по подстроке, лимит строк."""
    session = new_session(base_url="http://test.local")
    session.add_history("session_started", "Сессия начата")
    session.add_history("stand_snapshot", "Снимок стенда (start)")
    session.add_history("check_recorded", "Проверка TC-SYS-01: успех")

    rows = scr203_session.history_rows(session)

    assert rows[0]["event"] == "check_recorded"
    assert rows[-1]["event"] == "session_created"
    assert [row["event"] for row in scr203_session.history_rows(session, needle="снимок")] == [
        "stand_snapshot"
    ]
    assert len(scr203_session.history_rows(session, limit=2)) == 2


def test_session_picker_rows_are_ready_for_table():
    """Строки списка сессий: идентификатор, статус, наименование, число проверок."""
    rows = scr203_session.session_picker_rows(
        [
            {
                "session_id": "20260921-101105-2f6a",
                "status": "идёт",
                "title": "Приёмка",
                "object_of_test": "dev@83319ae",
                "checks_total": 41,
                "created_at": "2026-09-21T10:11:05",
            }
        ]
    )

    assert rows[0]["session_id"] == "20260921-101105-2f6a"
    assert rows[0]["checks_total"] == 41


def test_snapshot_labels_delegate_to_component():
    """Снимки сессии на экране берутся из компонента снимков (одна реализация)."""
    session = new_session(base_url="http://test.local")
    session.snapshots["start"] = {"at": "2026-09-21T10:12:00"}

    assert scr203_session.snapshot_labels(session) == [
        ("Начало", "2026-09-21T10:12:00"),
        ("Окончание", ""),
    ]


def test_build_warning_delegates_to_component():
    """Предупреждение о расхождении сборок считает компонент, экран его показывает."""
    session = new_session(
        base_url="http://test.local", server_version={"branch": "dev", "revision": "83319ae"}
    )

    assert scr203_session.build_warning(session, "dev@83319ae") == ""
    assert "main@111" in scr203_session.build_warning(session, "main@111")


# ---------------------------------------------------------------------------
# SCR-202: сборка стенда, журналы, состояние проверки связи
# ---------------------------------------------------------------------------
def test_logs_rows_list_all_three_files():
    """Журналы экрана: структурный журнал сессии, журнал запуска и файл сессии."""
    session = new_session(base_url="http://test.local")
    session.logs = {"session_log": "logs/session_x.jsonl", "app_log": "logs/applog/applog_x.log"}

    rows = scr202_stand.logs_rows(session)

    assert [row["journal"] for row in rows] == [
        "журнал сессии (JSONL)",
        "журнал запуска пульта",
        "файл сессии",
    ]
    assert rows[0]["path"].endswith("session_x.jsonl")
    assert rows[2]["path"].endswith(".json")
    assert scr202_stand.logs_rows(None) == []


def test_check_caption_names_time_and_build():
    """Подпись проверки связи: пиктограмма, время и сборка стенда."""
    payload = {
        "at": "2026-09-21T10:12:00",
        "health": True,
        "version": {"branch": "dev", "revision": "83319ae1234"},
    }

    assert scr202_stand.check_caption(payload) == "✅ · 21.09 10:12 · dev@83319ae1234"
    assert scr202_stand.check_caption(None) == "проверка ещё не выполнялась"
    assert scr202_stand.check_caption({"at": "2026-09-21T10:12:00"}) == "⚠ · 21.09 10:12"


def test_build_note_is_silent_without_session():
    """Без сессии сравнивать не с чем: экран не выдумывает предупреждение."""
    assert scr202_stand.build_note(None, "dev@83319ae") == ""

    session = new_session(
        base_url="http://test.local", server_version={"branch": "dev", "revision": "83319ae"}
    )
    assert "main@222" in scr202_stand.build_note(session, "main@222")
