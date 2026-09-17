"""Тесты автоматических сценариев проверок задач (TC-TASK, этап T4).

Сценарии проверяют испытуемый API и формируют вердикт по фактическим данным:
список задач, фильтры (включая период создания — только дата-время) и пагинация
(TC-TASK-02), карточка с прогонами (TC-TASK-03), идемпотентность команд
(TC-TASK-06), неизвестная задача (TC-TASK-07) и вердикты по истории переходов
FSM-1 (TC-TASK-01/04/05/08).

Отдельно фиксируется поведение движка: результат сценария попадает в сессию,
получает диапазон записей журнала и статус проверки, а отсутствие предусловия
(не выбрана задача) даёт статус «пропущена», а не падение.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from acceptance.checks import catalog
from acceptance.checks import tasks as check_tasks
from acceptance.checks.registry import CheckSpec, CheckStatus
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session
from acceptance.tasks_monitor import TaskMonitor
from client.http import ApiHttpClient
from client.tasks import TasksApi

TASK = "9f0a5b7e-0000-4000-8000-000000000001"
TASK2 = "9f0a5b7e-0000-4000-8000-000000000002"
UNKNOWN = "00000000-0000-4000-8000-000000000000"
RUN = "5f0a5b7e-0000-4000-8000-00000000000a"

#: Состояние «стенда»: тип задачи, статус, признак игнорирования `limit` и
#: поведение фильтров периода (живой стенд отклоняет «чистую» дату, `accept_bare_date`
#: позволяет проверить обратный вариант — это факт, а не отказ проверки).
#: `reverse_order` отдаёт задачи в обратном порядке (проверка факта «порядок не
#: гарантирован»), `short_answer` обрезает выдачу даже на большом `limit`.
STAND: dict[str, Any] = {
    "status": "running",
    "task_type": "celery-test",
    "ignore_limit": False,
    "accept_bare_date": False,
    "reverse_order": False,
    "short_answer": 0,
}


def card_payload(task_id: str, status: str, task_type: str = "celery-test") -> dict:
    """Карточка задачи (`TaskWithRuntimes`) с одним прогоном."""
    runtime = {
        "task_id": task_id,
        "status": status,
        "parameters": {"duration": 5},
        "id": RUN,
    }
    if status == "running":
        runtime["start_time"] = "2026-09-16T10:00:01"
        runtime["intermediate_result"] = {"progress": 40}
    return {
        "type": task_type,
        "name": "probe",
        "id": task_id,
        "created_at": "2026-09-16T10:00:00",
        "runtimes": [] if status == "empty" else [runtime],
    }


def handler(request: httpx.Request) -> httpx.Response:
    """Стенд задач для сценариев проверок."""
    path = request.url.path
    query = request.url.query.decode()
    if path == "/api/tasks/":
        for name in ("start_date", "end_date"):
            value = request.url.params.get(name)
            if value and "T" not in value and not STAND["accept_bare_date"]:
                return httpx.Response(
                    422,
                    json={
                        "detail": [
                            {
                                "type": "datetime_parsing",
                                "loc": ["query", name],
                                "msg": "Input should be a valid datetime, invalid datetime "
                                "separator, expected T, t, _ or space",
                            }
                        ]
                    },
                )
        tasks = [
            {
                "type": "celery-test",
                "name": "probe",
                "id": TASK,
                "created_at": "2026-09-16T10:00:00",
            },
            {
                "type": "training",
                "name": "train",
                "id": TASK2,
                "created_at": "2026-09-16T09:00:00",
            },
        ]
        count = len(tasks)
        if "task_type=celery-test" in query:
            tasks = [item for item in tasks if item["type"] == "celery-test"]
            count = len(tasks)
        if "task_type=training" in query:
            tasks = [item for item in tasks if item["type"] == "training"]
            count = len(tasks)
        if "limit=1" in query and not STAND["ignore_limit"]:
            offset = 1 if "offset=1" in query else 0
            tasks = tasks[offset : offset + 1]
        if STAND["reverse_order"]:
            tasks = list(reversed(tasks))
        if STAND["short_answer"]:
            tasks = tasks[: int(STAND["short_answer"])]
        return httpx.Response(200, json={"tasks": tasks, "count": count})
    if path.endswith("/pause"):
        STAND["status"] = "paused"
        return httpx.Response(200, json={})
    if path.endswith("/resume"):
        STAND["status"] = "running"
        return httpx.Response(200, json={})
    if path.endswith("/interrupt"):
        STAND["status"] = "interrupted"
        return httpx.Response(200, json={})
    if path == f"/api/tasks/{UNKNOWN}":
        return httpx.Response(404, json={"detail": "Task not found"})
    if path == f"/api/tasks/{TASK}":
        return httpx.Response(200, json=card_payload(TASK, STAND["status"], STAND["task_type"]))
    return httpx.Response(404, json={"detail": "Task not found"})


def spec_of(check_id: str) -> CheckSpec:
    """Описание проверки из каталога (в тестах оно всегда существует)."""
    spec = catalog.find(check_id)
    assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
    return spec


@pytest.fixture
def stand() -> tuple[check_tasks.AutomationContext, Journal]:
    """Контекст сценария: API задач, монитор, журнал и сессия испытаний."""
    STAND.update(
        {
            "status": "running",
            "task_type": "celery-test",
            "ignore_limit": False,
            "accept_bare_date": False,
            "reverse_order": False,
            "short_answer": 0,
        }
    )
    journal = Journal(max_records=100)
    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(handler)),
    )
    tasks_api = TasksApi(client)
    session = new_session(base_url="http://test.local")
    monitor = TaskMonitor(tasks_api, journal=journal, interval=0.05)
    return (
        check_tasks.AutomationContext(
            session=session,
            spec=spec_of("TC-TASK-02"),
            tasks=tasks_api,
            journal=journal,
            monitor=monitor,
            params={"task_id": TASK},
        ),
        journal,
    )


def context_for(
    stand: tuple[check_tasks.AutomationContext, Journal], check_id: str, **params
) -> check_tasks.AutomationContext:
    """Контекст для конкретной проверки (с общим стендом и сессией)."""
    base, _ = stand
    return check_tasks.AutomationContext(
        session=base.session,
        spec=spec_of(check_id),
        tasks=base.tasks,
        journal=base.journal,
        monitor=base.monitor,
        params={"task_id": TASK, **params},
    )


# ---------------------------------------------------------------------------
# TC-TASK-02 — список задач, фильтры и пагинация
# ---------------------------------------------------------------------------
def test_list_tasks_passes_on_healthy_stand(stand):
    """Список задач, фильтры и пагинация отвечают ожиданиям."""
    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-02"))

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["count"] == 2
    assert outcome.evidence["types"] == ["celery-test", "training"]
    assert outcome.evidence["limit=1"] == 1
    assert "статус задачи" in outcome.evidence["status_filter_note"]


def test_list_tasks_detects_broken_pagination(stand):
    """Игнорирование `limit` — отказ проверки с фактом, а не молчаливое «успех»."""
    STAND["ignore_limit"] = True

    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-02"))

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "пагинация не соблюдается" in outcome.verdict


def test_list_tasks_records_period_filters(stand):
    """Фильтры периода: границы как дата-время + факт отказа на «чистую» дату.

    Стенд отвечает 422 на `start_date=2026-09-09`, но это не дефект сервера
    (спецификация требует `format: date-time`), поэтому проверка остаётся
    пройденной, а факт уходит в протокол (и в замечание P2 о семантике границ).
    """
    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-02"))

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["filter_period"].endswith("→ 2")
    assert "T00:00:00" in outcome.evidence["filter_period"]
    assert "created_at<" in outcome.evidence["filter_period_yesterday"]
    assert outcome.evidence["date_only_probe"].startswith("422")
    assert "date-time" in outcome.evidence["date_only_note"]


def test_list_tasks_records_missing_status_in_list(stand):
    """Состав элемента списка фиксируется: статуса в нём нет — только в карточке.

    Пробел API («статус списка требует N запросов на N задач») должен попадать в
    доказательства проверки, а не выясняться оператором на живом стенде.
    """
    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-02"))

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED, outcome.verdict
    assert "status" not in outcome.evidence["list_fields"]
    assert set(outcome.evidence["list_fields"]) == {
        "type",
        "name",
        "description",
        "id",
        "created_at",
    }
    assert "N запросов" in outcome.evidence["status_in_list"]


def test_list_tasks_records_uncapped_limit_and_local_archive(stand):
    """Факты для отчёта: список читается целиком, архив ведёт пульт (TC-TASK-02).

    Экран «Задачи» показывает виды «Активные | Архив | Все» и клиентскую пагинацию,
    поэтому проверка обязана фиксировать два свойства API: верхняя граница `limit`
    не объявлена (список читается одним запросом) и архива задач в API нет — правило
    «завершена ≥ суток назад» считает пульт по времени прогона карточки.
    """
    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-02"))

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["limit_uncapped"] == f"limit={check_tasks.LIST_PROBE_LIMIT} → 2 из 2"
    assert "не отсортирован" not in outcome.evidence["list_unsorted"]
    assert "пульт сортирует" in outcome.evidence["list_unsorted"]
    assert "task_snapshot.json" in outcome.evidence["archive_is_local"]
    assert "нет ни признака архива" in outcome.evidence["archive_is_local"]
    assert "другие" not in outcome.evidence["list_sort_note"]
    assert "только для проверки пагинации" in outcome.evidence["list_sort_note"]


def test_list_tasks_notes_unsorted_server_order(stand):
    """Порядок ответа не гарантирован: если сервер отдал не по дате, это факт, не отказ."""
    STAND["reverse_order"] = True

    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-02"))

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED, outcome.verdict
    assert "не отсортирован" in outcome.evidence["list_unsorted"]
    assert "создана ↓" in outcome.evidence["list_unsorted"]


def test_list_tasks_detects_list_that_cannot_be_read_fully(stand):
    """Список, который не читается целиком, — отказ проверки (виды и архив не собрать)."""
    STAND["short_answer"] = 1

    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-02"))

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "список нельзя прочитать целиком" in outcome.verdict


def test_list_tasks_notes_stand_that_accepts_bare_date(stand):
    """Если стенд принял «чистую» дату, это факт для отчёта, а не отказ проверки."""
    STAND["accept_bare_date"] = True

    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-02"))

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["date_only_probe"].startswith("200")


# ---------------------------------------------------------------------------
# TC-TASK-03 — карточка задачи с прогонами
# ---------------------------------------------------------------------------
def test_task_card_passes_with_runtimes(stand):
    """Карточка задачи приходит с прогонами и известным статусом."""
    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-03"))

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED
    assert outcome.evidence["last_status"] == "running"
    assert outcome.evidence["runtimes"] == 1
    assert "Приостановлена" not in outcome.verdict


def test_task_card_fails_without_runtimes(stand):
    """Пустой `runtimes[]` — отказ: задача без прогонов бесполезна для отчёта."""
    STAND["status"] = "empty"

    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-03"))

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "runtimes[] пуст" in outcome.verdict


def test_task_card_fails_on_status_outside_fsm(stand):
    """Статус вне FSM-1 отсекается схемой задачи: проверка сообщает факт, а не молчит."""
    STAND["status"] = "почти-готово"

    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-03"))

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED, outcome.verdict
    assert "не по схеме" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-TASK-01 — запуск диагностической задачи (запускает оператор)
# ---------------------------------------------------------------------------
def test_run_test_task_verdict_uses_created_task(stand):
    """Вердикт TC-TASK-01 строится по созданной и поставленной на наблюдение задаче."""
    context = context_for(stand, "TC-TASK-01")
    context.monitor.register(context.session, task_id=TASK, task_type="celery-test")
    context.monitor.poll_once(context.session, TASK)

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED
    assert outcome.evidence["type"] == "celery-test"
    assert outcome.evidence["observed"] is True
    assert outcome.evidence["journal_from"] == 1


def test_run_test_task_skipped_without_task(stand):
    """Без выбранной задачи проверка «пропущена» — это не сбой пульта."""
    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-01", task_id=""))

    assert outcome is not None
    assert outcome.status == CheckStatus.SKIPPED
    assert "не выбран task_id" in outcome.verdict


def test_run_test_task_fails_for_other_type(stand):
    """Задача другого типа не заменяет диагностическую `celery-test`."""
    STAND["task_type"] = "training"
    context = context_for(stand, "TC-TASK-01")
    context.monitor.register(context.session, task_id=TASK, task_type="training")
    context.monitor.poll_once(context.session, TASK)

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "celery-test" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-TASK-04/05 — вердикты по истории переходов FSM-1
# ---------------------------------------------------------------------------
def test_fsm_transitions_verdict_follows_observed_chain(stand):
    """Цепочка `pause → resume` из истории наблюдения делает проверку успешной."""
    context = context_for(stand, "TC-TASK-04")
    context.monitor.register(context.session, task_id=TASK)
    context.monitor.poll_once(context.session, TASK)
    context.monitor.run_command(context.session, TASK, "pause")
    context.monitor.run_command(context.session, TASK, "resume")

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED
    assert outcome.evidence["observed"] == ["running", "paused", "running"]
    assert "не попали в опрос" in outcome.verdict  # `pausing` не наблюдался поллингом


def test_fsm_transitions_failed_without_pause(stand):
    """Без наблюдённой паузы переходы FSM-1 не подтверждены."""
    context = context_for(stand, "TC-TASK-04")
    context.monitor.register(context.session, task_id=TASK)
    context.monitor.poll_once(context.session, TASK)

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "не наблюдались переходы" in outcome.verdict


def test_interrupt_verdict(stand):
    """Вердикт TC-TASK-05 — факт перехода в терминальный `interrupted`."""
    context = context_for(stand, "TC-TASK-05")
    context.monitor.register(context.session, task_id=TASK)
    context.monitor.poll_once(context.session, TASK)
    context.monitor.run_command(context.session, TASK, "interrupt")

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED
    assert "Прервана" in outcome.verdict

    missing = check_tasks.evaluate(context_for(stand, "TC-TASK-05", task_id=TASK2))
    assert missing is not None and missing.status == CheckStatus.FAILED


# ---------------------------------------------------------------------------
# TC-TASK-06 — идемпотентность команд и запрет из терминального состояния
# ---------------------------------------------------------------------------
def test_idempotency_accepts_noop_repeat(stand):
    """Второй `pause` не даёт побочного эффекта — проверка пройдена с фактом «no-op»."""
    context = context_for(stand, "TC-TASK-06", action="pause")
    context.monitor.register(context.session, task_id=TASK)
    context.monitor.poll_once(context.session, TASK)

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED
    assert "no-op" in outcome.verdict
    assert outcome.evidence["first"]["sent"] is True
    assert outcome.evidence["second"]["sent"] is True
    assert outcome.evidence["status_chain"] == ["running", "paused"]


def test_idempotency_detects_side_effect(stand):
    """Повторная команда с переходом дважды — отказ (BR-R3: повторов быть не должно)."""
    calls = {"count": 0}

    def toggling(request: httpx.Request) -> httpx.Response:
        """Стенд, который «переключает» статус на каждый `pause`."""
        if request.url.path.endswith("/pause"):
            calls["count"] += 1
            STAND["status"] = "paused" if calls["count"] % 2 else "running"
            return httpx.Response(200, json={})
        return handler(request)

    base, journal = stand
    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(toggling)),
    )
    context = check_tasks.AutomationContext(
        session=base.session,
        spec=spec_of("TC-TASK-06"),
        tasks=TasksApi(client),
        journal=journal,
        monitor=TaskMonitor(TasksApi(client), journal=journal),
        params={"task_id": TASK, "action": "pause"},
    )
    context.monitor.register(context.session, task_id=TASK)
    context.monitor.poll_once(context.session, TASK)

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "второй переход" in outcome.verdict


def test_idempotency_rejects_unknown_command(stand):
    """Команда вне FSM-1 не запускает сценарий: проверка сообщает об ошибке вызова."""
    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-06", action="restart"))

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "не управляет задачей" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-TASK-07 — неизвестная задача
# ---------------------------------------------------------------------------
def test_unknown_task_accepts_404(stand):
    """404 на неизвестный `task_id` — допустимый вариант (BR-R7)."""
    outcome = check_tasks.evaluate(
        context_for(stand, "TC-TASK-07", task_id=UNKNOWN, unknown_task_id=UNKNOWN)
    )

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED
    assert outcome.evidence["variant"] == "404"


def test_unknown_task_accepts_not_found_status(stand):
    """Статус `not_found` в карточке — основной вариант ответа сервера (BR-R7)."""

    def not_found_stand(request: httpx.Request) -> httpx.Response:
        """Стенд, который отдаёт `not_found` вместо 404."""
        if request.url.path == f"/api/tasks/{UNKNOWN}":
            return httpx.Response(
                200,
                json={
                    "type": "celery-test",
                    "name": "probe",
                    "id": UNKNOWN,
                    "created_at": "2026-09-16T10:00:00",
                    "runtimes": [
                        {
                            "task_id": UNKNOWN,
                            "status": "not_found",
                            "parameters": {},
                            "id": RUN,
                        }
                    ],
                },
            )
        return handler(request)

    base, journal = stand
    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(not_found_stand)),
    )
    context = check_tasks.AutomationContext(
        session=base.session,
        spec=spec_of("TC-TASK-07"),
        tasks=TasksApi(client),
        journal=journal,
        monitor=None,
        params={"unknown_task_id": UNKNOWN},
    )

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["variant"] == "not_found"


def test_unknown_task_detects_wrong_answer(stand):
    """Существующая задача вместо `not_found` — отказ проверки."""
    outcome = check_tasks.evaluate(context_for(stand, "TC-TASK-07", unknown_task_id=TASK))

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "вместо not_found" in outcome.verdict


def test_unknown_task_schema_error_is_reported(stand):
    """Ответ не по схеме задачи (не-UUID) — отказ с понятным фактом."""

    def broken_stand(request: httpx.Request) -> httpx.Response:
        """Стенд, который отвечает карточкой на любой идентификатор."""
        return httpx.Response(
            200,
            json={
                "type": "celery-test",
                "name": "probe",
                "id": "не-uuid",
                "created_at": "2026-09-16T10:00:00",
                "runtimes": [],
            },
        )

    base, journal = stand
    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(broken_stand)),
    )
    context = check_tasks.AutomationContext(
        session=base.session,
        spec=spec_of("TC-TASK-07"),
        tasks=TasksApi(client),
        journal=journal,
        monitor=None,
        params={"unknown_task_id": "не-uuid"},
    )

    outcome = check_tasks.evaluate(context)

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert outcome.evidence["variant"] == "schema_error"


# ---------------------------------------------------------------------------
# TC-TASK-08 — наблюдение за внешней задачей (подтверждает оператор)
# ---------------------------------------------------------------------------
def test_external_observation_verdict(stand):
    """Внешняя задача с полученными статусами подтверждается как выполненная вручную."""
    context = context_for(stand, "TC-TASK-08")
    context.monitor.register_external(context.session, TASK)
    context.monitor.poll_once(context.session, TASK)

    outcome = check_tasks.evaluate(context, automation="tasks.external_observation")

    assert outcome is not None
    assert outcome.status == CheckStatus.MANUAL_OK
    assert outcome.evidence["origin"] == "внешняя"
    assert outcome.evidence["chain"] == ["running"]
    assert "Выполняется" in outcome.verdict


def test_external_observation_requires_external_origin(stand):
    """Пультовая задача не подтверждает TC-TASK-08: происхождение не «внешняя»."""
    context = context_for(stand, "TC-TASK-08")
    context.monitor.register(context.session, task_id=TASK)
    context.monitor.poll_once(context.session, TASK)

    outcome = check_tasks.evaluate(context, automation="tasks.external_observation")

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "не помечена как «внешняя»" in outcome.verdict


def test_external_observation_fails_without_polls(stand):
    """Внешняя задача без успешных опросов не подтверждается."""
    context = context_for(stand, "TC-TASK-08")
    context.monitor.register_external(context.session, TASK)

    outcome = check_tasks.evaluate(context, automation="tasks.external_observation")

    assert outcome is not None
    assert outcome.status == CheckStatus.FAILED
    assert "статусы внешней задачи не получены" in outcome.verdict


# ---------------------------------------------------------------------------
# Движок: прогон сценария и запись результата
# ---------------------------------------------------------------------------
def test_automate_records_result_with_journal_range(stand):
    """`automate` прогоняет сценарий, пишет результат в сессию и границы журнала."""
    base, journal = stand
    context = context_for(stand, "TC-TASK-03")

    result = check_tasks.automate(context)

    assert result is not None
    assert result.status == CheckStatus.PASSED
    assert result.params["task_id"] == TASK
    assert result.journal_from == 1
    assert result.journal_to is not None
    assert result.journal_to >= result.journal_from
    assert journal.for_label("TC-TASK-03")  # обмены помечены меткой проверки

    stored = base.session.checks[-1]
    assert stored["check_id"] == "TC-TASK-03"
    assert stored["status"] == str(CheckStatus.PASSED)
    assert stored["journal_to"] == result.journal_to
    assert "check_recorded" in [item["event"] for item in base.session.history]


def test_automate_returns_none_for_manual_check(stand):
    """У ручной проверки (TC-TASK-08) автоматического сценария нет."""
    base, _ = stand
    context = context_for(stand, "TC-TASK-08")

    assert check_tasks.automate(context) is None
    assert base.session.checks == []
    assert check_tasks.scenario(catalog.find("TC-TASK-08")) is None


def test_evaluate_without_scenario_returns_none(stand):
    """Неизвестный ключ сценария не запускает проверку."""
    assert check_tasks.evaluate(context_for(stand, "TC-TASK-02"), automation="tasks.nope") is None


def test_chain_outcome_reports_missing_and_optional_steps():
    """Проверка цепочки FSM-1: обязательные переходы и «неуловимые» шаги поллинга."""
    passed = check_tasks.chain_outcome(["running", "paused", "running"], ("running", "paused"))
    optional = check_tasks.chain_outcome(
        ["running", "paused"], ("running", "pausing", "paused"), optional=("pausing",)
    )
    failed = check_tasks.chain_outcome(
        ["running"], ("running", "paused", "running"), optional=("pausing",)
    )

    assert passed.status == CheckStatus.PASSED
    assert optional.status == CheckStatus.PASSED
    assert "Приостановка" in optional.verdict or "не попали в опрос" in optional.verdict
    assert failed.status == CheckStatus.FAILED
    assert failed.evidence["observed"] == ["running"]
    assert check_tasks.observed_chain(new_session(base_url="http://t"), "нет-задачи") == []


def test_setup_helpers_wrap_check_automation(stand):
    """Сценарии доступны по ключам, объявленным в каталоге."""
    for spec in catalog.by_group("TC-TASK"):
        if spec.automation:
            assert check_tasks.scenario(spec) is not None
    assert set(check_tasks.AUTOMATIONS) >= {
        "tasks.run_test_task",
        "tasks.list_tasks",
        "tasks.task_card",
        "tasks.fsm_transitions",
        "tasks.interrupt",
        "tasks.idempotency",
        "tasks.unknown_task",
        "tasks.external_observation",
    }
