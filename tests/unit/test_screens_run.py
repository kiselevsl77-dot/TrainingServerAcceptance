"""Тесты экранов испытаний: «Прогон» (`SCR-301`) и «Карточка проверки» (`SCR-302`).

Эти два экрана — рабочее место инженера-испытателя, и здесь держатся три правила, из которых
потом складывается протокол:

    * маркеры очереди различимы (`IR-P-3`), а команда запуска одна и называет пункт (`IR-P-4`);
    * изменяющие проверки не уходят на стенд без карточки запуска (`FR-P-19`);
    * карточка проверки **не** вводит собственной команды запуска: она умеет отметку оператора
      (`FR-P-32`) и чтение следа (`IR-P-5`).

Логика экранов вынесена в чистые функции, поэтому проверяется без Streamlit и без стенда.
"""

from __future__ import annotations

import pathlib

from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance import runner as runner_api
from acceptance import sets as sets_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme
from acceptance.session import new_session
from acceptance.ui.screens import scr301_run, scr302_check

#: Команды запуска: в карточке проверки их быть не должно (`IR-P-4`).
RUN_COMMANDS = ("execute_next", "run_batch_tech", "auto_step", "execute_call", "execute_check")

#: Изменяющая проверка (нужна карточка запуска) и безопасная (карточка не нужна).
LIVE_CHECK = "TC-FILE-13"
TECH_CHECK = "TC-SYS-01"


def _programme(*set_ids: str) -> Programme:
    """Программа из наборов библиотеки (объединение по ИЛИ)."""
    library = sets_api.library_from_catalog()
    ids = list(set_ids) or [sets_api.SMOKE_SET_ID]
    return Programme.from_sets(library, ids)


def _queue(programme: Programme) -> queue_api.Queue:
    """Очередь прогона — снимок ревизии программы."""
    return queue_api.build_queue(programme)


# ---------------------------------------------------------------------------
# SCR-301: очередь, маркеры и готовность шага
# ---------------------------------------------------------------------------
def test_queue_table_rows_mark_next_item():
    """Таблица очереди показывает маркер «следующая» и состояние пункта (`IR-P-3`)."""
    programme = _programme()
    queue = _queue(programme)
    rows = scr301_run.queue_table_rows(queue, programme)
    first = rows[0]

    assert first["marker"] == "▷ следующая"
    assert first["state"].endswith("ожидает")
    assert first["check_id"] == queue.next_item().check_id
    assert first["check_class"], "класс проверки показывается словами"
    assert first["result"] == "", "результата ещё нет — колонка пуста, а не выдумана"


def test_queue_table_rows_show_markers_of_progress():
    """Маркеры различают «сейчас» и «последняя» после начала прогона."""
    programme = _programme()
    queue = _queue(programme)
    first = str(queue.next_item().check_id)

    queue.begin(first)
    started = scr301_run.queue_table_rows(queue, programme)
    assert {row["check_id"]: row["marker"] for row in started}[first] == "▶ сейчас"

    queue.finish(first)
    rows = scr301_run.queue_table_rows(queue, programme)
    markers = {row["check_id"]: row["marker"] for row in rows}

    assert markers[first] == "🕘 последняя"
    assert rows[1]["marker"] == "▷ следующая"


def test_queue_table_rows_are_limited():
    """Таблица очереди не разворачивается на сотни строк: лишнее видно счётчиком."""
    programme = _programme()
    queue = _queue(programme)

    assert len(scr301_run.queue_table_rows(queue, programme, limit=2)) == 2
    assert scr301_run.queue_table_rows(queue, programme, limit=0) == []


def test_progress_caption_counts_outcomes():
    """KPI прогона: сколько выполнено и чем закончилось."""
    programme = _programme()
    queue = _queue(programme)
    first = str(queue.next_item().check_id)
    session = new_session(base_url="http://test.local")
    results_api.record(
        session,
        {"check_id": first, "status": str(CheckStatus.PASSED), "verdict": "ок"},
    )
    queue.finish(first)

    caption = scr301_run.progress_caption(queue, results_api.state(session))

    assert f"выполнено 1 из {queue.size}" in caption
    assert "успех 1" in caption


def test_next_note_names_the_item_and_run_card_requirement():
    """«Следующая» называет пункт, а для изменяющей проверки — требование карточки (`FR-P-19`)."""
    programme = Programme()
    programme.add_check(LIVE_CHECK)
    queue = _queue(programme)
    note = scr301_run.next_note(queue, programme)

    assert note.startswith(f"Следующая: {LIVE_CHECK}")
    assert "карточка запуска" in note

    tech = scr301_run.next_note(_queue(_programme()), _programme())
    assert tech.startswith(f"Следующая: {TECH_CHECK}")
    assert "карточка запуска" not in tech


def test_next_note_reports_finished_queue():
    """Пройденная очередь говорит об этом прямо, а не молчит."""
    programme = _programme()
    queue = _queue(programme)
    for check_id in queue.check_ids:
        queue.begin(check_id)
        queue.finish(check_id)

    assert scr301_run.next_note(queue, programme).startswith("Очередь пройдена")


def test_feed_label_filters_feed():
    """Лента обмена фильтруется по последней или следующей проверке."""
    programme = _programme()
    queue = _queue(programme)
    first = str(queue.next_item().check_id)
    queue.begin(first)
    queue.finish(first)
    following = queue.next_item()

    assert scr301_run.feed_label(scr301_run.FEED_ALL, queue) == ""
    assert scr301_run.feed_label(scr301_run.FEED_LAST, queue) == first
    assert scr301_run.feed_label(scr301_run.FEED_NEXT, queue) == following.check_id


def test_mode_index_follows_queue_mode():
    """Переключатель режима показывает текущий режим очереди."""
    queue = _queue(_programme())
    modes = list(queue_api.MODE_LABELS)

    assert scr301_run.mode_index(queue) == modes.index(queue_api.MODE_CHECK)
    queue.mode = queue_api.MODE_CALL
    assert scr301_run.mode_index(queue) == modes.index(queue_api.MODE_CALL)


def test_payload_lines_show_created_data_and_reads():
    """Предпросмотр payload честно различает создание данных и чтение (`FR-P-20`)."""
    session = new_session(base_url="http://test.local")

    upload = scr301_run.payload_lines(session, LIVE_CHECK, {})
    read_only = scr301_run.payload_lines(session, TECH_CHECK, {})

    assert any("__TEST__" in line for line in upload)
    assert any("не создаёт" in line for line in read_only)
    assert "отсутствует в каталоге" in scr301_run.payload_lines(session, "TC-UNKNOWN-99", {})[0]


def test_call_step_note_names_next_call():
    """Режим «по вызовам» показывает, какой вызов уйдёт следующим."""
    note = scr301_run.call_step_note(LIVE_CHECK, 0)

    assert note.startswith("вызов 1 из ")
    assert "POST" in note or "GET" in note


def test_outcome_lines_and_feedback_describe_step():
    """Вердикт последнего шага и сообщение о шаге «пачки tech» читаются как отчёт."""
    summary = runner_api.RunOutcome(
        check_id=TECH_CHECK,
        status=str(CheckStatus.PASSED),
        verdict="GET /health отвечает 200",
        journal_from=214,
        journal_to=214,
        duration_ms=41.2,
    ).summary()

    lines = scr301_run.outcome_lines(summary)

    assert lines[0].startswith(TECH_CHECK)
    assert "214" in lines[2]
    assert scr301_run.outcome_lines(None)[0] == "Прогон ещё не выполнялся."
    assert scr301_run.summary_feedback([summary]).startswith(TECH_CHECK)
    assert scr301_run.summary_feedback([summary, summary]).startswith("пачка tech: шагов 2")


def test_run_screen_has_single_run_command_source():
    """Единственная команда запуска описана в ядре, а не собирается на экране (`IR-P-4`)."""
    source = pathlib.Path(str(scr301_run.__file__)).read_text(encoding="utf-8")

    assert "runner_api.next_label(queue)" in source, "подпись команды берётся из ядра"
    assert 'f"▶ Следующая: {check_id}"' not in source, "экран не собирает подпись команды сам"


# ---------------------------------------------------------------------------
# SCR-302: выбор пункта, соседи, доказательства и отметка оператора
# ---------------------------------------------------------------------------
def test_selected_check_prefers_chosen_then_just_run():
    """Карточка показывает выбранный пункт, иначе только что прогнанный, иначе следующий."""
    programme = _programme()
    queue = _queue(programme)
    session = new_session(base_url="http://test.local")
    ids = queue.check_ids

    assert scr302_check.selected_check(session, queue, ids[1]) == ids[1]
    assert scr302_check.selected_check(session, queue, "TC-UNKNOWN-99") == ids[0]
    assert scr302_check.selected_check(session, queue, "") == ids[0]

    queue.begin(ids[0])
    queue.finish(ids[0])

    assert scr302_check.selected_check(session, queue, "") == ids[0], (
        "после прогона карточка показывает его результат, а не следующий пункт"
    )

    for check_id in ids[1:]:
        queue.begin(check_id)
        queue.finish(check_id)

    assert scr302_check.selected_check(session, queue, "") == ids[-1]


def test_neighbour_ids_walk_the_queue():
    """Соседние пункты очереди: с краёв переходов нет — «зацикливания» не бывает."""
    assert scr302_check.neighbour_ids(["A", "B", "C"], "B") == ("A", "C")
    assert scr302_check.neighbour_ids(["A", "B", "C"], "A") == ("", "B")
    assert scr302_check.neighbour_ids(["A", "B", "C"], "C") == ("B", "")
    assert scr302_check.neighbour_ids(["A", "B"], "X") == ("", "")


def test_header_lines_describe_place_and_confirmation():
    """Заголовок карточки называет класс, требования, подтверждение и позицию в программе."""
    library = sets_api.library_from_catalog()
    programme = _programme(sets_api.SMOKE_SET_ID, library.by_section("TC-SYS")[0].set_id)
    queue = _queue(programme)
    spec = catalog.find(TECH_CHECK)
    session = new_session(base_url="http://test.local")
    row = results_api.row(session, TECH_CHECK)

    lines = scr302_check.header_lines(spec, programme, queue, row)

    assert "Модуль:" in lines[0]
    assert "без карточки запуска" in lines[0], "tech-проверка не требует карточки запуска"
    assert lines[1].startswith("Ожидание:")
    assert "позиция 1 из" in lines[2]
    assert "результат: не выполнена" in lines[2]


def test_live_check_header_requires_run_card():
    """Изменяющая проверка помечена как требующая карточки запуска (`FR-P-19`)."""
    programme = _programme()
    programme.add_check(LIVE_CHECK)
    queue = _queue(programme)
    spec = catalog.find(LIVE_CHECK)
    row = results_api.row(new_session(base_url="http://test.local"), LIVE_CHECK)

    assert "подтверждение обязательно" in scr302_check.header_lines(spec, programme, queue, row)[0]


def test_evidence_rows_flatten_evidence():
    """Доказательства показываются полями: словарь превращается в JSON-строку."""
    row = {
        "evidence": {
            "endpoint": "get /health",
            "run_card": {"data": "__TEST__", "confirm": True},
            "empty": None,
        }
    }

    rows = {item["field"]: item["value"] for item in scr302_check.evidence_rows(row)}

    assert rows["endpoint"] == "get /health"
    assert '"confirm": true' in rows["run_card"]
    assert rows["empty"] == ""
    assert scr302_check.evidence_rows({}) == []


def test_mark_payload_keeps_operator_conclusion():
    """Ручная отметка несёт статус и заключение оператора (`FR-P-32`)."""
    payload = scr302_check.mark_payload("tc-sys-06", str(CheckStatus.MANUAL_OK), "сверил вручную")

    assert payload["check_id"] == "TC-SYS-06"
    assert payload["status"] == str(CheckStatus.MANUAL_OK)
    assert payload["operator_note"] == "сверил вручную"

    empty = scr302_check.mark_payload(TECH_CHECK, str(CheckStatus.SKIPPED), "")
    assert empty["verdict"] == str(CheckStatus.SKIPPED)


def test_mark_options_are_manual_statuses():
    """Отметить можно только исходы, которые ставит человек."""
    assert str(CheckStatus.MANUAL_OK) in scr302_check.MARK_OPTIONS
    assert str(CheckStatus.SKIPPED) in scr302_check.MARK_OPTIONS
    assert str(CheckStatus.INTERRUPTED) in scr302_check.MARK_OPTIONS
    assert str(CheckStatus.PASSED) not in scr302_check.MARK_OPTIONS


def test_check_card_has_no_run_command():
    """Карточка проверки не вводит второй команды запуска (`IR-P-4`)."""
    source = pathlib.Path(str(scr302_check.__file__)).read_text(encoding="utf-8")

    for command in RUN_COMMANDS:
        assert command not in source, f"в карточке проверки найдена команда запуска {command}"
    assert "→ К прогону" in source, "карточка ведёт к прогону, но сама проверку не запускает"


def test_run_screens_do_not_store_status_outside_results():
    """Экраны испытаний не пишут статус мимо `results.py` (`DR-P-5`)."""
    for module in (scr301_run, scr302_check):
        source = pathlib.Path(str(module.__file__)).read_text(encoding="utf-8")
        assert "session.checks.append" not in source
        assert "results_api.record(" in source or "runner_api." in source
