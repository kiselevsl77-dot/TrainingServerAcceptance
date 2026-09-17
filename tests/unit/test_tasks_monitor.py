"""Тесты монитора задач (FR-8, FR-T11; BR-R5/BR-R6; этап T4).

Монитор — фундамент всех асинхронных проверок, поэтому фиксируются его правила:

    * наблюдение живёт в сессии и переживает перезапуск пульта (BR-R5, BR-R6);
    * статус берётся из последнего прогона (`runtimes[]`), `not_found` — результат, а не сбой;
    * команды FSM-1 отправляются только по `available_actions`, кроме осознанной проверки
      идемпотентности (TC-TASK-06), а эффект команды определяется повторным опросом;
    * обрыв связи не роняет пульт и не теряет задачу — ошибка опроса пишется в историю.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import (
    ORIGIN_EXTERNAL,
    find_task,
    new_session,
    task_history,
)
from acceptance.session import (
    TestSession as SessionModel,
)
from acceptance.task_snapshot import TaskSnapshotEntry
from acceptance.tasks_monitor import (
    MONITOR_LABEL,
    SOURCE_CARD,
    SOURCE_NONE,
    SOURCE_POLL,
    SOURCE_SNAPSHOT,
    STATUS_ORDER,
    TaskMonitor,
    TaskObservation,
    current_status,
    is_terminal_status,
    last_intermediate,
    runtime_row,
    short_body,
    status_color,
    status_glyph,
    status_icon,
    status_label,
    status_legend,
    status_text,
    task_card,
    task_id_from_payload,
    task_status_view,
)
from client.http import ApiHttpClient
from client.tasks import TasksApi

TASK = "9f0a5b7e-0000-4000-8000-000000000001"
TASK2 = "9f0a5b7e-0000-4000-8000-000000000002"
UNKNOWN = "00000000-0000-4000-8000-000000000000"
RUN = "5f0a5b7e-0000-4000-8000-00000000000a"

#: Текущий статус «стенда» задач: команды меняют его так же, как настоящий сервер.
STATUS = {"value": "running"}


def card_payload(task_id: str, status: str, *, task_type: str = "celery-test") -> dict:
    """Ответ `GET /api/tasks/{task_id}` с одним прогоном (`TaskWithRuntimes`)."""
    runtime: dict = {
        "task_id": task_id,
        "status": status,
        "parameters": {"duration": 5},
        "id": RUN,
    }
    if status != "new":
        runtime["start_time"] = "2026-09-16T10:00:01"
    if status in ("completed", "failed", "interrupted"):
        runtime["end_time"] = "2026-09-16T10:00:09"
        runtime["result"] = {"files": ["/api/data/files?file_name=a"]}
    if status == "running":
        runtime["intermediate_result"] = {"progress": 40}
    return {
        "type": task_type,
        "name": "probe",
        "id": task_id,
        "created_at": "2026-09-16T10:00:00",
        "runtimes": [runtime],
    }


def handler(request: httpx.Request) -> httpx.Response:
    """Стенд задач: список, карточка, команды FSM-1 и «неизвестная» задача."""
    path = request.url.path
    if path == "/api/tasks/":
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "type": "celery-test",
                        "name": "probe",
                        "id": TASK,
                        "created_at": "2026-09-16T10:00:00",
                    }
                ],
                "count": 1,
            },
        )
    if path.endswith("/pause"):
        STATUS["value"] = "paused"
        return httpx.Response(200, json={})
    if path.endswith("/resume"):
        STATUS["value"] = "running"
        return httpx.Response(200, json={})
    if path.endswith("/interrupt"):
        STATUS["value"] = "interrupted"
        return httpx.Response(200, json={})
    if path == f"/api/tasks/{TASK}":
        return httpx.Response(200, json=card_payload(TASK, STATUS["value"]))
    if path == f"/api/tasks/{UNKNOWN}":
        return httpx.Response(200, json=card_payload(UNKNOWN, "not_found"))
    if path == f"/api/tasks/{TASK2}":
        return httpx.Response(404, json={"detail": "Task not found"})
    if path.startswith("/api/tasks/"):
        # «стенд» отвечает карточкой на любой идентификатор: не-UUID ловится схемой клиента
        probe = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=card_payload(probe, "new"))
    return httpx.Response(404, json={"detail": f"неожиданный путь {path}"})


@pytest.fixture
def wired() -> tuple[TaskMonitor, SessionModel, Journal]:
    """Монитор с логирующим транспортом, сессия испытаний и журнал обмена."""
    journal = Journal(max_records=200)
    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(handler)),
    )
    monitor = TaskMonitor(TasksApi(client), journal=journal, interval=0.05)
    STATUS["value"] = "running"
    return monitor, new_session(base_url="http://test.local"), journal


def test_register_and_poll_once_records_transition(wired):
    """Регистрация и опрос пишут задачу и переход FSM-1 в сессию и журнал."""
    monitor, session, journal = wired

    monitor.register(session, task_id=TASK, task_type="celery-test", check_id="TC-TASK-04")
    poll = monitor.poll_once(session, TASK)

    assert poll.ok
    assert poll.status == "running"
    assert poll.changed
    assert not poll.terminal
    assert poll.journal_seq == 1
    assert journal.records[-1].label == "TC-TASK-04"
    assert find_task(session, TASK)["poll"]["polls"] == 1

    observation = monitor.observed(session)[0]
    assert observation.status_label == "Выполняется"
    assert observation.actions == ("pause", "interrupt")
    assert not observation.is_terminal


def test_poll_detects_status_chain_and_keeps_history(wired):
    """Цепочка FSM-1 собирается из опросов: running → paused → running."""
    monitor, session, journal = wired
    monitor.register(session, task_id=TASK)
    monitor.poll_once(session, TASK)

    paused = monitor.run_command(session, TASK, "pause")
    resumed = monitor.run_command(session, TASK, "resume")

    assert "переход: Выполняется → Приостановлена" in paused.variant
    assert "переход: Приостановлена → Выполняется" in resumed.variant
    assert journal.records[-1].label == MONITOR_LABEL
    assert [item["status"] for item in task_history(session, TASK)] == [
        "",
        "running",
        "paused",
        "running",
    ]


def test_terminal_status_stops_polling(wired):
    """Терминальный статус останавливает наблюдение, но сохраняет диагностику."""
    monitor, session, journal = wired
    monitor.register(session, task_id=TASK)
    monitor.poll_once(session, TASK)

    stopped = monitor.run_command(session, TASK, "interrupt")

    task = find_task(session, TASK)
    assert stopped.poll is not None and stopped.poll.terminal
    assert task["status"] == "interrupted"
    assert task["poll"]["active"] is False
    assert "терминальный" in task["note"]
    assert monitor.due(session) == []
    assert status_icon("interrupted") == "⛔"
    assert journal.for_label(MONITOR_LABEL)


def test_command_only_from_available_actions(wired):
    """Команда не отправляется, если запрещена текущим статусом (кроме force)."""
    monitor, session, _ = wired
    monitor.register(session, task_id=TASK)
    monitor.poll_once(session, TASK)

    skipped = monitor.run_command(session, TASK, "resume")

    assert not skipped.allowed
    assert not skipped.sent
    assert "запрещена" in skipped.variant
    assert "pause" in skipped.note
    assert [item["event"] for item in session.history].count("task_command") == 1
    assert session.history[-1]["payload"]["variant"].startswith("команда не отправлена")


def test_forced_command_fixes_variant_for_idempotency(wired):
    """Осознанная отправка вне состояния фиксирует вариант «no-op» (TC-TASK-06)."""
    monitor, session, _ = wired
    monitor.register(session, task_id=TASK)
    monitor.poll_once(session, TASK)

    forced = monitor.run_command(session, TASK, "resume", force=True)

    assert forced.allowed is False
    assert forced.sent is True
    assert "проверка идемпотентности" in forced.variant
    assert "no-op" in forced.variant
    assert forced.status == 200
    assert forced.body == {}


def test_command_http_error_is_recorded_not_raised(wired):
    """Ответ `409` не роняет пульт: вариант поведения фиксируется (BR-R3)."""
    monitor, session, journal = wired

    def reject(request: httpx.Request) -> httpx.Response:
        """Стенд, который отклоняет команду `pause`, но отдаёт карточку задачи."""
        if request.url.path.endswith("/pause"):
            return httpx.Response(409, json={"detail": "задача не приостановлена"})
        return handler(request)

    client = ApiHttpClient(
        base_url="http://test.local", timeout=5.0, transport=httpx.MockTransport(reject)
    )
    monitor = TaskMonitor(TasksApi(client), journal=journal)
    monitor.register(session, task_id=TASK)
    monitor.poll_once(session, TASK)

    command = monitor.run_command(session, TASK, "pause")

    assert command.sent
    assert command.status == 409
    assert "HTTP 409" in command.variant
    assert not command.ok
    assert find_task(session, TASK)["status"] == "running"  # статус не изменился


def test_unknown_task_and_missing_task_id(wired):
    """`not_found` — результат проверки (BR-R7), неправильный `task_id` — понятная ошибка."""
    monitor, session, _ = wired

    monitor.register(session, task_id=UNKNOWN)
    unknown = monitor.poll_once(session, UNKNOWN)
    assert unknown.ok
    assert unknown.unknown
    assert unknown.terminal
    assert find_task(session, UNKNOWN)["note"].startswith("задача не найдена")

    monitor.register(session, task_id=TASK2)
    broken = monitor.poll_once(session, TASK2)
    assert not broken.ok
    assert "404" in broken.error
    assert find_task(session, TASK2)["errors"]

    monitor.register(session, task_id="не-uuid")
    invalid = monitor.poll_once(session, "не-uuid")
    assert not invalid.ok
    assert "некорректный task_id" in invalid.error

    missing = monitor.poll_once(session, "нет-такой-задачи")
    assert not missing.ok
    assert "не наблюдается" in missing.error


def test_network_failure_keeps_observation(wired):
    """Обрыв связи не теряет задачу: наблюдение сохраняется, ошибка пишется в историю (BR-R5)."""
    monitor, session, journal = wired

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("соединение разорвано", request=request)

    client = ApiHttpClient(
        base_url="http://test.local", timeout=5.0, transport=httpx.MockTransport(failing)
    )
    monitor = TaskMonitor(TasksApi(client), journal=journal)
    monitor.register(session, task_id=TASK)

    poll = monitor.poll_once(session, TASK)

    assert not poll.ok
    assert "сервер недоступен" in poll.error
    task = find_task(session, TASK)
    assert task["poll"]["polls"] == 0  # неудачный опрос не считается
    assert task["errors"][-1]["error"].startswith("сервер недоступен")
    assert "task_poll_error" in [item["event"] for item in session.history]


def test_watch_reaches_terminal_status(wired):
    """`watch` доводит задачу до терминального статуса блокирующим поллингом."""
    monitor, session, _ = wired
    monitor.register(session, task_id=TASK)
    calls = {"count": 0}

    def handler_with_finish(request: httpx.Request) -> httpx.Response:
        """Стенд, который завершает задачу после второго опроса."""
        if request.url.path == f"/api/tasks/{TASK}":
            calls["count"] += 1
            status = "completed" if calls["count"] >= 2 else "running"
            return httpx.Response(200, json=card_payload(TASK, status))
        return handler(request)

    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=httpx.MockTransport(handler_with_finish),
    )
    monitor = TaskMonitor(TasksApi(client), interval=0.01)

    result = monitor.watch(session, TASK, interval=0.01, timeout=2.0)

    assert result is not None
    assert result.status == "completed"
    assert result.terminal
    assert calls["count"] == 2
    # последний известный промежуточный результат сохраняется в наблюдении
    assert find_task(session, TASK)["intermediate_result"] == {"progress": 40}


def test_observation_management_and_kpi(wired):
    """Управление наблюдением: пауза поллинга, интервал, привязка, снятие, KPI."""
    monitor, session, _ = wired
    monitor.register(session, task_id=TASK, check_id="TC-TASK-04")
    monitor.poll_once(session, TASK)

    assert monitor.set_active(session, TASK, False) is not None
    assert monitor.observed(session, active_only=True) == []
    monitor.set_active(session, TASK, True)
    monitor.set_interval(session, TASK, 7.5)
    assert monitor.attach_to_check(session, TASK, "TC-TASK-05")["check_id"] == "TC-TASK-05"
    assert monitor.set_interval(session, "нет-такой", 1.0) is None
    assert monitor.attach_to_check(session, "нет-такой", "TC-TASK-01") is None
    assert monitor.set_active(session, "нет-такой", True) is None

    kpi = monitor.kpi(session)
    assert kpi["total"] == 1
    assert kpi["active"] == 1
    assert kpi["external"] == 0

    assert monitor.unregister(session, TASK)
    assert monitor.observed(session) == []
    assert not monitor.unregister(session, TASK)
    assert "task_unobserved" in [item["event"] for item in session.history]


def test_adopt_from_session_picks_up_console_tasks(wired):
    """Задачи, созданные до монитора (консоль, перезапуск), подхватываются из сессии."""
    monitor, session, _ = wired
    session.add_history("console_task_started", "Задача создана из консоли: task-42")
    session.console_calls.append(
        {
            "at": "2026-09-16T10:00:00",
            "label": "TC-TASK-01",
            "operation": "post /api/tasks/test",
            "task_id": "task-42",
            "journal_seq": 4,
        }
    )

    adopted = monitor.adopt_from_session(session)

    assert adopted[0]["task_id"] == "task-42"
    task = find_task(session, "task-42")
    assert task["check_id"] == "TC-TASK-01"
    assert task["journal_from"] == 4
    assert monitor.adopt_from_session(session) == []  # повторный вход не дублирует


def test_due_respects_interval_and_pause(wired):
    """Автоопрос идёт по интервалу наблюдения и уважает паузу поллинга."""
    monitor, session, _ = wired
    monitor.register(session, task_id=TASK)
    assert monitor.due(session) == [TASK]  # задача ещё ни разу не опрошена

    monitor.set_interval(session, TASK, 60.0)
    monitor.poll_once(session, TASK)
    assert monitor.due(session) == []  # интервал ещё не истёк
    future = datetime.now() + timedelta(seconds=120)
    assert monitor.due(session, moment=future) == [TASK]

    monitor.set_active(session, TASK, False)
    assert monitor.due(session, moment=future) == []


def test_helpers_render_task_data(wired):
    """Помощники монитора: статусы, карточка, идентификатор задачи, тела ответов."""
    monitor, session, _ = wired
    monitor.register(session, task_id=TASK, task_type="celery-test")
    poll = monitor.poll_once(session, TASK)

    assert poll.task is not None
    assert current_status(poll.task) == "running"
    assert status_label("unknown-status") == "unknown-status"
    assert status_icon("unknown-status") == "⚪"
    assert is_terminal_status("pausing") is False
    assert is_terminal_status("нет-такого") is False

    row = runtime_row(poll.task.runtimes[0])
    assert row["status"] == "running"
    assert row["parameters"] == {"duration": 5}
    assert row["start_time"] == "2026-09-16T10:00:01"
    assert last_intermediate(poll.task) == {"progress": 40}

    snapshot = monitor.card_snapshot(poll.task)
    assert snapshot["task_id"] == TASK
    assert snapshot["runtimes"][0]["status_label"] == "Выполняется"

    assert task_id_from_payload({"task_id": TASK}) == TASK
    assert task_id_from_payload({"task": TASK}) == TASK
    assert task_id_from_payload({"id": TASK}) == TASK
    assert task_id_from_payload(poll.task) == TASK
    assert task_id_from_payload([1, 2]) == ""

    assert short_body({"a": "x" * 10})["a"] == "x" * 10
    assert short_body("y" * 500) == "y" * 400


def test_find_recent_returns_server_tasks(wired):
    """Поиск последних задач сервера — обходной путь для операций без `task_id` (P1)."""
    monitor, _, _ = wired

    tasks = monitor.find_recent(task_type="celery-test", limit=1)

    assert [str(task.id) for task in tasks] == [TASK]


def test_observation_from_record_tolerates_partial_data():
    """Строка наблюдения строится из неполной записи (совместимость схемы v4)."""
    observation = TaskObservation.from_record({"task_id": "task-x"})

    assert observation.task_type == ""
    assert observation.status_label == "неизвестен"
    assert observation.actions == ()
    assert observation.errors == ()
    assert not observation.is_terminal


def test_poll_all_and_session_persistence(wired, tmp_path: Path):
    """`poll_all` опрашивает наблюдаемые задачи, результат сохраняется в файл сессии."""
    from acceptance.session import load_session, save_session

    monitor, session, _ = wired
    monitor.register(session, task_id=TASK)

    polls = monitor.poll_all(session)

    assert len(polls) == 1 and polls[0].ok
    save_session(session, tmp_path)
    restored = load_session(session.session_id, tmp_path)
    restored_task = find_task(restored, TASK)
    assert restored_task is not None
    assert restored_task["status"] == "running"
    assert monitor.poll_all(restored, label="TC-TASK-04")[0].label == "TC-TASK-04"


def test_register_from_response_and_external_origin(wired):
    """Задачи из ответов API и внешние задачи различаются по происхождению."""
    monitor, session, _ = wired

    assert monitor.register_from_response(session, {"status": "ok"}) is None
    record = monitor.register_from_response(session, {"task_id": TASK}, task_type="celery-test")
    assert record is not None and record["origin"] == "пульт"

    monitor.register_external(session, UNKNOWN)
    external = monitor.observed(session)
    assert [item.origin for item in external] == ["пульт", ORIGIN_EXTERNAL]
    assert monitor.kpi(session)["external"] == 1


def test_observation_index_gives_status_without_requests(wired):
    """Индекс наблюдений даёт статус для списка задач без запросов к серверу.

    `GET /api/tasks/` статус не возвращает, поэтому экран «Задачи» берёт статус
    строки списка из последнего опроса монитора; задачи без наблюдения в индекс
    не попадают, а список без сессии даёт пустой индекс.
    """
    monitor, session, _ = wired
    monitor.register(session, task_id=TASK, task_type="celery-test", name="probe")
    monitor.register_external(session, UNKNOWN)
    monitor.poll_once(session, TASK)

    index = monitor.observation_index(session)

    assert set(index) == {TASK, UNKNOWN}
    assert index[TASK].status == "running"
    assert index[TASK].status_label == "Выполняется"
    assert index[TASK].name == "probe"
    assert index[UNKNOWN].status == ""
    assert monitor.observation_index(None) == {}

    unknown_key = "00000000-0000-4000-8000-00000000000f"
    assert unknown_key not in index


def test_status_glyphs_colors_and_legend():
    """Пиктограммы и цвета статусов: один источник для таблицы, отчёта и легенды.

    Таблица списка должна читаться «с одного взгляда»: `▶` — исполняется, `✔`/`✖` —
    чем закончилась, `?` — статус неизвестен. Цвет даёт второй канал различения,
    поэтому он объявлен рядом с пиктограммой (`lib.task_status`).
    """
    assert status_glyph("running") == "▶"
    assert status_glyph("completed") == "✔"
    assert status_glyph("failed") == "✖"
    assert status_glyph("") == "?"
    assert status_glyph("нет-такого-статуса") == "?"

    assert status_color("running") == "green"
    assert status_color("completed") == "green"
    assert status_color("failed") == "red"
    assert status_color("paused") == "yellow"
    assert status_color("interrupted") == "orange"
    assert status_color("") == "grey"

    assert status_text("completed") == "✔ Завершена"
    assert status_text("") == "? неизвестен"

    legend = status_legend()
    assert legend.startswith("○ новая · ▶ выполняется")
    assert "⏹ прервана" in legend
    assert legend.count("·") == len(STATUS_ORDER) - 1


def test_task_card_projection_for_the_list(wired):
    """Проекция карточки: в таблицу идут статус, тип, название и времена прогона."""
    monitor, session, _ = wired
    monitor.register(session, task_id=TASK)
    poll = monitor.poll_once(session, TASK)

    card = task_card(poll.task)

    assert card["status"] == "running"
    assert card["type"] == "celery-test"
    assert card["name"] == "probe"
    assert card["start_time"] == "2026-09-16T10:00:01"
    assert card["end_time"] == ""
    assert set(card) == {"status", "type", "name", "start_time", "end_time"}


def test_task_status_view_priority_card_snapshot_poll():
    """Статус строки: карточка сервера → снимок → последний опрос сессии.

    Приоритет важен для честности интерфейса: свежий ответ карточки важнее записи
    снимка, а снимок (последний известный статус) — важнее старого опроса сессии.
    Пульт не показывает выдуманный статус: если нет ни одного источника, колонка
    «Статус» получает «нет данных», а «Источник» — `нет данных`.
    """
    entry = TaskSnapshotEntry(task_id=TASK, status="completed", end_time="2026-09-16T09:00:00")
    card = {
        "status": "running",
        "start_time": "2026-09-16T11:00:00",
        "fetched_at": "2026-09-16T11:00:01",
    }

    from_card = task_status_view(card=card, snapshot=entry, poll_status="paused")

    assert from_card.status == "running"
    assert from_card.source == SOURCE_CARD
    assert from_card.start_time == "2026-09-16T11:00:00"
    assert from_card.end_time == "2026-09-16T09:00:00"
    assert from_card.text == "▶ Выполняется"
    assert from_card.color == "green"
    assert from_card.is_terminal is False

    from_snapshot = task_status_view(snapshot=entry, poll_status="paused")

    assert from_snapshot.status == "completed"
    assert from_snapshot.source == SOURCE_SNAPSHOT
    assert from_snapshot.is_terminal is True
    assert from_snapshot.observed_at == entry.fetched_at

    from_poll = task_status_view(poll_status="pausing")

    assert from_poll.status == "pausing"
    assert from_poll.source == SOURCE_POLL
    assert from_poll.known is True

    empty = task_status_view()

    assert empty.status == ""
    assert empty.known is False
    assert empty.source == SOURCE_NONE
    assert empty.text == "? неизвестен"
    assert empty.color == "grey"


def test_task_status_view_keeps_card_error_for_hint():
    """Ошибка карточки сохраняется в строке: оператор видит, почему статус не свежий."""
    view = task_status_view(
        card={"status": "", "error": "HTTP 500", "fetched_at": "2026-09-16T11:00:01"}
    )

    assert view.known is False
    assert view.source == SOURCE_NONE
    assert view.error == "HTTP 500"
