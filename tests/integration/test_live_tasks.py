"""Интеграционные проверки задач на живом стенде (этап T4, FSM-1).

Запуск:

    $env:TRAINING_SERVER_BASE_URL = "https://energomera.ai-center.online"
    python -m pytest -m integration

Проверяется то, что нельзя проверить на заглушке: реальные статусы задач, реальные
переходы FSM-1 у задачи `celery-test`, поведение на неизвестный `task_id` (BR-R7),
маркировка обменов монитора в журнале пульта и требование стенда к формату фильтров
периода (`start_date`/`end_date` — только дата-время, «чистая» дата → 422).

Сценарий создания задачи запускается **только** при явном разрешении
(`PULT_LIVE_WRITE=1`): по умолчанию проверяются чтение и наблюдение уже
существующих задач — испытания не должны менять стенд незаметно для оператора.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pytest

from acceptance.api import Apis, build_client
from acceptance.http_log import Journal
from acceptance.session import new_session
from acceptance.tasks_monitor import TaskMonitor, current_status, status_label
from client.errors import ApiError, NotFoundError
from client.schemas import TaskStatus, TaskType
from client.settings import load_settings
from lib.period import day_bounds
from lib.polling import poll_until

pytestmark = pytest.mark.integration

#: Разрешение на создание диагностической задачи на живом стенде.
WRITE_ENV = "PULT_LIVE_WRITE"
DIAG_DURATION = 8


def _settings_or_skip():
    """Настройки подключения или пропуск теста (если base URL не задан)."""
    settings = load_settings()
    if not settings.is_configured:
        pytest.skip("TRAINING_SERVER_BASE_URL не задан")
    return settings


@pytest.fixture
def live_journal() -> Journal:
    """Журнал обмена для интеграционной проверки."""
    return Journal(max_records=200)


# ---------------------------------------------------------------------------
# Чтение: список, карточка, фильтры (TC-TASK-02, TC-TASK-03)
# ---------------------------------------------------------------------------
def test_live_task_list_and_card(live_journal: Journal):
    """Список задач читается, карточка задачи приходит с прогонами."""
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    try:
        tasks_api = Apis.build(client).tasks
        page = tasks_api.list_tasks(limit=5)
        assert page.count >= 0
        if not page.tasks:
            pytest.skip("на стенде нет задач — сценарий карточки пропускается")
        task_id = str(page.tasks[0].id)
        card = tasks_api.get_task(task_id)
    finally:
        client.close()

    assert card.runtimes, "карточка задачи пришла без прогонов"
    assert current_status(card) in {str(status) for status in TaskStatus}
    assert live_journal.summary()["errors"] == 0
    assert [record.path for record in live_journal.records][:2] == [
        "/api/tasks/",
        f"/api/tasks/{task_id}",
    ]


def test_live_task_filters_do_not_break_the_list(live_journal: Journal):
    """Фильтры `task_type`/`status` отвечают, не ломая выдачу (TC-TASK-02)."""
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    try:
        tasks_api = Apis.build(client).tasks
        base = tasks_api.list_tasks(limit=1)
        filtered = tasks_api.list_tasks(task_type=str(TaskType.CELERY_TEST), limit=5)
        completed = tasks_api.list_tasks(status=str(TaskStatus.COMPLETED), limit=5)
    finally:
        client.close()

    if base.count:
        assert filtered.count <= base.count
    assert all(str(task.type) == str(TaskType.CELERY_TEST) for task in filtered.tasks)
    assert completed.count >= 0


def test_live_task_list_has_no_status_but_card_has(live_journal: Journal):
    """Статуса в элементе списка нет (только в карточке) — основание для P1.

    Проверяется по сырому телу ответа (`response_body` журнала): клиентская модель
    `CeleryTask` лишние поля не сохраняет, поэтому факт «сервер не отдаёт статус в
    списке» подтверждается именно телом ответа. Следствие для пульта: статус строки
    берётся из наблюдения, а полный список со статусами — это N запросов карточек.
    """
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    try:
        tasks_api = Apis.build(client).tasks
        page = tasks_api.list_tasks(limit=5)
        if not page.tasks:
            pytest.skip("на стенде нет задач — пробу состава списка выполнить нечем")
        card = tasks_api.get_task(str(page.tasks[0].id))
    finally:
        client.close()

    list_body = json.loads(live_journal.records[0].response_body or "{}")
    item = (list_body.get("tasks") or [{}])[0]
    assert "status" not in item, "в элементе списка появился статус — N+1 больше не нужен"
    assert set(item) == {"type", "name", "description", "id", "created_at"}
    assert card.runtimes and str(card.runtimes[-1].status)
    assert current_status(card)


def test_live_period_filters_require_date_time(live_journal: Journal):
    """Фильтры периода: дата-время принимается, «чистая» дата — 422 (TC-TASK-02).

    Найдено 16.09.2026: спецификация объявляет `start_date`/`end_date` как
    `format: date-time`, и стенд требует время в значении. Проба носит
    справочный характер: она объясняет, почему пульт считает границы периода
    в `lib.period.day_bounds`, и попадает в протокол испытаний.
    """
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    start, end = day_bounds(date.today() - timedelta(days=7), date.today())
    try:
        tasks_api = Apis.build(client).tasks
        page = tasks_api.list_tasks(start_date=start, end_date=end, limit=5)
        with pytest.raises(ApiError) as error:
            tasks_api.list_tasks(start_date=date.today().isoformat(), limit=5)
    finally:
        client.close()

    assert page.count >= 0
    assert error.value.status_code == 422
    assert live_journal.records[-1].status == 422
    assert live_journal.records[-2].status == 200


def test_live_unknown_task_is_reported_as_result(live_journal: Journal):
    """Неизвестный `task_id`: `not_found` в карточке или 404 — оба варианта BR-R7."""
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    probe = "00000000-0000-4000-8000-000000000000"
    try:
        tasks_api = Apis.build(client).tasks
        try:
            card = tasks_api.get_task(probe)
        except NotFoundError:
            variant = "404"
        else:
            variant = current_status(card)
    finally:
        client.close()

    assert variant in ("404", str(TaskStatus.NOT_FOUND)), f"неожиданный ответ: {variant}"


# ---------------------------------------------------------------------------
# Наблюдение и переходы FSM-1 (TC-TASK-04, TC-TASK-05, TC-TASK-06)
# ---------------------------------------------------------------------------
def test_live_task_list_is_uncapped_and_unsorted(live_journal: Journal):
    """Список читается целиком, а порядок ответа не гарантирован (TC-TASK-02).

    На этом свойстве держится экран «Задачи»: пульт читает список одним запросом
    (`acceptance.ui.state.LOAD_TASK_CAP`) и сортирует его сам, поэтому порядок
    сервера и клиентская пагинация не важны. Проба фиксирует оба факта в журнале:
    большой `limit` возвращает весь список, а `created_at` идёт не по возрастанию
    и не по убыванию.
    """
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    try:
        tasks_api = Apis.build(client).tasks
        base = tasks_api.list_tasks()
        big = tasks_api.list_tasks(limit=1000)
    finally:
        client.close()

    assert big.count == base.count
    assert len(big.tasks) == big.count, "список не читается целиком одним запросом"
    created = [task.created_at for task in big.tasks]
    if len(created) > 2:
        assert created != sorted(created), "порядок ответа неожиданно возрастающий"
        assert created != sorted(created, reverse=True), "порядок ответа неожиданно убывающий"


def test_live_archive_is_a_pult_side_notion(live_journal: Journal):
    """Архива задач в API нет: снимок статусов и архив ведёт пульт (TC-TASK-02).

    Проверяется по сырому телу ответа: в элементе списка нет ни признака архива, ни
    времени завершения задачи, поэтому правило «завершена ≥ суток назад → архив»
    вычисляется пультом (`acceptance.task_snapshot`) и хранится вне файла сессии.
    """
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    try:
        tasks_api = Apis.build(client).tasks
        page = tasks_api.list_tasks(limit=5)
        if not page.tasks:
            pytest.skip("на стенде нет задач — пробу архива выполнить нечем")
        card = tasks_api.get_task(str(page.tasks[0].id))
    finally:
        client.close()

    item = (json.loads(live_journal.records[0].response_body or "{}").get("tasks") or [{}])[0]
    assert not [name for name in item if "archiv" in name], "в элементе списка появился архив"
    assert not {"archived", "finished_at", "end_time"} & set(item)
    runtime = card.runtimes[-1]
    # время завершения есть только в прогоне карточки: по нему пульт и считает архив
    assert hasattr(runtime, "end_time")


def test_live_observation_marks_journal_with_check_label(live_journal: Journal):
    """Опрос задачи помечается меткой проверки, история наблюдения заполняется."""
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    try:
        tasks_api = Apis.build(client).tasks
        monitor = TaskMonitor(tasks_api, journal=live_journal, interval=0.5)
        page = tasks_api.list_tasks(limit=1)
        if not page.tasks:
            pytest.skip("на стенде нет задач — наблюдение проверить нечем")

        task_id = str(page.tasks[0].id)
        session = new_session(base_url=settings.base_url)
        monitor.register(
            session,
            task_id=task_id,
            task_type=str(page.tasks[0].type),
            check_id="TC-TASK-04",
        )
        poll = monitor.poll_once(session, task_id)
    finally:
        client.close()

    assert poll.ok, poll.error
    assert poll.journal_seq is not None
    assert live_journal.for_label("TC-TASK-04"), "обмены монитора не помечены меткой проверки"
    assert session.tasks[0]["history"], "история переходов наблюдения пуста"
    assert status_label(poll.status)


@pytest.mark.skipif(
    os.getenv(WRITE_ENV) != "1",
    reason=f"изменяющий сценарий: разрешается переменной {WRITE_ENV}=1",
)
def test_live_celery_test_task_fsm_1(live_journal: Journal):
    """Живая задача `celery-test`: создание, `pause`/`resume`, `interrupt` (UC-26…UC-28).

    Проверка выполняется только с `PULT_LIVE_WRITE=1`: она создаёт задачу на общем
    стенде и меняет её состояние. Все обмены остаются в журнале пульта.
    """
    settings = _settings_or_skip()
    client = build_client(settings, live_journal)
    try:
        tasks_api = Apis.build(client).tasks
        monitor = TaskMonitor(tasks_api, journal=live_journal, interval=0.5)
        payload = tasks_api.run_test_task(DIAG_DURATION)
        task_id = str(payload.get("task_id") or "")
        assert task_id, f"сервер не вернул task_id: {payload}"

        session = new_session(base_url=settings.base_url)
        monitor.register(session, task_id=task_id, task_type=str(TaskType.CELERY_TEST))

        # ждём статус `running`: сценарий требует задачи, у которой есть время на команды
        running = poll_until(
            lambda: monitor.poll_once(session, task_id),
            lambda poll: poll.status == "running" or not poll.ok,
            interval=1.0,
            timeout=float(DIAG_DURATION),
        )
        assert running.ok and running.status == "running", running.error or running.status

        paused = monitor.run_command(session, task_id, "pause")
        assert paused.allowed, paused.note
        resumed = monitor.run_command(session, task_id, "resume")
        assert resumed.allowed, resumed.note
        stopped = monitor.run_command(session, task_id, "interrupt")
        assert stopped.allowed, stopped.note

        chain = [item["status"] for item in session.tasks[0]["history"]]
    finally:
        client.close()

    assert any(status in chain for status in ("running", "paused", "interrupted")), chain
    assert live_journal.for_label("TC-TASK-04") or live_journal.for_label("TC-TASK-05")
    assert live_journal.summary()["errors"] == 0
