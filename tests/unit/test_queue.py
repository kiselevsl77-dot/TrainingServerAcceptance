"""Тесты очереди прогона (`acceptance/queue.py`).

Проверяются свойства, на которых держатся требования «Прогона»: очередь — снимок
утверждённой ревизии программы (`FR-P-13`), маркеры «следующая/выполняется/
последняя» (`IR-P-20`), снятие пункта с причиной как результат (`FR-P-15`),
запрет правки состава в прогоне (`IR-P-18`) и хранение очереди в сессии (v7).
"""

from __future__ import annotations

from typing import Any

import pytest

from acceptance import programme as programme_api
from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance import sets as sets_api
from acceptance.session import new_session


@pytest.fixture()
def library() -> sets_api.SetsLibrary:
    """Стартовая библиотека: набор на каждый раздел плюс «Смоук-минимум»."""
    return sets_api.library_from_catalog(author="Иванов И.И.")


@pytest.fixture()
def programme(library: sets_api.SetsLibrary) -> programme_api.Programme:
    """Черновик программы: раздел «Система» плюс смоук-минимум (с пересечением)."""
    return programme_api.Programme.from_sets(library, ["SET-SYS", sets_api.SMOKE_SET_ID])


@pytest.fixture()
def approved(programme: programme_api.Programme) -> programme_api.Programme:
    """Утверждённая программа (с обоснованием сокращения: покрыты не все разделы)."""
    programme.approve(
        by="Иванов И.И.", comment="приёмка", reduction="смоук-минимум и раздел системы"
    )
    return programme


@pytest.fixture()
def session() -> Any:
    """Сессия испытаний без записи на диск."""
    return new_session(base_url="https://stand.local")


def _item(queue: queue_api.Queue, check_id: str) -> queue_api.QueueItem:
    """Пункт очереди (в тестах пункт обязан быть в очереди)."""
    found = queue.find(check_id)
    assert found is not None, f"пункт {check_id} не найден в очереди"
    return found


def _next(queue: queue_api.Queue) -> queue_api.QueueItem:
    """Следующий пункт очереди (в тестах он обязан быть)."""
    found = queue.next_item()
    assert found is not None, "в очереди нет ожидающих пунктов"
    return found


def test_build_queue_snapshots_programme(approved: programme_api.Programme):
    """Очередь повторяет состав и порядок ревизии программы (снимок, `FR-P-13`)."""
    queue = queue_api.build_queue(approved, author="Петров П.П.")

    assert queue.check_ids == approved.check_ids
    assert [item.order for item in queue.items] == [item.order for item in approved.items]
    assert queue.revision == approved.revision
    assert queue.approved is True
    assert queue.draft is False
    assert queue.author == "Петров П.П."
    assert queue.is_current(approved)


def test_draft_programme_marks_queue(programme: programme_api.Programme):
    """Прогон по черновику программы разрешён, но очередь это помечает (`A1`)."""
    queue = queue_api.build_queue(programme)

    assert queue.draft is True
    assert queue.approved is False
    assert queue.summary()["draft"] is True


def test_markers_show_next_running_and_last(approved: programme_api.Programme):
    """Маркеры очереди: «следующая», «выполняется», «последняя» (`IR-P-20`)."""
    queue = queue_api.build_queue(approved)
    first, second = queue.check_ids[0], queue.check_ids[1]

    assert queue.marker(first) == queue_api.MARKER_NEXT
    assert queue.marker(second) == ""

    queue.begin(first)
    assert queue.marker(first) == queue_api.MARKER_RUNNING
    current = queue.current()
    assert current is not None and current.check_id == first

    queue.finish(first)
    assert queue.marker(first) == queue_api.MARKER_LAST
    assert queue.marker(second) == queue_api.MARKER_NEXT
    last = queue.last()
    assert last is not None and last.check_id == first


def test_next_item_skips_done_and_off(approved: programme_api.Programme):
    """«Следующая» — первый ожидающий пункт: выполненные и снятые пропускаются."""
    queue = queue_api.build_queue(approved)
    ids = queue.check_ids

    queue.begin(ids[0])
    queue.finish(ids[0])
    queue.take_off(ids[1], "нет данных для прогона")

    following = queue.next_item()
    assert following is not None
    assert following.check_id == ids[2]
    assert queue.pending_ids == ids[2:]


def test_finish_sets_journal_range_and_pointer(approved: programme_api.Programme):
    """Закрытие пункта фиксирует диапазон журнала и двигает указатель прогона."""
    queue = queue_api.build_queue(approved)
    first = queue.check_ids[0]

    queue.begin(first)
    item = queue.finish(first, journal_from=214, journal_to=217, note="ответ 200")

    assert item is not None
    assert item.state == queue_api.QUEUE_DONE
    assert item.journal_range == "#214–#217"
    assert item.note == "ответ 200"
    assert queue.pointer == 1
    assert queue.summary()["done"] == 1


def test_take_off_requires_reason(approved: programme_api.Programme):
    """Снятие без причины невозможно (`FR-P-15`, `AC-P-15`)."""
    queue = queue_api.build_queue(approved)

    for reason in ("", "   "):
        with pytest.raises(ValueError):
            queue.take_off(queue.check_ids[0], reason)

    assert _next(queue).check_id == queue.check_ids[0]


def test_take_off_is_a_result_and_programme_untouched(
    approved: programme_api.Programme, session: Any
):
    """Снятие — результат прогона: программа и её ревизия не меняются (`FR-P-15`)."""
    queue = queue_api.build_queue(approved)
    first = queue.check_ids[0]
    revision = approved.revision

    item = queue.take_off(first, "данные стенда заняты", session=session, author="Петров П.П.")

    assert item is not None and item.state == queue_api.QUEUE_OFF
    assert item.reason == "данные стенда заняты"
    assert results_api.is_suspended(session, first)
    assert results_api.row(session, first)["reason"] == "данные стенда заняты"
    assert approved.check_ids == queue.check_ids
    assert approved.revision == revision


def test_return_to_queue_removes_suspension(approved: programme_api.Programme, session: Any):
    """Возврат пункта убирает результат снятия и снова делает пункт ожидающим."""
    queue = queue_api.build_queue(approved)
    first = queue.check_ids[0]
    queue.take_off(first, "нет подтверждения заказчика", session=session)

    item = queue.return_to_queue(first, session=session, author="Петров П.П.")

    assert item is not None and item.state == queue_api.QUEUE_PENDING
    assert item.reason == ""
    assert not results_api.is_suspended(session, first)
    assert results_api.result_of(session, first) is None
    assert _next(queue).check_id == first


def test_reset_clears_run_state_but_keeps_composition(
    approved: programme_api.Programme, session: Any
):
    """Новый прогон: отметки сняты, состав очереди неприкосновенен."""
    queue = queue_api.build_queue(approved)
    ids = queue.check_ids
    queue.begin(ids[0])
    queue.finish(ids[0], journal_from=1, journal_to=2)
    queue.take_off(ids[1], "вне контура", session=session)

    queue.reset(session=session)

    assert queue.check_ids == ids
    assert all(item.is_pending for item in queue.items)
    assert all(item.reason == "" and item.journal_range == "" for item in queue.items)
    assert queue.pointer == 0
    assert queue.last_check_id == ""
    assert results_api.result_of(session, ids[1]) is None


def test_queue_has_no_composition_editing_methods():
    """Состав очереди не правится в прогоне: методов правки нет (`IR-P-18`)."""
    forbidden = ("add", "remove", "exclude", "move", "reorder", "set_mandatory", "append")

    for name in forbidden:
        assert not hasattr(queue_api.Queue, name), f"у очереди не должно быть {name}()"

    assert sorted(queue_api.Queue.__dataclass_fields__) == [
        "approved",
        "author",
        "created_at",
        "draft",
        "items",
        "last_check_id",
        "mode",
        "pause_seconds",
        "pointer",
        "revision",
        "signature",
        "stop_reason",
    ]


def test_is_current_detects_composition_change(library: sets_api.SetsLibrary):
    """Расхождение очереди и программы ловится отпечатком состава, а не «на глаз»."""
    draft = programme_api.Programme.from_sets(library, ["SET-SYS"])
    queue = queue_api.build_queue(draft)

    assert queue.is_current(draft)
    # тот же состав в отдельной программе — очередь актуальна
    assert queue.is_current(programme_api.Programme.from_sets(library, ["SET-SYS"]))

    optional = next(item for item in draft.items if not item.mandatory)
    draft.exclude([optional.check_id])

    assert not queue.is_current(draft)
    assert not queue.is_current(programme_api.Programme.from_sets(library, ["SET-REC"]))


def test_queue_rows_take_status_from_results(approved: programme_api.Programme, session: Any):
    """Таблица очереди берёт статус из результатов (`DR-P-5`), а не хранит его."""
    queue = queue_api.build_queue(approved)
    first = queue.check_ids[0]
    results_api.record(
        session, {"check_id": first, "status": "успех", "verdict": "сервер отвечает"}
    )

    rows = queue.rows(results_api.state(session))

    assert rows[0]["check_id"] == first
    assert rows[0]["status"] == "успех"
    assert rows[0]["icon"] == "✅"
    assert rows[0]["verdict"] == "сервер отвечает"
    assert rows[0]["marker"] == queue_api.MARKER_NEXT
    assert rows[1]["status"] == results_api.STATUS_NOT_RUN


def test_queue_summary_counts_states(approved: programme_api.Programme):
    """KPI очереди: сколько пройдено, снято и осталось."""
    queue = queue_api.build_queue(approved)
    ids = queue.check_ids
    queue.begin(ids[0])
    queue.finish(ids[0])
    queue.take_off(ids[1], "по решению комиссии")

    summary = queue.summary()

    assert summary["total"] == len(ids)
    assert summary["done"] == 1
    assert summary["off"] == 1
    assert summary["pending"] == len(ids) - 2
    assert summary["next"] == ids[2]
    assert summary["last"] == ids[0]


def test_mode_and_pause_are_validated(approved: programme_api.Programme):
    """Режим и пауза прогона живут в очереди и приводятся к допустимым значениям."""
    queue = queue_api.build_queue(approved)

    assert queue.mode == queue_api.MODE_CHECK
    assert queue.set_mode(queue_api.MODE_CALL) == queue_api.MODE_CALL
    assert "вызов" in queue.mode_label
    assert queue.set_pause(1000) == queue_api.MAX_PAUSE_SECONDS
    assert queue.set_pause("нет") == queue_api.DEFAULT_PAUSE_SECONDS
    # неизвестный режим откатывается к основному — «проверка целиком»
    assert queue.set_mode("чепуха") == queue_api.MODE_CHECK
    assert "сценари" in queue.mode_label


def test_to_dict_round_trip_tolerates_broken_data(approved: programme_api.Programme):
    """Восстановление очереди: неизвестный режим, состояние и пауза не ломают чтение."""
    queue = queue_api.build_queue(approved)
    queue.begin(queue.check_ids[0])
    payload = queue.to_dict()
    payload["mode"] = "нечто"
    payload["pause_seconds"] = 1000
    payload["items"][0]["state"] = "непонятно"
    payload["items"].append("мусор")

    restored = queue_api.Queue.from_dict(payload)

    assert restored.check_ids == queue.check_ids
    assert restored.mode == queue_api.MODE_CHECK
    assert restored.pause_seconds == queue_api.MAX_PAUSE_SECONDS
    assert _item(restored, restored.check_ids[0]).state == queue_api.QUEUE_PENDING
    assert restored.signature == queue.signature


def test_load_and_save_queue_in_session(approved: programme_api.Programme, session: Any):
    """Очередь живёт в сессии (схема v7) и восстанавливается из неё."""
    assert queue_api.load_queue(session).is_empty

    queue = queue_api.build_queue(approved)
    queue_api.save_queue(session, queue, event="queue_built", message="Очередь собрана")

    restored = queue_api.load_queue(session)
    assert restored.check_ids == approved.check_ids
    assert restored.revision == approved.revision
    assert session.queue["approved"] is True
    assert any(entry.get("event") == "queue_built" for entry in session.history)
