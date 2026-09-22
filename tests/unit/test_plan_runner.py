"""Тесты исполнителя программы испытаний (`acceptance/plan_runner.py`).

Проверяется то, что оператор видит при пошаговом прогоне: отправку **одного** вызова,
вердикт «соответствует ожиданию», пропуски (нет параметров, битый JSON, негативная
проба) и закрытие вызовов проверки по фактическим обменам журнала.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from acceptance import plan, plan_runner
from acceptance.checks import catalog
from acceptance.checks.engine import CheckResult
from acceptance.checks.registry import CheckClass, CheckStatus
from acceptance.config import PultConfig
from acceptance.exchange import build_console_client
from acceptance.http_log import HttpExchange, Journal
from acceptance.ui.state import Apis, Runtime
from client.settings import TrainingServerSettings

LABEL = "https://stand.local"


def _handler(request: httpx.Request) -> httpx.Response:
    """Подменённый стенд: чтение — 200, изменяющие — 202, удаление — 404."""
    path = request.url.path
    if path == "/api/data/files":
        return httpx.Response(200, json={"count": 2, "items": []})
    if path.startswith("/api/datasets/fill"):
        return httpx.Response(202, json={"detail": "принято"})
    if request.method == "DELETE":
        return httpx.Response(404, json={"detail": "not found"})
    if path.startswith("/api/datasets/"):
        return httpx.Response(500, json={"detail": "boom"})
    return httpx.Response(200, json={"ok": True})


@pytest.fixture
def wired():
    """Рантайм пульта с подменённым стендом и настоящим журналом обмена."""
    journal = Journal(max_records=200)
    config = PultConfig(body_limit=500, log_bodies=True, journal_max=200)
    settings = TrainingServerSettings(base_url="http://stand.local", timeout=5.0)
    client = build_console_client(
        settings,
        journal,
        config=config,
        inner=httpx.MockTransport(_handler),
    )
    runtime = Runtime(
        config=config,
        settings=settings,
        journal=journal,
        client=client,
        apis=Apis.build(client),
    )
    session = _session()
    return runtime, journal, client, session


def _session():
    """Минимальная сессия испытаний (без записи на диск)."""
    from acceptance.session import new_session

    return new_session(base_url=LABEL)


def _call_item(
    *,
    item_id: str = "TC-FILE-01#1",
    method: str = "GET",
    path: str = "/api/data/files",
    endpoint_key: str = "get /api/data/files",
    safety: str = "read",
    body_kind: str = "",
    probe: bool = False,
    check_class: str = "tech",
    params: dict | None = None,
) -> plan.PlanItem:
    """Пункт-вызов для тестов (по умолчанию — чтение реестра $файлов)."""
    return plan.PlanItem(
        item_id=item_id,
        level=plan.LEVEL_CALL,
        title=f"вызов {method} {path}",
        parent_id=item_id.split("#")[0],
        group="TC-FILE",
        method=method,
        path=path,
        endpoint_key=endpoint_key,
        safety=safety,
        body_kind=body_kind,
        check_class=check_class,
        probe=probe,
        expected="2xx — чтение выполнено" if safety == "read" else "2xx",
        params=dict(params or {}),
    )


def test_next_item_delegates_to_plan(wired):
    """Исполнитель берёт следующий пункт из плана (без своей очереди)."""
    _runtime, _journal, _client, session = wired
    state = plan.build_plan([catalog.CHECKS[0]])

    assert plan_runner.next_item(state) is state.next_item()
    assert session.status


@pytest.mark.parametrize(
    ("level", "check_class", "safety", "expected"),
    [
        (plan.LEVEL_CHECK, "tech", "", False),
        (plan.LEVEL_CHECK, "live", "", True),
        (plan.LEVEL_CHECK, "heavy", "", True),
        (plan.LEVEL_CALL, "", "read", False),
        (plan.LEVEL_CALL, "", "write", True),
        (plan.LEVEL_CALL, "", "destructive", True),
    ],
)
def test_item_needs_confirmation(level: str, check_class: str, safety: str, expected: bool):
    """Подтверждение требуют проверки классов live/heavy и изменяющие вызовы (NFR-T4)."""
    item = plan.PlanItem(
        item_id="TC-X-01",
        level=level,
        title="пункт",
        check_class=check_class or str(CheckClass.TECH),
        safety=safety,
    )

    need, reason = plan_runner.item_needs_confirmation(item)

    assert need is expected
    if expected:
        assert reason


def test_execute_call_mode_sends_one_request_and_judges_it(wired):
    """Режим «по вызовам»: один запрос, вердикт «соответствует ожиданию», диапазон журнала."""
    runtime, journal, client, session = wired
    state = plan.PlanState(items=[_call_item()])

    run = plan_runner.execute_item(
        state, item=state.items[0], session=session, runtime=runtime, client=client
    )

    assert run.status == plan.STATUS_PASSED
    assert run.item.verdict == plan.VERDICT_MATCH
    assert run.journal_to == journal.records[-1].seq
    assert run.journal_from is not None
    assert run.item.duration_ms is not None
    assert journal.records[-1].label == "TC-FILE-01"
    assert state.find("TC-FILE-01#1").status == plan.STATUS_PASSED
    assert state.last_item_id == "TC-FILE-01#1"


def test_execute_call_mode_reports_mismatch(wired):
    """5xx на вызове чтения — отказ и вердикт «не соответствует ожиданию»."""
    runtime, journal, client, session = wired
    item = _call_item(
        item_id="TC-DS-01#1",
        path="/api/datasets/{dataset_id}",
        endpoint_key="get /api/datasets/{dataset_id}",
        params={"dataset_id": "abc"},
    )
    state = plan.PlanState(items=[item])

    run = plan_runner.execute_item(
        state, item=item, session=session, runtime=runtime, client=client
    )

    assert run.status == plan.STATUS_FAILED
    assert run.item.verdict == plan.VERDICT_MISMATCH
    assert "500" in run.detail
    assert journal.records[-1].status == 500


def test_execute_call_mode_skips_without_path_parameter(wired):
    """Нет значения path-параметра — вызов не отправляется (пропуск, а не отказ)."""
    runtime, _journal, client, session = wired
    item = _call_item(
        item_id="TC-DS-02#1",
        path="/api/datasets/{dataset_id}",
        endpoint_key="get /api/datasets/{dataset_id}",
    )
    state = plan.PlanState(items=[item])

    run = plan_runner.execute_item(
        state, item=item, session=session, runtime=runtime, client=client
    )

    assert run.status == plan.STATUS_SKIPPED
    assert "dataset_id" in run.verdict


def test_execute_call_mode_skips_on_broken_json_body(wired):
    """Битый JSON тела — «пропущено»: оператору нужно поправить параметры пункта."""
    runtime, _journal, client, session = wired
    item = _call_item(
        item_id="TC-DS-03#1",
        method="POST",
        path="/api/datasets/",
        endpoint_key="post /api/datasets/",
        safety="write",
        body_kind="json",
        params={"body": "{это не JSON"},
    )
    state = plan.PlanState(items=[item])

    run = plan_runner.execute_item(
        state, item=item, session=session, runtime=runtime, client=client
    )

    assert run.status == plan.STATUS_SKIPPED
    assert "JSON" in run.verdict


def test_negative_probe_is_not_sent_by_plan(wired):
    """Негативная проба выполняется сценарием проверки, план её не дублирует."""
    runtime, journal, client, session = wired
    item = _call_item(
        item_id="TC-SYS-03#1",
        method="DELETE",
        path="/api/loads/{load_id}",
        endpoint_key="",
        safety="destructive",
        probe=True,
    )
    state = plan.PlanState(items=[item])
    before = len(journal.records)

    run = plan_runner.execute_item(
        state, item=item, session=session, runtime=runtime, client=client
    )

    assert run.status == plan.STATUS_SKIPPED
    assert "проба" in run.verdict
    assert len(journal.records) == before


def test_execute_without_client_is_a_failure(wired):
    """Без клиента консоли вызов не отправляется: пункт получает отказ с пояснением."""
    runtime, _journal, _client, session = wired
    item = _call_item()
    state = plan.PlanState(items=[item])

    run = plan_runner.execute_item(state, item=item, session=session, runtime=runtime)

    assert run.status == plan.STATUS_FAILED
    assert "клиент" in run.detail


def test_execute_check_mode_closes_calls_by_journal(wired, monkeypatch: pytest.MonkeyPatch):
    """Проверка целиком: статус из движка, вызовы закрываются по фактическим обменам."""
    runtime, journal, client, session = wired
    spec = next(item for item in catalog.CHECKS if item.check_id == "TC-SYS-01")

    def fake_automate(context):  # noqa: ANN001 — подмена прогона сценария
        journal.add(
            HttpExchange(
                seq=journal.next_seq(),
                started_at="2026-09-21T10:00:00",
                method="GET",
                path="/health",
                query="",
                status=200,
                duration_ms=12.0,
                request_bytes=0,
                response_bytes=24,
                content_type="application/json",
                label=spec.check_id,
            )
        )
        return CheckResult(
            check_id=spec.check_id,
            status=CheckStatus.PASSED,
            verdict="сервер отвечает",
            journal_from=1,
            journal_to=journal.records[-1].seq,
            duration_ms=12.0,
            started_at="2026-09-21T10:00:00",
            ended_at="2026-09-21T10:00:01",
        )

    monkeypatch.setattr(plan_runner.checks_runner, "automate", fake_automate)
    state = plan.build_plan([spec])
    item = state.find(spec.check_id)
    assert item is not None

    run = plan_runner.execute_item(
        state, item=item, session=session, runtime=runtime, client=client
    )

    assert run.status == plan.STATUS_PASSED
    assert run.item.verdict == "сервер отвечает"
    assert session.checks, "результат проверки попал в сессию"
    # вызов проверки, который реально ушёл на стенд, закрыт по журналу
    called = [call for call in state.calls_of(spec.check_id) if call.path == "/health"]
    assert called, "у TC-SYS-01 есть вызов /health"
    assert called[0].status == plan.STATUS_PASSED
    assert called[0].journal_to is not None
    # остальные вызовы проверки в прогоне не участвовали
    others = [call for call in state.calls_of(spec.check_id) if call.path != "/health"]
    assert all(call.status == plan.STATUS_SKIPPED for call in others)


def test_execute_check_mode_without_scenario_is_skipped(wired, monkeypatch: pytest.MonkeyPatch):
    """Ручная проверка (нет сценария) в плане помечается «пропущено»."""
    runtime, _journal, client, session = wired
    monkeypatch.setattr(plan_runner.checks_runner, "automate", lambda context: None)
    spec = catalog.CHECKS[0]
    state = plan.build_plan([spec])
    item = state.items[0]

    run = plan_runner.execute_item(
        state, item=item, session=session, runtime=runtime, client=client
    )

    assert run.status == plan.STATUS_SKIPPED
    assert "сценария" in run.detail


def test_execute_unknown_check_is_skipped(wired):
    """Пункт проверки, которой нет в каталоге, не ломает прогон."""
    runtime, _journal, client, session = wired
    item = plan.PlanItem(
        item_id="TC-NOPE-01",
        level=plan.LEVEL_CHECK,
        title="неизвестная проверка",
        check_class=str(CheckClass.TECH),
    )
    state = plan.PlanState(items=[item])

    run = plan_runner.execute_item(
        state, item=item, session=session, runtime=runtime, client=client
    )

    assert run.status == plan.STATUS_SKIPPED
    assert "каталоге" in run.detail


def test_auto_run_pointer_moves_forward(wired):
    """Пункт, выполненный по кнопке «Выполнить», становится указателем плана."""
    runtime, _journal, client, session = wired
    check = next(spec for spec in catalog.CHECKS if len(spec.endpoints) >= 2)
    state = plan.build_plan([check])
    calls = state.calls_of(check.check_id)
    assert len(calls) >= 2, "выбрана проверка хотя бы с двумя вызовами"
    state.mode = plan.MODE_CALL

    first, second = calls[0], calls[1]
    run = plan_runner.execute_item(
        state, item=first, session=session, runtime=runtime, client=client
    )

    assert run.ok
    assert state.pointer > state.index_of(first.item_id)
    following = plan_runner.next_item(state)
    assert following is not None and following.item_id == second.item_id


def test_helpful_helpers_expose_parameters(wired):
    """Публичные помощники отдают имена параметров операции для интерфейса плана."""
    item = _call_item(
        item_id="TC-DS-04#1",
        path="/api/datasets/{dataset_id}",
        endpoint_key="get /api/datasets/{dataset_id}",
    )

    assert plan_runner.path_params(item) == ["dataset_id"]
    assert isinstance(plan_runner.query_params(item), list)
    assert plan_runner.call_label(item) == "TC-DS-04"
    assert plan_runner.plan_endpoint(item) is not None


def test_path_matching_handles_parameters():
    """Сопоставление фактического пути с шаблоном пункта учитывает `{…}`."""
    matches: Callable[[str, str], bool] = plan_runner._path_matches

    assert matches("/api/datasets/{dataset_id}", "/api/datasets/abc")
    assert not matches("/api/datasets/{dataset_id}", "/api/datasets/abc/chunks")
    assert matches("/health", "/health")
