"""Тесты клиента сервиса задач (FR-T4, UC-26…UC-28).

Фиксируются особенности, важные для испытаний: `duration` — обязательный
query-параметр тестовой задачи, команды управления передаются без тела,
неизвестный `task_id` отдаётся сервером как `not_found` (BR-R7), а не 404.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest

from client.errors import NotFoundError
from client.tasks import TasksApi
from lib.period import day_bounds


def test_run_test_task_sends_duration_as_query(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        seen["body"] = request.content
        return httpx.Response(200, json={"status": "ok"})

    TasksApi(client_factory(handler)).run_test_task(5)

    assert seen["path"] == "/api/tasks/test"
    assert seen["query"] == "duration=5"
    assert seen["body"] in (b"", None)


def test_list_tasks_drops_empty_filters(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode()
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "type": "training",
                        "name": "train",
                        "id": "9f0a5b7e-0000-4000-8000-000000000001",
                        "created_at": "2026-09-15T10:00:00",
                    }
                ],
                "count": 1,
            },
        )

    response = TasksApi(client_factory(handler)).list_tasks(task_type="training", limit=100)

    assert seen["query"] == "task_type=training&limit=100"
    assert response.count == 1
    assert response.tasks[0].type == "training"


def test_get_task_parses_runtimes(client_factory):
    payload = {
        "type": "celery-test",
        "name": "test",
        "id": "9f0a5b7e-0000-4000-8000-000000000001",
        "created_at": "2026-09-15T10:00:00",
        "runtimes": [
            {
                "task_id": "9f0a5b7e-0000-4000-8000-000000000001",
                "status": "running",
                "parameters": {},
                "id": "9f0a5b7e-0000-4000-8000-000000000002",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tasks/9f0a5b7e-0000-4000-8000-000000000001"
        return httpx.Response(200, json=payload)

    task = TasksApi(client_factory(handler)).get_task("9f0a5b7e-0000-4000-8000-000000000001")

    assert task.type == "celery-test"
    assert task.runtimes[0].status == "running"


def test_control_commands_use_post_without_body(client_factory):
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return httpx.Response(200, json={"status": "ok"})

    api = TasksApi(client_factory(handler))
    api.pause_task("task-1")
    api.resume_task("task-1")
    api.interrupt_task("task-1")

    assert [path for path, _ in seen] == [
        "/api/tasks/task-1/pause",
        "/api/tasks/task-1/resume",
        "/api/tasks/task-1/interrupt",
    ]
    assert all(body in (b"", None) for _, body in seen)


def test_get_task_propagates_404(client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Task not found"})

    with pytest.raises(NotFoundError):
        TasksApi(client_factory(handler)).get_task("missing")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 9, 9), "2026-09-09T00:00:00"),
        (datetime(2026, 9, 9, 12, 30, 15), "2026-09-09T12:30:15"),
        ("2026-09-09T01:02:03", "2026-09-09T01:02:03"),
        ("", None),
    ],
)
def test_filters_are_always_date_time(client_factory, value, expected):
    """Стенд требует `format: date-time`: «чистая» дата превращается в начало суток.

    Строка `2026-09-09` в `start_date`/`end_date` отвечает 422 (`datetime_parsing`),
    поэтому клиент не пропускает такой формат наружу (проверено на живом сервере).
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"tasks": [], "count": 0})

    TasksApi(client_factory(handler)).list_tasks(start_date=value, limit=25)

    params = httpx.QueryParams(seen["query"])
    assert params.get("start_date") == expected
    assert ("start_date" in params) is (expected is not None)


def test_period_bounds_are_sent_as_next_day(client_factory):
    """Период суток: `start_date` — начало дня, `end_date` — начало следующих суток."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"tasks": [], "count": 0})

    start, end = day_bounds(date(2026, 9, 9), date(2026, 9, 16))
    TasksApi(client_factory(handler)).list_tasks(start_date=start, end_date=end, limit=25)

    params = httpx.QueryParams(seen["query"])
    assert params.get("start_date") == "2026-09-09T00:00:00"
    assert params.get("end_date") == "2026-09-17T00:00:00"
    assert params.get("limit") == "25"
