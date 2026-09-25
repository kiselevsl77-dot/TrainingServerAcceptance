"""Тесты состояния прогона (`acceptance/run_state.py`).

Проверяется правило «выполнено по факту запроса» (решение заказчика 25.09.2026,
`llm_tasks/31.4` §8): пункт закрывается успешными обменами этой сессии, состояние
пунктов восстанавливается из единственного следа результата, а режим «только
невыполненное» / «выполнять всё» меняет очередь, но не результаты.
"""

from __future__ import annotations

from typing import Any

import pytest

from acceptance import programme as programme_api
from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance import run_state
from acceptance import sets as sets_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.http_log import HttpExchange
from acceptance.session import new_session

TECH_CHECK = "TC-SYS-01"


@pytest.fixture()
def library() -> sets_api.SetsLibrary:
    """Стартовая библиотека наборов (раздел «Система» плюс «Смоук-минимум»)."""
    return sets_api.library_from_catalog(author="Иванов И.И.")


@pytest.fixture()
def programme(library: sets_api.SetsLibrary) -> programme_api.Programme:
    """Утверждённая программа: раздел «Система» и смоук-минимум."""
    draft = programme_api.Programme.from_sets(library, ["SET-SYS", sets_api.SMOKE_SET_ID])
    draft.approve(by="Иванов И.И.", comment="приёмка", reduction="смоук и раздел системы")
    return draft


@pytest.fixture()
def session() -> Any:
    """Сессия испытаний без записи на диск."""
    return new_session(base_url="https://stand.local")


def _exchange(
    seq: int,
    method: str,
    path: str,
    status: int,
    *,
    at: str,
    query: str = "",
) -> HttpExchange:
    """Запись журнала обмена (как её пишет `http_log.LoggingTransport`)."""
    return HttpExchange(
        seq=seq,
        started_at=at,
        method=method,
        path=path,
        query=query,
        status=status,
        duration_ms=1.0,
        request_bytes=0,
        response_bytes=0,
        content_type="application/json",
    )


def _served(session: Any, seq: int, path: str, status: int = 200, **kwargs: Any) -> HttpExchange:
    """Успешный обмен внутри окна сессии (время создания — граница включительно)."""
    return _exchange(seq, "GET", path, status, at=str(session.created_at), **kwargs)


# ---------------------------------------------------------------------------
# Журнал: что считается подтверждением
# ---------------------------------------------------------------------------
def test_session_records_are_limited_to_session_window(session: Any):
    """В факт сессии попадают только обмены, сделанные после её создания."""
    fresh = _served(session, 2, "/health")
    old = _exchange(1, "GET", "/health", 200, at="2000-01-01T00:00:00")

    assert run_state.session_records([old, fresh], session) == (fresh,)
    assert run_state.session_records(None, session) == ()


def test_is_success_covers_only_2xx():
    """Успех — только 2xx: ошибка стенда подтверждением не считается."""
    assert run_state.is_success(_exchange(1, "GET", "/health", 200, at="2026-01-01T00:00:00"))
    assert run_state.is_success(_exchange(2, "GET", "/health", 299, at="2026-01-01T00:00:00"))
    assert not run_state.is_success(_exchange(3, "GET", "/health", 300, at="2026-01-01T00:00:00"))
    assert not run_state.is_success(_exchange(4, "GET", "/health", 404, at="2026-01-01T00:00:00"))
    assert not run_state.is_success(object())


def test_endpoint_specs_from_catalog():
    """Проверка подтверждается объявленными операциями, проба — нет."""
    assert [str(spec.path) for spec in run_state.endpoint_specs(TECH_CHECK)] == ["/health"]
    assert run_state.endpoint_specs("TC-???-99") == ()

    spec = catalog.find(TECH_CHECK)
    assert spec is not None and spec.endpoints


def test_endpoint_specs_empty_for_probe_only_check():
    """Негативные пробы (`probe_paths`) запросом не подтверждаются."""
    without = [
        check_id
        for check_id in catalog.check_ids()
        if (spec := catalog.find(check_id)) is not None and not spec.endpoints
    ]
    assert without, "в каталоге должны быть проверки без объявленных операций"
    assert run_state.endpoint_specs(without[0]) == ()


def test_evidence_requires_success_for_every_endpoint(session: Any):
    """Подтверждение — успешные обмены по всем операциям проверки."""
    ok = run_state.evidence_by_request([_served(session, 7, "/health")], TECH_CHECK)
    assert ok is not None
    assert ok["endpoints"] == ["get /health"]
    assert (ok["journal_from"], ok["journal_to"]) == (7, 7)
    assert ok["note"] == run_state.CONFIRMED_NOTE

    failed = run_state.evidence_by_request([_served(session, 8, "/health", 404)], TECH_CHECK)
    assert failed is None
    assert run_state.evidence_by_request([], TECH_CHECK) is None


# ---------------------------------------------------------------------------
# Результат «по факту запроса»
# ---------------------------------------------------------------------------
def test_confirm_by_request_records_passed_result(session: Any, programme: programme_api.Programme):
    """Успешный запрос сессии даёт результат проверки со ссылкой на журнал."""
    records = [_served(session, 3, "/health")]
    written = run_state.confirm_by_request(session, programme, records, author="Петров П.П.")

    assert written == [TECH_CHECK]
    row = results_api.row(session, TECH_CHECK)
    assert row["status"] == str(CheckStatus.PASSED)
    assert row["origin"] == results_api.ORIGIN_REQUEST
    assert row["operator_note"] == run_state.CONFIRMED_NOTE
    assert (row["journal_from"], row["journal_to"]) == (3, 3)
    assert results_api.repeats(session, TECH_CHECK) == 0, "подтверждение — не повтор проверки"

    assert run_state.confirm_by_request(session, programme, records) == []


def test_confirm_by_request_skips_unconfirmed(session: Any, programme: programme_api.Programme):
    """Без успешных обменов результат не выдумывается."""
    assert run_state.confirm_by_request(session, programme, []) == []
    assert results_api.status_of(session, TECH_CHECK) == results_api.STATUS_NOT_RUN


# ---------------------------------------------------------------------------
# Очередь и режим прогона
# ---------------------------------------------------------------------------
def test_run_queue_only_remaining_and_all(session: Any, programme: programme_api.Programme):
    """Режим «только невыполненное» пропускает закрытое, «выполнять всё» — нет."""
    queue = run_state.run_queue(session, programme)
    assert queue.size == programme.size
    assert queue.pending_ids == programme.check_ids, "результатов нет — все пункты ждут"

    results_api.record(session, {"check_id": TECH_CHECK, "status": str(CheckStatus.PASSED)})

    only_remaining = run_state.run_queue(session, programme)
    assert TECH_CHECK not in only_remaining.pending_ids
    item = only_remaining.find(TECH_CHECK)
    assert item is not None and item.state == queue_api.QUEUE_DONE

    session.skip_done = False
    run_all = run_state.run_queue(session, programme)
    assert run_all.pending_ids == programme.check_ids, "режим «выполнять всё» не смотрит результаты"


def test_restore_state_marks_suspended_as_off(session: Any, programme: programme_api.Programme):
    """Снятие — результат: пункт восстанавливается как «снята» с причиной."""
    results_api.suspend(session, TECH_CHECK, "нет доступа к реестру", author="Петров П.П.")

    queue = run_state.run_queue(session, programme)
    item = queue.find(TECH_CHECK)
    assert item is not None
    assert item.state == queue_api.QUEUE_OFF
    assert TECH_CHECK not in queue.pending_ids
    assert run_state.suspension_reason(session, TECH_CHECK) == "нет доступа к реестру"


def test_run_queue_uses_programme_revision(session: Any, programme: programme_api.Programme):
    """Прогон привязан к номеру ревизии: состав берётся из её снимка (`DR-P-14`)."""
    first = programme.revision
    programme.revise(comment="правка после начала прогона")
    session.run_revision = first

    queue = run_state.run_queue(session, programme)
    assert queue.revision == first


def test_run_queue_mode_comes_from_argument(session: Any, programme: programme_api.Programme):
    """Режим исполнения задаётся вызовом, а не хранится в сессии (v8)."""
    queue = run_state.run_queue(session, programme, mode=queue_api.MODE_CALL)
    assert queue.mode == queue_api.MODE_CALL


def test_progress_reports_next_and_done(session: Any, programme: programme_api.Programme):
    """Прогресс прогона: сколько закрыто, сколько осталось и что запустится."""
    queue = run_state.run_queue(session, programme)
    data = run_state.progress(queue)

    assert data["total"] == programme.size
    assert data["done"] == 0
    assert data["pending"] == programme.size
    assert data["next"] == programme.check_ids[0]
    assert data["mode"] == queue_api.MODE_CHECK
