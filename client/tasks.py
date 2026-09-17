"""Методы модуля «Task service» (FR-T4, UC-26…UC-28).

Эндпоинты `SOM1.json`:
    POST /api/tasks/test?duration=N      — тестовая задача `celery-test` (диагностика очереди);
    GET  /api/tasks/                     — список задач (фильтры `task_type`, `status`,
                                           `start_date`, `end_date`, `limit` (def 100), `offset`);
    GET  /api/tasks/{task_id}            — задача с `runtimes[]`;
    POST /api/tasks/{task_id}/pause      — приостановка (`running → pausing`);
    POST /api/tasks/{task_id}/interrupt  — прерывание (из `running`/`pausing`/`paused`);
    POST /api/tasks/{task_id}/resume     — продолжение (`paused → running`).

Особенности, проверенные на живом сервисе и важные для испытаний:
    * команды управления передаются **без тела запроса**;
    * для неизвестного `task_id` сервер отдаёт статус `not_found` (BR-R7), а не 404 —
      клиент возвращает модель задачи, а решение о «неизвестной задаче» принимает UI;
    * `POST /api/datasets/fill/{id}` не возвращает `task_id`, поэтому связанная задача
      ищется в списке (монитор задач, этап T4).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from client.http import ApiHttpClient
from client.schemas import TaskListResponse, TaskType, TaskWithRuntimes


def _iso(value: date | datetime | str | None) -> str | None:
    """Приводит дату/время к строке ISO для query-параметров.

    Стенд разбирает фильтры как `datetime` (`format: date-time` в спецификации):
    строка из одной даты (`2026-09-09`) отклоняется статусом 422
    (`datetime_parsing`), поэтому «чистая» дата превращается в начало суток.
    Верхнюю границу клиент не расширяет: `end_date` на сервере исключающая
    (`created_at < end_date`), поэтому конец периода задаёт вызывающий код —
    `lib.period.day_bounds`.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime.combine(value, time.min).isoformat()
    return value


def _drop_empty(params: dict[str, Any]) -> dict[str, Any]:
    """Убирает незаполненные фильтры, чтобы не отправлять пустые query-параметры."""
    return {key: value for key, value in params.items() if value not in (None, "")}


class TasksApi:
    """Доступ к эндпоинтам сервиса задач."""

    def __init__(self, client: ApiHttpClient) -> None:
        self._client = client

    # -- запуск диагностической задачи ---------------------------------------
    def run_test_task(self, duration: int) -> Any:
        """POST /api/tasks/test — тестовая задача Celery длительностью `duration` секунд (UC-26).

        Дешёвый способ проверить очередь, FSM-1 и команды управления без
        расхода ресурсов на обучение/инференс.
        """
        return self._client.post("/api/tasks/test", params={"duration": int(duration)})

    # -- чтение --------------------------------------------------------------
    def list_tasks(
        self,
        *,
        task_type: str | TaskType | None = None,
        status: str | None = None,
        start_date: date | datetime | str | None = None,
        end_date: date | datetime | str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> TaskListResponse:
        """GET /api/tasks/ — список задач с фильтрами и пагинацией (UC-27).

        Границы периода сервер принимает только как дату-время (одна дата → 422),
        `start_date` — включающая, `end_date` — исключающая: конец периода
        считается началом следующих суток (`lib.period.day_bounds`).
        """
        params: dict[str, Any] = {
            "task_type": str(task_type) if task_type is not None else None,
            "status": status,
            "start_date": _iso(start_date),
            "end_date": _iso(end_date),
            "limit": limit,
            "offset": offset,
        }
        payload = self._client.get("/api/tasks/", params=_drop_empty(params))
        if payload is None:
            return TaskListResponse(tasks=[], count=0)
        return TaskListResponse.model_validate(payload)

    def get_task(self, task_id: UUID | str) -> TaskWithRuntimes:
        """GET /api/tasks/{task_id} — задача с прогонами `runtimes[]` (UC-27)."""
        payload = self._client.get(f"/api/tasks/{task_id}")
        return TaskWithRuntimes.model_validate(payload)

    # -- управление выполнением (FSM-1, UC-28) -------------------------------
    def pause_task(self, task_id: UUID | str) -> Any:
        """POST /api/tasks/{task_id}/pause — приостановка выполняющейся задачи."""
        return self._client.post(f"/api/tasks/{task_id}/pause")

    def interrupt_task(self, task_id: UUID | str) -> Any:
        """POST /api/tasks/{task_id}/interrupt — прерывание задачи."""
        return self._client.post(f"/api/tasks/{task_id}/interrupt")

    def resume_task(self, task_id: UUID | str) -> Any:
        """POST /api/tasks/{task_id}/resume — продолжение приостановленной задачи."""
        return self._client.post(f"/api/tasks/{task_id}/resume")
