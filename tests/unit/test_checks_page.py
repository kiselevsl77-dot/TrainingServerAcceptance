"""Тесты помощников экрана «Чек-лист проверок» (пикер задачи, К2 от 18.09.2026).

Проверки задач требуют `task_id`, а история переходов FSM-1 собирается монитором
поллингом. Поэтому экран:

    * предлагает задачу из наблюдений сессии, а если их нет — из живого списка
      задач сервера (иначе `TC-TASK-03/04/05/06` остаются «пропущены» при живом
      реестре задач — находка прогона 18.09.2026);
    * перед запуском сценария ставит выбранную задачу на наблюдение (BR-R5):
      без наблюдения цепочка статусов пуста, и проверкам FSM-1 нечего оценивать.

Виджеты Streamlit и кэш списка задач подменяются, поэтому тесты не зависят от
интерфейса и не обращаются к стенду.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from acceptance.checks import catalog
from acceptance.checks.registry import CheckSpec
from acceptance.session import TestSession as PultSession
from acceptance.tasks_monitor import TaskMonitor
from acceptance.ui.pages import checks

TASK = "9f0a5b7e-0000-4000-8000-000000000001"
OTHER = "9f0a5b7e-0000-4000-8000-000000000002"


def _spec(check_id: str) -> CheckSpec:
    """Описание проверки каталога (без Optional в аннотациях)."""
    found = catalog.find(check_id)
    assert found is not None, check_id
    return found


def _session() -> PultSession:
    """Сессия-пустышка: экран и монитор передают её дальше, не читая полей."""
    return cast(PultSession, object())


def _monitor(observed: tuple[str, ...] = ()) -> tuple[_FakeMonitor, TaskMonitor]:
    """Подменённый монитор задач: сам объект для проверок и его типизированное лицо."""
    fake = _FakeMonitor(observed)
    return fake, cast(TaskMonitor, fake)


class _FakeMonitor:
    """Минимальный монитор: наблюдения, регистрация задачи и один опрос."""

    def __init__(self, observed: tuple[str, ...] = ()) -> None:
        self._observed = [
            SimpleNamespace(task_id=task_id, task_type="celery-test", status_label="Выполняется")
            for task_id in observed
        ]
        self.registered_task: tuple[Any, str] | None = None
        self.registered_external: tuple[str, str] | None = None
        self.polled: tuple[str, str] | None = None

    def observed(self, session: Any, *, active_only: bool = False) -> list[Any]:
        """Строки наблюдения сессии (в тесте — заранее заданные)."""
        return list(self._observed)

    def register_task(self, session: Any, task: Any, *, check_id: str = "") -> dict[str, Any]:
        """Регистрирует задачу из серверного списка."""
        self.registered_task = (task, check_id)
        return {}

    def register_external(
        self, session: Any, task_id: str, *, check_id: str = ""
    ) -> dict[str, Any]:
        """Регистрирует внешнюю задачу (BR-R5)."""
        self.registered_external = (task_id, check_id)
        return {}

    def poll_once(self, session: Any, task_id: str, *, label: str = "") -> None:
        """Фиксирует факт опроса статуса задачи."""
        self.polled = (task_id, label)


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Перехватывает запись сессии (`state.store_session`)."""
    saved: list[Any] = []
    monkeypatch.setattr(checks.state, "store_session", saved.append)
    return saved


def _server_tasks(
    monkeypatch: pytest.MonkeyPatch, *task_ids: str, error: str | None = None
) -> None:
    """Подменяет живой список задач сервера (`state.load_tasks`)."""
    tasks = [
        SimpleNamespace(id=task_id, type="celery-test", name="test_task") for task_id in task_ids
    ]
    monkeypatch.setattr(checks.state, "load_tasks", lambda **kwargs: (tasks, len(tasks), error))


# ---------------------------------------------------------------------------
# Список задач сервера (подстановка `task_id`)
# ---------------------------------------------------------------------------
def test_server_task_options_lists_live_tasks(monkeypatch: pytest.MonkeyPatch):
    """Пикер строит варианты выбора из живого списка задач сервера."""
    _server_tasks(monkeypatch, TASK)

    options = checks._server_task_options()

    assert list(options.values()) == [TASK]
    label = next(iter(options))
    assert "celery-test" in label and "test_task" in label


def test_server_task_options_reports_unavailable_list(monkeypatch: pytest.MonkeyPatch):
    """Недоступный список задач не мешает выбрать задачу на экране «Задачи»."""
    captions: list[str] = []
    monkeypatch.setattr(checks.state, "load_tasks", lambda **kwargs: ([], 0, "сервер недоступен"))
    monkeypatch.setattr(
        checks.st, "caption", lambda text, *args, **kwargs: captions.append(str(text))
    )

    assert checks._server_task_options() == {}
    assert any("Список задач недоступен" in text for text in captions)


# ---------------------------------------------------------------------------
# Пикер задачи в карточке проверки
# ---------------------------------------------------------------------------
def test_task_picker_falls_back_to_server_list(monkeypatch: pytest.MonkeyPatch):
    """Без наблюдений пикер предлагает задачи сервера и возвращает выбранный id."""
    _server_tasks(monkeypatch, TASK)
    captions: list[str] = []
    chosen: dict[str, Any] = {}

    def fake_selectbox(label: str, options: list[str], **kwargs: Any) -> str:
        chosen["label"] = label
        chosen["options"] = list(options)
        return options[1]

    monkeypatch.setattr(
        checks.st, "caption", lambda text, *args, **kwargs: captions.append(str(text))
    )
    monkeypatch.setattr(checks.st, "selectbox", fake_selectbox)
    _fake, monitor = _monitor()

    task_id = checks._render_task_picker(_session(), monitor, _spec("TC-TASK-03"))

    assert task_id == TASK
    assert chosen["label"] == "Задача для сценария проверки"
    assert len(chosen["options"]) == 2 and chosen["options"][0] == ""
    assert any("список задач сервера" in text for text in captions)


def test_task_picker_prefers_observed_tasks(monkeypatch: pytest.MonkeyPatch):
    """При наличии наблюдений серверный список не запрашивается."""
    monkeypatch.setattr(
        checks.state,
        "load_tasks",
        lambda **kwargs: pytest.fail("наблюдения есть — список сервера не нужен"),
    )
    values: dict[str, Any] = {}

    def fake_selectbox(label: str, options: list[str], **kwargs: Any) -> str:
        values["options"] = list(options)
        return options[1]

    monkeypatch.setattr(checks.st, "selectbox", fake_selectbox)
    _fake, monitor = _monitor((TASK,))

    task_id = checks._render_task_picker(_session(), monitor, _spec("TC-TASK-04"))

    assert task_id == TASK
    assert any(TASK in str(value) for value in values["options"])


def test_task_picker_without_tasks_explains_what_to_do(monkeypatch: pytest.MonkeyPatch):
    """Если задач нет ни в сессии, ни на сервере — подсказка оператору."""
    infos: list[str] = []
    _server_tasks(monkeypatch)
    monkeypatch.setattr(
        checks.st, "selectbox", lambda *args, **kwargs: pytest.fail("выбор не нужен")
    )
    monkeypatch.setattr(checks.st, "info", lambda text, *args, **kwargs: infos.append(str(text)))
    _fake, monitor = _monitor()

    assert checks._render_task_picker(_session(), monitor, _spec("TC-TASK-04")) == ""
    assert any("Наблюдаемых задач нет" in text for text in infos)

    infos.clear()
    assert checks._render_task_picker(_session(), monitor, _spec("TC-TASK-02")) == ""
    assert infos == [], "TC-TASK-02 задачу не использует — подсказка не нужна"


# ---------------------------------------------------------------------------
# Постановка выбранной задачи на наблюдение (BR-R5)
# ---------------------------------------------------------------------------
def test_ensure_task_observed_registers_server_task(
    monkeypatch: pytest.MonkeyPatch, stored: list[Any]
):
    """Задача из списка сервера ставится на наблюдение и опрашивается один раз."""
    _server_tasks(monkeypatch, TASK)
    fake, monitor = _monitor()
    session = _session()

    checks._ensure_task_observed(session, monitor, _spec("TC-TASK-04"), TASK)

    assert fake.registered_task is not None
    task, check_id = fake.registered_task
    assert str(task.id) == TASK
    assert check_id == "TC-TASK-04"
    assert fake.registered_external is None
    assert fake.polled == (TASK, "TC-TASK-04")
    assert stored == [session]


def test_ensure_task_observed_skips_observed_task(
    monkeypatch: pytest.MonkeyPatch, stored: list[Any]
):
    """Уже наблюдаемая задача повторно не регистрируется."""
    _server_tasks(monkeypatch, TASK)
    _fake, monitor = _monitor((TASK,))

    checks._ensure_task_observed(_session(), monitor, _spec("TC-TASK-04"), TASK)

    assert stored == []


def test_ensure_task_observed_marks_unknown_task_external(
    monkeypatch: pytest.MonkeyPatch, stored: list[Any]
):
    """Задачи нет в серверном списке — она регистрируется как внешняя (BR-R5)."""
    _server_tasks(monkeypatch)
    fake, monitor = _monitor()

    checks._ensure_task_observed(_session(), monitor, _spec("TC-TASK-05"), OTHER)

    assert fake.registered_external == (OTHER, "TC-TASK-05")
    assert fake.registered_task is None


def test_ensure_task_observed_marks_external_check_external(
    monkeypatch: pytest.MonkeyPatch, stored: list[Any]
):
    """Проверка TC-TASK-08 всегда работает с внешней задачей (BR-R5)."""
    _server_tasks(monkeypatch, TASK)
    fake, monitor = _monitor()

    checks._ensure_task_observed(_session(), monitor, _spec("TC-TASK-08"), TASK)

    assert fake.registered_external == (TASK, "TC-TASK-08")
    assert fake.registered_task is None


def test_ensure_task_observed_without_task_does_nothing(
    monkeypatch: pytest.MonkeyPatch, stored: list[Any]
):
    """Без выбранной задачи и без монитора ничего не происходит."""
    _server_tasks(monkeypatch, TASK)
    fake, monitor = _monitor()

    checks._ensure_task_observed(_session(), monitor, _spec("TC-TASK-04"), "")
    checks._ensure_task_observed(_session(), None, _spec("TC-TASK-04"), TASK)

    assert fake.polled is None and stored == []
