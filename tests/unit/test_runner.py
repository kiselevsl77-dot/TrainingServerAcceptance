"""Тесты исполнителя очереди (`acceptance/runner.py`).

Проверяется то, что видит оператор при прогоне: подпись команды «▶ Следующая: `TC-…`»
(`IR-P-4`, `IR-P-20`), подтверждение изменяющих и ресурсоёмких пунктов (`FR-P-19`),
режимы «сценарием»/«по вызовам» (`A2`), «Пачка tech» (`A4`), правила останова
авто-прогона (`A3`) и пояснения вместо «молчаливых» пропусков.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from acceptance import endpoints as ep
from acceptance import programme as programme_api
from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance import runner as runner_api
from acceptance import sets as sets_api
from acceptance.api import build_client
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks.engine import CheckResult
from acceptance.checks.registry import CheckSpec, CheckStatus
from acceptance.config import PultConfig
from acceptance.exchange import build_console_client
from acceptance.http_log import HttpExchange, Journal
from acceptance.session import new_session
from acceptance.ui.state import Apis, Runtime
from client.settings import TrainingServerSettings

LABEL = "https://stand.local"


def _handler(request: httpx.Request) -> httpx.Response:
    """Подменённый стенд: чтение — 200, датасеты по `{id}` — 500, удаление — 404."""
    path = request.url.path
    if path == "/api/data/files":
        return httpx.Response(200, json={"count": 2, "items": []})
    if request.method == "DELETE":
        return httpx.Response(404, json={"detail": "not found"})
    if path.startswith("/api/datasets/"):
        return httpx.Response(500, json={"detail": "boom"})
    return httpx.Response(200, json={"ok": True})


@pytest.fixture()
def wired() -> Any:
    """Рантайм пульта: API-слой и «сырой» клиент консоли на подменённом стенде.

    Собирается так же, как в приложении (`acceptance.ui.state`): `Runtime.client` и
    `Apis` строятся из `acceptance.api.build_client`, а «сырой» клиент консоли
    (`build_console_client`) уходит в пробу негативных маршрутов.
    """
    journal = Journal(max_records=200)
    config = PultConfig(body_limit=500, log_bodies=True, journal_max=200)
    settings = TrainingServerSettings(base_url="http://stand.local", timeout=5.0)
    inner = httpx.MockTransport(_handler)
    api_client = build_client(settings, journal, config=config, inner=inner)
    console = build_console_client(settings, journal, config=config, inner=inner)
    runtime = Runtime(
        config=config,
        settings=settings,
        journal=journal,
        client=api_client,
        apis=Apis.build(api_client),
    )
    return runtime, journal, console, new_session(base_url=LABEL)


@pytest.fixture()
def library() -> sets_api.SetsLibrary:
    """Стартовая библиотека наборов."""
    return sets_api.library_from_catalog(author="Иванов И.И.")


def _programme(library: sets_api.SetsLibrary, *set_ids: str) -> programme_api.Programme:
    """Черновик программы из наборов (по умолчанию — раздел «Система»)."""
    return programme_api.Programme.from_sets(library, list(set_ids or ("SET-SYS",)))


def _queue(programme: programme_api.Programme) -> queue_api.Queue:
    """Очередь — снимок текущей ревизии программы."""
    return queue_api.build_queue(programme)


def _spec(check_id: str) -> CheckSpec:
    """Описание проверки каталога (в тестах проверка обязана быть в каталоге)."""
    found = catalog.find(check_id)
    assert found is not None, f"проверка {check_id} не найдена в каталоге"
    return found


def _item(queue: queue_api.Queue, check_id: str) -> queue_api.QueueItem:
    """Пункт очереди (в тестах пункт обязан быть в очереди)."""
    found = queue.find(check_id)
    assert found is not None, f"пункт {check_id} не найден в очереди"
    return found


def _record(session: Any, check_id: str) -> dict[str, Any]:
    """Запись результата проверки (в тестах результат обязан быть записан)."""
    found = results_api.result_of(session, check_id)
    assert found is not None, f"результат {check_id} не записан"
    return found


def _fake_automate(
    monkeypatch: pytest.MonkeyPatch,
    spec: CheckSpec,
    *,
    status: CheckStatus = CheckStatus.PASSED,
    verdict: str = "сервер отвечает",
) -> Callable[[Any], CheckResult]:
    """Подменяет сценарий: пишет обмен, результат (как движок) и возвращает его."""

    def fake(context: Any) -> CheckResult:
        journal = context.journal
        seq = None
        if journal is not None:
            seq = journal.next_seq()
            journal.add(
                HttpExchange(
                    seq=seq,
                    started_at="2026-09-22T10:00:00",
                    method="GET",
                    path="/health",
                    query="",
                    status=200,
                    duration_ms=9.0,
                    request_bytes=0,
                    response_bytes=12,
                    content_type="application/json",
                    label=spec.check_id,
                )
            )
        result = CheckResult(
            check_id=spec.check_id,
            status=status,
            verdict=verdict,
            journal_from=1 if seq is not None else None,
            journal_to=seq,
            duration_ms=9.0,
            started_at="2026-09-22T10:00:00",
            ended_at="2026-09-22T10:00:01",
        )
        checks_engine.record_result(context.session, result)
        return result

    monkeypatch.setattr(runner_api.checks_runner, "automate", fake)
    return fake


def can_run_error(queue: queue_api.Queue, programme: programme_api.Programme) -> str:
    """Пояснение «почему нельзя запускать» (помощник тестов)."""
    allowed, reason = runner_api.can_run(queue, programme)
    assert allowed is False
    return reason


# ---------------------------------------------------------------------------
# Одна команда запуска и готовность прогона
# ---------------------------------------------------------------------------
def test_next_label_names_the_next_check(library: sets_api.SetsLibrary):
    """Подпись команды содержит идентификатор пункта, который будет запущен."""
    programme = _programme(library)
    queue = _queue(programme)
    first = programme.check_ids[0]

    assert runner_api.next_check_id(queue) == first
    assert runner_api.next_label(queue) == f"▶ Следующая: {first}"

    for item in queue.items:
        queue.begin(item.check_id)
        queue.finish(item.check_id)

    assert runner_api.next_check_id(queue) == ""
    assert runner_api.next_label(queue) == "▶ Следующая"


@pytest.mark.parametrize(
    ("check_class", "confirm", "manual"),
    [
        ("tech", False, False),
        ("live", True, False),
        ("heavy", True, False),
        ("manual", False, True),
    ],
)
def test_confirmation_and_operator_rules(check_class: str, confirm: bool, manual: bool):
    """Карточка запуска нужна для `live`/`heavy`, отметка оператора — для `manual`."""
    item = programme_api.ProgrammeItem(check_id="TC-SYS-01", check_class=check_class)

    need, reason = runner_api.needs_confirmation(item)
    assert need is confirm
    assert bool(reason) is confirm

    need, reason = runner_api.requires_operator(item)
    assert need is manual
    assert bool(reason) is manual


def test_can_run_reports_readiness(library: sets_api.SetsLibrary):
    """Готовность прогона: пустая программа, расхождение ревизий, пройденная очередь."""
    programme = _programme(library)
    queue = _queue(programme)

    assert runner_api.can_run(queue, programme) == (True, "")
    assert can_run_error(queue_api.Queue(), programme) != ""
    assert "пересоберите" in can_run_error(queue, _programme(library, "SET-REC"))

    for item in queue.items:
        queue.begin(item.check_id)
        queue.finish(item.check_id)

    assert "пройдена" in can_run_error(queue, programme)


def test_programme_notice_marks_draft(library: sets_api.SetsLibrary):
    """Баннер прогона честно говорит, что программа — черновик (`A1`)."""
    draft = _programme(library)
    queue = _queue(draft)

    assert "черновик" in runner_api.programme_notice(queue)
    assert "ревизия 1" in runner_api.programme_notice(queue)

    draft.approve(by="Иванов И.И.", reduction="сокращение: только раздел системы")
    approved_queue = _queue(draft)

    assert "черновик" not in runner_api.programme_notice(approved_queue)
    assert "утверждена" in runner_api.programme_notice(approved_queue)
    assert "SCR-102" in runner_api.programme_notice(queue_api.Queue())


# ---------------------------------------------------------------------------
# Режим «сценарием»: проверка целиком
# ---------------------------------------------------------------------------
def test_execute_check_runs_scenario_and_closes_item(
    wired: Any, library: sets_api.SetsLibrary, monkeypatch: pytest.MonkeyPatch
):
    """Проверка целиком: сценарий, результат движка, журнал и маркер «последняя»."""
    runtime, journal, client, session = wired
    programme = _programme(library)
    queue = _queue(programme)
    check_id = programme.check_ids[0]
    _fake_automate(monkeypatch, _spec(check_id))

    outcome = runner_api.execute_check(queue, programme, session, runtime, client=client)

    assert outcome.ok
    assert outcome.level == runner_api.LEVEL_CHECK
    assert outcome.status == str(CheckStatus.PASSED)
    assert outcome.journal_to == journal.records[-1].seq
    assert outcome.repeats == 0
    assert queue.marker(check_id) == queue_api.MARKER_LAST
    assert _item(queue, check_id).state == queue_api.QUEUE_DONE
    assert _item(queue, check_id).journal_to is not None
    assert results_api.status_of(session, check_id) == str(CheckStatus.PASSED)
    assert _record(session, check_id)["revision"] == programme.revision


def test_repeat_keeps_previous_result_in_history(
    wired: Any, library: sets_api.SetsLibrary, monkeypatch: pytest.MonkeyPatch
):
    """Повтор проверки сохраняет прежний результат как историю (`FR-P-36`)."""
    runtime, _journal, client, session = wired
    programme = _programme(library)
    queue = _queue(programme)
    check_id = programme.check_ids[0]
    _fake_automate(monkeypatch, _spec(check_id), verdict="первый прогон")

    first = runner_api.execute_check(
        queue, programme, session, runtime, check_id=check_id, client=client
    )
    second = runner_api.execute_check(
        queue, programme, session, runtime, check_id=check_id, client=client
    )

    assert first.repeats == 0
    assert second.repeats == 1
    assert results_api.repeats(session, check_id) == 1
    assert results_api.history(session, check_id)[0]["verdict"] == "первый прогон"
    assert len(session.checks) == 1  # одна запись на проверку (DR-P-5)


def test_live_check_requires_run_card(
    wired: Any, library: sets_api.SetsLibrary, monkeypatch: pytest.MonkeyPatch
):
    """Без карточки запуска изменяющая проверка на стенд не отправляется (`FR-P-19`)."""
    runtime, journal, client, session = wired
    programme = _programme(library)
    programme.items[0].check_class = "live"
    queue = _queue(programme)
    check_id = programme.check_ids[0]
    _fake_automate(monkeypatch, _spec(check_id))

    outcome = runner_api.execute_check(queue, programme, session, runtime, client=client)

    assert outcome.confirmation_required is True
    assert outcome.stopped is True
    assert not journal.records
    assert session.checks == []
    assert _item(queue, check_id).is_pending
    assert "карточка запуска" in queue.stop_reason


def test_run_card_evidence_allows_live_check(
    wired: Any, library: sets_api.SetsLibrary, monkeypatch: pytest.MonkeyPatch
):
    """Заполненная карточка запуска позволяет прогон и попадает в доказательства."""
    runtime, _journal, client, session = wired
    programme = _programme(library)
    programme.items[0].check_class = "live"
    queue = _queue(programme)
    check_id = programme.check_ids[0]
    _fake_automate(monkeypatch, _spec(check_id))
    card = {"goal": "полный цикл файла", "data": "__TEST__…csv", "responsible": "Петров П.П."}

    outcome = runner_api.execute_check(
        queue, programme, session, runtime, client=client, run_evidence=card
    )

    assert outcome.ok
    assert _record(session, check_id)["evidence"]["run_card"] == card


def test_manual_check_waits_for_operator(wired: Any, library: sets_api.SetsLibrary):
    """Ручная проверка автоматически не выполняется: нужна отметка оператора (`FR-P-32`)."""
    runtime, journal, client, session = wired
    programme = _programme(library)
    programme.items[0].check_class = "manual"
    queue = _queue(programme)
    check_id = programme.check_ids[0]

    outcome = runner_api.execute_check(queue, programme, session, runtime, client=client)

    assert outcome.operator_required is True
    assert not journal.records
    assert session.checks == []
    assert _item(queue, check_id).is_pending
    assert "оператор" in outcome.detail


def test_unknown_check_is_closed_as_skipped(wired: Any, library: sets_api.SetsLibrary):
    """Проверки нет в каталоге — пункт закрывается «пропущена» с пояснением."""
    runtime, _journal, client, session = wired
    programme = _programme(library)
    programme.add_check("TC-NOPE-01")
    queue = _queue(programme)

    outcome = runner_api.execute_check(
        queue, programme, session, runtime, check_id="TC-NOPE-01", client=client
    )

    assert outcome.status == str(CheckStatus.SKIPPED)
    assert "каталоге" in outcome.detail
    assert _item(queue, "TC-NOPE-01").state == queue_api.QUEUE_DONE
    assert results_api.status_of(session, "TC-NOPE-01") == str(CheckStatus.SKIPPED)


# ---------------------------------------------------------------------------
# Режим «по вызовам»
# ---------------------------------------------------------------------------
def test_mode_call_sends_one_call_and_leaves_result_empty(
    wired: Any, library: sets_api.SetsLibrary
):
    """Один шаг = один вызов; результат проверки при этом не ставится (`A2`)."""
    runtime, journal, client, session = wired
    programme = _programme(library)
    queue = _queue(programme)
    queue.set_mode(queue_api.MODE_CALL)
    check_id = "TC-SYS-05"  # две операции: реестр файлов и датасет по {id}
    steps = runner_api.call_steps(_spec(check_id))

    outcome = runner_api.execute_call(
        queue, programme, session, runtime, check_id=check_id, client=client
    )

    assert outcome.level == runner_api.LEVEL_CALL
    assert outcome.status == runner_api.STATUS_PASSED
    assert outcome.verdict == runner_api.VERDICT_MATCH
    assert outcome.call_index == 1
    assert outcome.steps == len(steps)
    assert len(journal.records) == 1
    assert journal.records[0].label == check_id
    assert session.checks == []
    item = _item(queue, check_id)
    assert item is not None and item.is_pending and item.call_index == 1
    assert "вызов 1/2" in item.note


def test_mode_call_waits_for_parameters(wired: Any, library: sets_api.SetsLibrary):
    """Без значения path-параметра вызов не отправляется, шаг не «съедается»."""
    runtime, journal, client, session = wired
    programme = _programme(library)
    queue = _queue(programme)
    queue.set_mode(queue_api.MODE_CALL)
    check_id = "TC-SYS-05"
    runner_api.execute_call(queue, programme, session, runtime, check_id=check_id, client=client)

    skipped = runner_api.execute_call(
        queue, programme, session, runtime, check_id=check_id, client=client
    )

    assert skipped.status == runner_api.STEP_SKIPPED
    assert skipped.stopped is True
    assert "dataset_id" in skipped.verdict
    assert len(journal.records) == 1  # второй вызов не ушёл
    assert _item(queue, check_id).call_index == 1
    assert _item(queue, check_id).is_pending
    assert _item(queue, check_id).note == skipped.verdict


def test_mode_call_closes_item_after_last_call(wired: Any, library: sets_api.SetsLibrary):
    """После последнего вызова пункт закрывается, но результат проверки остаётся пустым."""
    runtime, journal, client, session = wired
    programme = _programme(library)
    queue = _queue(programme)
    queue.set_mode(queue_api.MODE_CALL)
    check_id = "TC-SYS-05"
    _item(queue, check_id).params["dataset_id"] = "abc"
    runner_api.execute_call(queue, programme, session, runtime, check_id=check_id, client=client)

    outcome = runner_api.execute_call(
        queue, programme, session, runtime, check_id=check_id, client=client
    )

    assert outcome.status == runner_api.STATUS_FAILED  # стенд ответил 500
    assert outcome.verdict == runner_api.VERDICT_MISMATCH
    assert _item(queue, check_id).is_done
    assert _item(queue, check_id).call_index == 2
    assert "результат проверки не ставится" in _item(queue, check_id).note
    assert session.checks == []
    assert journal.records[-1].label == check_id


def test_mode_call_without_client_keeps_item_pending(wired: Any, library: sets_api.SetsLibrary):
    """Без клиента консоли вызов не отправляется: пункт остаётся ожидающим."""
    runtime, journal, _client, session = wired
    programme = _programme(library)
    queue = _queue(programme)
    queue.set_mode(queue_api.MODE_CALL)
    check_id = "TC-SYS-05"

    outcome = runner_api.execute_call(queue, programme, session, runtime, check_id=check_id)

    assert outcome.status == runner_api.STATUS_FAILED
    assert "клиент" in outcome.detail
    assert not journal.records
    assert _item(queue, check_id).is_pending


def test_mode_call_requires_run_card_for_changing_check(wired: Any, library: sets_api.SetsLibrary):
    """Изменяющая проверка и в режиме «по вызовам» требует карточку запуска (`FR-P-19`)."""
    runtime, journal, client, session = wired
    programme = _programme(library, "SET-SYS", "SET-DS")
    queue = _queue(programme)
    queue.set_mode(queue_api.MODE_CALL)

    outcome = runner_api.execute_call(
        queue, programme, session, runtime, check_id="TC-DS-01", client=client
    )

    assert outcome.confirmation_required is True
    assert outcome.level == runner_api.LEVEL_CALL
    assert not journal.records
    assert session.checks == []
    assert _item(queue, "TC-DS-01").is_pending


def test_execute_next_dispatches_by_mode(
    wired: Any, library: sets_api.SetsLibrary, monkeypatch: pytest.MonkeyPatch
):
    """«Следующая» работает по режиму очереди: сценарием либо по вызовам."""
    runtime, _journal, client, session = wired
    programme = _programme(library)
    queue = _queue(programme)
    check_id = "TC-SYS-05"
    queue.set_mode(queue_api.MODE_CALL)

    by_call = runner_api.execute_next(
        queue, programme, session, runtime, check_id=check_id, client=client
    )
    assert by_call.level == runner_api.LEVEL_CALL

    queue.reset()
    queue.set_mode(queue_api.MODE_CHECK)
    _fake_automate(monkeypatch, _spec(check_id))

    by_scenario = runner_api.execute_next(queue, programme, session, runtime, client=client)

    assert by_scenario.level == runner_api.LEVEL_CHECK
    assert by_scenario.ok
    assert results_api.status_of(session, check_id) == str(CheckStatus.PASSED)


# ---------------------------------------------------------------------------
# Пачка tech и авто-прогон с паузой
# ---------------------------------------------------------------------------
def test_batch_tech_stops_before_live_item(
    wired: Any, library: sets_api.SetsLibrary, monkeypatch: pytest.MonkeyPatch
):
    """«Пачка tech» идёт по безопасным пунктам и останавливается перед живой проверкой."""
    runtime, _journal, client, session = wired
    programme = _programme(library)
    ids = programme.check_ids
    programme.items[1].check_class = "live"
    queue = _queue(programme)
    _fake_automate(monkeypatch, _spec(ids[0]))

    assert runner_api.batch_tech_ids(queue, programme) == [ids[0]]

    outcomes = runner_api.run_batch_tech(queue, programme, session, runtime, client=client)

    assert len(outcomes) == 1
    assert outcomes[0].ok
    assert _item(queue, ids[0]).is_done
    assert _item(queue, ids[1]).is_pending


def test_batch_tech_goes_on_after_failure(
    wired: Any, library: sets_api.SetsLibrary, monkeypatch: pytest.MonkeyPatch
):
    """Отказ проверки пачку не прерывает (`A4`): отказ зафиксирован, прогон продолжен."""
    runtime, _journal, client, session = wired
    programme = _programme(library)
    ids = programme.check_ids
    queue = _queue(programme)
    _fake_automate(monkeypatch, _spec(ids[0]), status=CheckStatus.FAILED, verdict="500 от стенда")

    outcomes = runner_api.run_batch_tech(queue, programme, session, runtime, client=client, limit=2)

    assert len(outcomes) == 2
    assert all(not outcome.stopped for outcome in outcomes)
    assert results_api.status_of(session, ids[0]) == str(CheckStatus.FAILED)
    assert not results_api.is_suspended(session, ids[0])


def test_auto_step_stops_on_queue_end_and_operator(library: sets_api.SetsLibrary, wired: Any):
    """Авто-прогон останавливается на «Стоп» оператора и на пройденной очереди (`A3`)."""
    runtime, _journal, client, session = wired
    programme = _programme(library)
    queue = _queue(programme)

    assert runner_api.auto_stop_reason(queue, programme) == ""
    assert (
        runner_api.auto_step(queue, programme, session, runtime, client=client, stop_requested=True)
        is None
    )
    assert "оператором" in queue.stop_reason

    for item in queue.items:
        queue.begin(item.check_id)
        queue.finish(item.check_id)

    assert runner_api.auto_step(queue, programme, session, runtime) is None
    assert "очередь пройдена" in queue.stop_reason


def test_auto_step_stops_on_live_item(
    wired: Any, library: sets_api.SetsLibrary, monkeypatch: pytest.MonkeyPatch
):
    """Пункт с карточкой запуска останавливает авто-прогон: запрос не отправляется."""
    runtime, journal, client, session = wired
    programme = _programme(library)
    programme.items[0].check_class = "live"
    queue = _queue(programme)
    _fake_automate(monkeypatch, _spec(programme.check_ids[0]))

    outcome = runner_api.auto_step(queue, programme, session, runtime, client=client)

    assert outcome is not None
    assert outcome.confirmation_required is True
    assert not journal.records
    assert "карточка запуска" in queue.stop_reason
    assert queue.stop_reason == outcome.stop_reason


def test_call_helpers_estimate_expectations():
    """Помощники оценки вызова: ожидания по безопасности, вердикты, параметры пути."""
    step = runner_api.CallStep(
        index=1, method="GET", path="/api/data/files", endpoint_key="get /api/data/files"
    )

    assert runner_api.parse_operation("get /api/data/files") == ("GET", "/api/data/files")
    assert runner_api.parse_operation("/health") == ("", "/health")
    assert runner_api.expected_rule() == "2xx — чтение выполнено"
    assert runner_api.expected_statuses(step) == (200, 201, 202, 204)
    assert runner_api.judge_response(step, status=200).verdict == runner_api.VERDICT_MATCH
    assert runner_api.judge_response(step, status=500).verdict == runner_api.VERDICT_MISMATCH
    assert runner_api.judge_response(step, status=None, error="таймаут").detail == "таймаут"
    assert runner_api.judge_response(step, status=None).verdict == runner_api.VERDICT_NOT_RUN

    heavy = runner_api.CallStep(
        index=1, method="POST", path="/api/datasets/fill", safety=str(ep.Safety.HEAVY)
    )
    verdict = runner_api.judge_response(heavy, status=202)

    assert heavy.is_heavy
    assert runner_api.expected_statuses(heavy) == (202, 200, 201)
    assert verdict.is_match is True
    assert "task_id" in verdict.detail

    with_param = runner_api.CallStep(
        index=1,
        method="GET",
        path="/api/datasets/{dataset_id}",
        endpoint_key="get /api/datasets/{dataset_id}",
    )

    assert runner_api.path_params(with_param) == ["dataset_id"]
    assert runner_api.plan_endpoint(with_param) is not None
    assert runner_api.plan_endpoint(step) is not None
    assert "id" in runner_api.query_params(step)  # у реестра файлов есть query-параметры


def test_outcome_journal_range_and_summary(wired: Any, library: sets_api.SetsLibrary):
    """Итог шага показывает диапазон журнала и свою сводку для интерфейса."""
    runtime, _journal, client, session = wired
    programme = _programme(library)
    queue = _queue(programme)

    outcome = runner_api.execute_call(
        queue, programme, session, runtime, check_id="TC-SYS-05", client=client
    )

    assert outcome.journal_range.startswith("#")
    summary = outcome.summary()
    assert summary["check_id"] == "TC-SYS-05"
    assert summary["level"] == runner_api.LEVEL_CALL
    assert summary["steps"] == 2
