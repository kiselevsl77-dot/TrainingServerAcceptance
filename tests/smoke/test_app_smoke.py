"""Смоук-тесты экранов пульта (Streamlit AppTest, без сети).

Клиент подменяется транспортом `httpx.MockTransport`, а журналирование
направляется во временные файлы, поэтому тесты не зависят от живого стенда
и не загрязняют `acceptance_data/`.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from acceptance import endpoints as ep
from acceptance import overrides as overrides_api
from acceptance import task_snapshot as task_snapshot_api
from acceptance.api import Apis
from acceptance.config import PultConfig
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.logging_setup import setup_logging
from acceptance.overrides import OVERRIDES_FILENAME, Overrides
from acceptance.records import record_id_for
from acceptance.session import new_session
from acceptance.task_snapshot import SNAPSHOT_FILENAME, TaskSnapshot
from acceptance.ui import state
from acceptance.ui.state import Runtime
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

APP_PATH = Path(__file__).resolve().parents[2] / "acceptance" / "app.py"

FILES = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "file_name": "Antminer_S19.raw.csv",
        "size": 1000,
        "s3_path": "RAW/2026/09/09/Antminer_S19.raw.csv",
        "import_date": "2026-09-09T13:53:56",
        "file_type": "RAW",
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "file_name": "Antminer_S19.markup.csv",
        "size": 100,
        "s3_path": "LOADS/2026/09/09/Antminer_S19.markup.csv",
        "import_date": "2026-09-09T13:53:57",
        "file_type": "LOADS",
    },
    {
        "id": "33333333-3333-4333-8333-333333333333",
        "file_name": "Antminer_S19.raw.csv",
        "size": 1500,
        "s3_path": "RAW/2026/09/09/1500/Antminer_S19.raw.csv",
        "import_date": "2026-09-09T14:10:00",
        "file_type": "RAW",
    },
    {
        "id": "44444444-4444-4444-8444-444444444444",
        "file_name": "Loose_Unit.markup.csv",
        "size": 200,
        "s3_path": "LOADS/2026/09/09/Loose_Unit.markup.csv",
        "import_date": "2026-09-09T14:20:00",
        "file_type": "LOADS",
    },
    {
        "id": "55555555-5555-4555-8555-555555555555",
        "file_name": "model_1.h5",
        "size": 5000,
        "s3_path": "MODELS/2026/09/09/model_1.h5",
        "import_date": "2026-09-09T14:30:00",
        "file_type": "H5",
    },
]


TASKS_SCREEN_ID = "9f0a5b7e-0000-4000-8000-000000000001"
TASK_RUN_ID = "5f0a5b7e-0000-4000-8000-00000000000a"

#: Состояние задачи на подменённом сервере (команды FSM-1 его меняют).
TASK_STATE: dict[str, str] = {"status": "running"}

#: Доступность карточки задачи на «сервере» (проверка честного «нет данных»).
CARD_STATE: dict[str, bool] = {"available": True}


def _task_card(task_id: str, status: str) -> dict:
    """Карточка задачи (`TaskWithRuntimes`) для смоук-тестов."""
    runtime = {
        "task_id": task_id,
        "status": status,
        "parameters": {"duration": 2},
        "id": TASK_RUN_ID,
    }
    if status != "new":
        runtime["start_time"] = "2026-09-16T10:00:01"
        runtime["intermediate_result"] = {"progress": 40}
    return {
        "type": "celery-test",
        "name": "__TEST__probe",
        "id": task_id,
        "created_at": "2026-09-16T10:00:00",
        "runtimes": [runtime],
    }


def _tasks_list_response(request: httpx.Request) -> httpx.Response:
    """`GET /api/tasks/` на заглушке — как живой стенд: фильтры только дата-время.

    Стенд разбирает `start_date`/`end_date` как `datetime` (`format: date-time`)
    и отвечает 422 (`datetime_parsing`) на строку из одной даты. Заглушка ведёт
    себя так же, иначе регрессия формата фильтров проходит смоук-тесты незамеченной.
    """
    for name in ("start_date", "end_date"):
        value = request.url.params.get(name)
        if value and "T" not in value:
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
    return httpx.Response(
        200,
        json={
            "tasks": [
                {
                    "type": "celery-test",
                    "name": "__TEST__probe",
                    "id": TASKS_SCREEN_ID,
                    "created_at": "2026-09-16T10:00:00",
                }
            ],
            "count": 1,
        },
    )


def _handler(request: httpx.Request) -> httpx.Response:
    """Подменённый сервер для смоук-тестов экранов."""
    path = request.url.path
    if path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if path == "/version":
        return httpx.Response(200, json={"branch": "dev", "revision": "58ac72f1234567"})
    if path == "/api/data/files":
        return httpx.Response(200, json={"files": FILES, "count": len(FILES)})
    if path == "/api/loads/list":
        return httpx.Response(200, json={"loads": [], "result_size": 0, "limit": 1, "offset": 0})
    if path == "/api/ml_models/models":
        return httpx.Response(200, json={"models": [], "count": 0})
    if path == "/api/datasets/" and request.method == "POST":
        return httpx.Response(201, json={"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"})
    if path == "/api/datasets/":
        return httpx.Response(200, json={"datasets": [], "count": 0})
    if path == "/api/tasks/test":
        TASK_STATE["status"] = "running"
        return httpx.Response(200, json={"task_id": TASKS_SCREEN_ID, "status": "new"})
    if path == "/api/tasks/":
        return _tasks_list_response(request)
    if path.endswith("/pause"):
        TASK_STATE["status"] = "paused"
        return httpx.Response(200, json={})
    if path.endswith("/resume"):
        TASK_STATE["status"] = "running"
        return httpx.Response(200, json={})
    if path.endswith("/interrupt"):
        TASK_STATE["status"] = "interrupted"
        return httpx.Response(200, json={})
    if path == f"/api/tasks/{TASKS_SCREEN_ID}":
        if not CARD_STATE["available"]:
            return httpx.Response(500, json={"detail": "internal error"})
        return httpx.Response(200, json=_task_card(TASKS_SCREEN_ID, TASK_STATE["status"]))
    if path.startswith("/api/tasks/"):
        return httpx.Response(404, json={"detail": "Task not found"})
    if path.startswith("/api/data/file/") and path.endswith("/download"):
        return httpx.Response(
            200,
            content=b"time,u\n0,1\n",
            headers={
                "content-type": "application/octet-stream",
                "content-disposition": "attachment; filename=Antminer_S19.markup.csv",
            },
        )
    return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def pult(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """AppTest пульта с подменённым runtime и журналированием во временные файлы."""
    if str(APP_PATH.parents[1]) not in sys.path:
        sys.path.insert(0, str(APP_PATH.parents[1]))

    journal = Journal(max_records=200)
    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(_handler), body_limit=500),
    )
    settings = TrainingServerSettings(base_url="http://test.local", timeout=5.0)
    runtime = Runtime(
        config=PultConfig(body_limit=500, log_bodies=True, journal_max=200),
        settings=settings,
        journal=journal,
        client=client,
        apis=Apis.build(client),
    )
    artifacts = setup_logging(
        level="DEBUG",
        session_id="smoke",
        app_log=tmp_path / "app.log",
        session_log=tmp_path / "session.jsonl",
        console=False,
        enqueue=False,
    )

    monkeypatch.setattr(state, "get_runtime", lambda: runtime)
    monkeypatch.setattr(state, "ensure_logging", lambda level, session_id=None: artifacts)
    # клиент консоли запросов: тот же журнал, но «сырой» httpx (нужны статус и заголовки)
    console = build_console_client(
        settings,
        journal,
        config=PultConfig(body_limit=500, log_bodies=True, journal_max=200),
        inner=httpx.MockTransport(_handler),
    )
    monkeypatch.setattr(state, "_console_client", lambda *args, **kwargs: console)
    # ручные решения оператора: чтение — пусто, запись — во временный файл (герметичность теста)
    monkeypatch.setattr(overrides_api, "load_overrides", lambda directory=None: Overrides())
    monkeypatch.setattr(
        overrides_api,
        "save_overrides",
        lambda overrides, directory=None: tmp_path / OVERRIDES_FILENAME,
    )
    # снимок статусов и архив задач: живут в памяти теста, а не в `acceptance_data` (T4)
    snapshots: dict[str, TaskSnapshot] = {}
    monkeypatch.setattr(
        task_snapshot_api,
        "load_snapshot",
        lambda directory=None: snapshots.get("current") or TaskSnapshot(),
    )

    def _save_snapshot(snapshot: TaskSnapshot, directory=None) -> Path:
        """Сохраняет снимок в памяти теста (файл на диске не создаётся)."""
        snapshots["current"] = snapshot
        return tmp_path / SNAPSHOT_FILENAME

    monkeypatch.setattr(task_snapshot_api, "save_snapshot", _save_snapshot)
    # кэш реестра файлов общий для процесса: сбрасываем, чтобы тесты не влияли друг на друга
    state.load_files.clear()
    # кэш списка задач, карточек и состояние задачи на «сервере» тоже общие (T4)
    state.load_tasks.clear()
    state.clear_task_cards()
    TASK_STATE["status"] = "running"
    CARD_STATE["available"] = True

    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.session_state["pult_screen"] = "stand"
    app.run()
    return app, journal


def _open(app: AppTest, screen: str) -> AppTest:
    """Открывает экран и возвращает результат прогона."""
    app.session_state["pult_screen"] = screen
    return app.run()


class _MemorySessions:
    """Сессии испытаний в памяти: смоук-тесты не пишут в `acceptance_data` (FR-T5)."""

    def __init__(self, session) -> None:
        self.session = session

    def current(self):
        """Текущая сессия (всегда одна — смоук-сценарий)."""
        return self.session

    def store(self, session):
        """Сохраняет сессию в памяти вместо диска."""
        self.session = session
        return session


@pytest.fixture
def pult_with_session(pult, monkeypatch: pytest.MonkeyPatch):
    """Пульт с выбранной сессией в памяти (экраны «Замечания к API» и «Консоль»)."""
    app, journal = pult
    store = _MemorySessions(new_session(base_url="http://test.local"))
    store.session.info.title = "Смоук-испытания"
    store.session.info.operator_fio = "Иванов И.И."

    monkeypatch.setattr(state, "current_session", store.current)
    monkeypatch.setattr(state, "store_session", store.store)
    app.run()
    return app, journal, store


def test_stand_screen_renders_server_state(pult):
    app, _ = pult
    labels = [metric.label for metric in app.metric]

    assert not app.exception
    assert app.title[0].value == "Стенд"
    assert "Запросов" in labels
    assert any("Сервер доступен" in box.value for box in app.success)


def test_stand_screen_can_take_snapshot(pult):
    app, journal = pult

    snapshot_button = next(button for button in app.button if "Снять снимок" in button.label)
    snapshot_button.click().run()

    assert not app.exception
    labels = [metric.label for metric in app.metric]
    assert "Моделей" in labels
    assert any(record.path == "/api/data/files" for record in journal.records)


def test_session_screen_shows_new_session_form(pult):
    app, _ = pult
    app = _open(app, "session")

    assert not app.exception
    assert app.title[0].value == "Сессия испытаний"
    assert any("Наименование испытаний" in widget.label for widget in app.text_input)


def test_logs_screen_lists_journal_records(pult):
    app, _ = pult
    app = _open(app, "logs")

    assert not app.exception
    assert app.title[0].value == "Журнал"
    assert any("Только ошибки" in checkbox.label for checkbox in app.checkbox)


def test_planned_screens_render_as_stubs(pult):
    app, _ = pult

    for screen in ("report",):
        app = _open(app, screen)
        assert not app.exception, f"экран {screen} упал"
        assert app.info, f"экран {screen} не показал заглушку"


# ---------------------------------------------------------------------------
# Экран «Задачи» — монитор задач (FR-8, FR-T11) — этап T4
# ---------------------------------------------------------------------------
def test_tasks_screen_renders_kpi_list_and_observation(pult_with_session):
    """Экран «Задачи»: KPI монитора, список сервера со статусами и вкладка наблюдения."""
    app, journal, _store = pult_with_session
    app = _open(app, "tasks")

    assert not app.exception
    assert app.title[0].value == "Задачи"
    values = _metric_values(app)
    assert values["Наблюдаемых"] == "0"
    assert values["Активных"] == "0"
    assert values["Внешних"] == "0"

    frames = app.dataframe
    assert frames, "экран не показал список задач сервера"
    rows = frames[0].value.to_dict("records")
    assert [row["Задача"] for row in rows] == [f"…{TASKS_SCREEN_ID[-8:]}"]
    assert rows[0]["Тип"] == "celery-test"
    assert list(rows[0]) == [
        "Задача",
        "Тип",
        "Статус",
        "Источник",
        "Наблюдение",
        "Проверка",
        "Название",
        "Описание",
        "Создана",
        "Начало",
        "Окончание",
        "Архив",
    ]
    # статус подтянут карточкой (в списке статуса нет) — и это видно по источнику
    assert rows[0]["Статус"] == "▶ Выполняется"
    assert rows[0]["Источник"] == "карточка"
    assert rows[0]["Наблюдение"] == "—"
    assert rows[0]["Проверка"] == "—"
    assert rows[0]["Архив"] == "—"
    assert rows[0]["Создана"] == "2026-09-16T10:00:00"
    # имя задачи длиннее 21 символа — в таблице сокращено, полное значение в панели строки
    assert rows[0]["Название"] == "__TEST__probe"
    labeled = [
        record
        for record in journal.records
        if record.path == f"/api/tasks/{TASKS_SCREEN_ID}" and record.label == "TC-TASK-02"
    ]
    assert labeled, "статус подтянут без метки проверки TC-TASK-02"
    assert all(
        record.path in ("/api/tasks/", f"/api/tasks/{TASKS_SCREEN_ID}")
        for record in journal.records
        if record.path.startswith("/api/tasks")
    ), "экран «Задачи» запрашивает лишние эндпоинты"


def test_tasks_screen_shortens_long_values_and_shows_full_ones(pult_with_session):
    """Длинные id/название/описание сокращаются, полные значения — в панели строки."""
    app, _journal, _store = pult_with_session
    app = _open(app, "tasks")

    rows = app.dataframe[0].value.to_dict("records")
    assert rows[0]["Задача"].startswith("…")
    assert len(rows[0]["Задача"]) == 9

    _widget(app, "checkbox", "tasks_full_values").check().run()

    rows = app.dataframe[0].value.to_dict("records")
    assert rows[0]["Задача"] == TASKS_SCREEN_ID


def test_tasks_screen_status_comes_from_observation(pult_with_session):
    """Наблюдение видно в списке: колонки «Наблюдение» и времена прогона заполнены."""
    app, _journal, _store = pult_with_session
    app = _open(app, "tasks")

    next(button for button in app.button if button.key == "tasks_watch").click().run()
    next(button for button in app.button if button.key == "tasks_poll_all").click().run()

    rows = app.dataframe[0].value.to_dict("records")

    assert rows[0]["Статус"] == "▶ Выполняется"
    assert rows[0]["Наблюдение"] == "⏱️ да"
    assert rows[0]["Проверка"] == "—"  # задача поставлена на наблюдение из списка: метки нет
    assert rows[0]["Начало"] == "2026-09-16T10:00:01"


def test_tasks_screen_marks_unknown_status_without_card(pult_with_session):
    """Карточка недоступна — пульт не выдумывает статус: «нет данных», источник указан."""
    app, _journal, _store = pult_with_session
    CARD_STATE["available"] = False
    app = _open(app, "tasks")

    rows = app.dataframe[0].value.to_dict("records")

    assert rows[0]["Статус"] == "— нет данных"
    assert rows[0]["Источник"] == "нет данных"


def test_tasks_screen_has_single_page_size_selector(pult_with_session):
    """Размер страницы задаётся одним полем: дубль «На странице» убран."""
    app, _journal, _store = pult_with_session
    app = _open(app, "tasks")

    keys = [widget.key for widget in app.selectbox]

    assert "tasks_per_page" in keys
    assert "tasks_page_size" not in keys


def test_tasks_screen_refreshes_statuses_with_check_label(pult_with_session):
    """«Обновить статусы»: карточки запрашиваются заново и помечаются меткой TC-TASK-02."""
    app, journal, _store = pult_with_session
    app = _open(app, "tasks")

    journal.clear()
    _widget(app, "button", "tasks_statuses_refresh").click().run()

    assert not app.exception
    assert any("Кэш карточек сброшен" in box.value for box in app.info)
    labeled = [
        record
        for record in journal.records
        if record.path == f"/api/tasks/{TASKS_SCREEN_ID}" and record.label == "TC-TASK-02"
    ]
    assert labeled, "запросы карточек не помечены меткой проверки TC-TASK-02"
    rows = app.dataframe[0].value.to_dict("records")
    assert rows[0]["Статус"] == "▶ Выполняется"
    assert rows[0]["Источник"] == "карточка"


def test_tasks_screen_moves_completed_tasks_to_archive(pult_with_session):
    """«Перенести завершённые в архив»: подтверждение, перенос и вид «📦 Архив»."""
    app, _journal, _store = pult_with_session
    TASK_STATE["status"] = "completed"
    app = _open(app, "tasks")

    rows = app.dataframe[0].value.to_dict("records")
    assert rows[0]["Статус"] == "✔ Завершена"

    assert _widget(app, "button", "tasks_archive_completed").disabled, "без подтверждения нельзя"
    _widget(app, "checkbox", "tasks_archive_confirm").check().run()
    next(button for button in app.button if button.key == "tasks_archive_completed").click().run()

    assert not app.exception
    assert any("перенесено в архив: 1" in box.value for box in app.success)
    assert any("В этом виде нет задач" in box.value for box in app.info)

    _widget(app, "radio", "tasks_view").set_value("📦 Архив").run()

    assert not app.exception
    rows = app.dataframe[0].value.to_dict("records")
    assert rows[0]["Статус"] == "✔ Завершена"
    assert rows[0]["Архив"].endswith("(оператор)")


def test_tasks_screen_sends_period_as_date_time(pult_with_session):
    """Фильтры периода уходят как дата-время: «чистая» дата отвечает 422 (регрессия)."""
    app, journal, _store = pult_with_session
    app = _open(app, "tasks")

    assert not app.exception
    assert not [box.value for box in app.error], "экран «Задачи» показал ошибку API"

    without_period = [record.query for record in journal.records if record.path == "/api/tasks/"]
    assert without_period, "список задач не запрашивался"
    assert all("start_date" not in query for query in without_period), (
        "период выключен — фильтры даты не должны отправляться"
    )

    _widget(app, "checkbox", "tasks_filter_period").check().run()

    assert not app.exception
    assert not [box.value for box in app.error], "фильтр периода отклонён сервером"
    sent = [
        httpx.QueryParams(record.query).get("start_date")
        for record in journal.records
        if record.path == "/api/tasks/"
    ]
    period_values = [value for value in sent if value]
    assert period_values, "фильтр периода не был отправлен"
    # «чистая» дата отклоняется сервером (422): у значения обязан быть разделитель T
    assert all(datetime.fromisoformat(value) for value in period_values)
    assert all(":" in value for value in period_values), f"«чистая» дата: {period_values}"


def test_tasks_screen_registers_and_polls_task(pult_with_session):
    """«Наблюдать» + «Опросить все сейчас»: задача и переход FSM-1 попадают в сессию."""
    app, journal, store = pult_with_session
    app = _open(app, "tasks")

    next(button for button in app.button if button.key == "tasks_watch").click().run()

    assert not app.exception
    assert any("поставлена на наблюдение" in box.value for box in app.success)
    assert [task["task_id"] for task in store.session.tasks] == [TASKS_SCREEN_ID]

    next(button for button in app.button if button.key == "tasks_poll_all").click().run()

    assert not app.exception
    assert any("Опрошено задач: 1" in box.value for box in app.success)
    task = store.session.tasks[0]
    assert task["status"] == "running"
    assert task["poll"]["polls"] >= 1
    assert [item["status"] for item in task["history"]] == ["", "running"]
    assert any(record.path == f"/api/tasks/{TASKS_SCREEN_ID}" for record in journal.records)
    assert _metric_values(app)["Активных"] == "1"


def test_tasks_screen_runs_celery_test_after_confirmation(pult_with_session):
    """Диагностика TC-TASK-01: запуск только после подтверждения, задача наблюдаема."""
    app, journal, store = pult_with_session
    app = _open(app, "tasks")

    assert _widget(app, "button", "tasks_diag_run").disabled, "запуск без подтверждения запрещён"

    _widget(app, "checkbox", "tasks_diag_confirm").check().run()
    next(button for button in app.button if button.key == "tasks_diag_run").click().run()

    assert not app.exception
    assert any("celery-test" in box.value for box in app.success)
    assert any(record.path == "/api/tasks/test" for record in journal.records)
    assert [task["task_id"] for task in store.session.tasks] == [TASKS_SCREEN_ID]
    assert "TC-TASK-01" in [record.label for record in journal.records]


def test_tasks_screen_actions_follow_fsm_1(pult_with_session):
    """Команды FSM-1 доступны по статусу: `pause` переводит задачу в «Приостановлена»."""
    app, _journal, store = pult_with_session
    app = _open(app, "tasks")
    next(button for button in app.button if button.key == "tasks_watch").click().run()
    next(button for button in app.button if button.key == "tasks_poll_all").click().run()

    _widget(app, "selectbox", f"tasks_action_obs_{TASKS_SCREEN_ID}").select("pause")
    app.run()
    next(
        button for button in app.button if button.key == f"obs_tasks_cmd_{TASKS_SCREEN_ID}"
    ).click().run()

    assert not app.exception
    assert any("переход: Выполняется → Приостановлена" in box.value for box in app.success)
    assert TASK_STATE["status"] == "paused"
    assert store.session.tasks[0]["status"] == "paused"


def test_tasks_screen_watches_external_task(pult_with_session):
    """Внешняя задача (TC-TASK-08): `task_id` вводится вручную и помечается «внешняя»."""
    app, _journal, store = pult_with_session
    app = _open(app, "tasks")

    _widget(app, "text_input", "tasks_external_id").input(TASKS_SCREEN_ID).run()
    next(button for button in app.button if button.key == "tasks_ext_add").click().run()

    assert not app.exception
    task = store.session.tasks[0]
    assert task["origin"] == "внешняя"
    assert task["check_id"] == "TC-TASK-08"
    assert any("Внешняя задача" in box.value for box in app.success)


# ---------------------------------------------------------------------------
# Экран «Чек-лист проверок» — группа TC-TASK (FR-T4) — этап T4
# ---------------------------------------------------------------------------
def test_checks_screen_shows_task_group(pult_with_session):
    """Чек-лист: 8 проверок группы TC-TASK, шаги и ожидание в карточке проверки."""
    app, _journal, _store = pult_with_session
    app = _open(app, "checks")

    assert not app.exception
    assert app.title[0].value == "Чек-лист проверок"
    values = _metric_values(app)
    assert values["Проверок в группе"] == "8"
    assert values["Не выполнено"] == "8"
    assert values["Выполнено"] == "0"

    rows = app.dataframe[0].value.to_dict("records")
    assert [row["ID"] for row in rows] == [f"TC-TASK-0{index}" for index in range(1, 9)]
    assert rows[1]["Требования"] == "UC-27, NFR-4"

    options = _widget(app, "selectbox", "checks_selected").options
    _widget(app, "selectbox", "checks_selected").select(
        next(option for option in options if option.startswith("TC-TASK-02"))
    )
    app.run()

    texts = " ".join(markdown.value for markdown in app.markdown)
    assert "GET /api/tasks/" in texts  # шаг проверки TC-TASK-02
    assert any(box.value for box in app.info)  # ожидаемый результат проверки


def test_checks_screen_runs_scenario_and_records_result(pult_with_session):
    """Автоматический сценарий TC-TASK-02 выполняется и фиксируется в сессии."""
    app, journal, store = pult_with_session
    app = _open(app, "checks")

    options = _widget(app, "selectbox", "checks_selected").options
    _widget(app, "selectbox", "checks_selected").select(
        next(option for option in options if option.startswith("TC-TASK-02"))
    )
    app.run()
    next(button for button in app.button if button.key == "checks_run_TC-TASK-02").click().run()

    assert not app.exception
    assert any("TC-TASK-02" in box.value and "успех" in box.value for box in app.success)
    stored = {item["check_id"]: item for item in store.session.checks}
    assert str(stored["TC-TASK-02"]["status"]) == "успех"
    assert stored["TC-TASK-02"]["journal_from"] is not None
    assert journal.for_label("TC-TASK-02")


def test_checks_screen_requires_operator_note_for_manual_marks(pult_with_session):
    """Ручная отметка без заключения оператора не сохраняется (NFR-T4)."""
    app, _journal, store = pult_with_session
    app = _open(app, "checks")

    options = _widget(app, "selectbox", "checks_selected").options
    _widget(app, "selectbox", "checks_selected").select(
        next(option for option in options if option.startswith("TC-TASK-08"))
    )
    app.run()
    mark_key = next(
        button.key for button in app.button if str(button.key).startswith("checks_mark_TC-TASK-08_")
    )
    next(button for button in app.button if button.key == mark_key).click().run()

    assert not app.exception
    assert any("заключения оператора" in box.value for box in app.warning)
    assert store.session.checks == []

    _widget(app, "text_area", "checks_noteTC-TASK-08").input("проверено вручную").run()
    next(button for button in app.button if button.key == mark_key).click().run()

    assert any("TC-TASK-08" in box.value for box in app.success)
    assert [item["check_id"] for item in store.session.checks] == ["TC-TASK-08"]


def test_checks_screen_creates_api_note_from_result(pult_with_session):
    """Замечание к API из карточки проверки (FR-T7): факт и ожидание предзаполнены."""
    app, _journal, store = pult_with_session
    app = _open(app, "checks")

    options = _widget(app, "selectbox", "checks_selected").options
    _widget(app, "selectbox", "checks_selected").select(
        next(option for option in options if option.startswith("TC-TASK-07"))
    )
    app.run()
    _widget(app, "text_input", "checks_note_title_TC-TASK-07").input("TC-TASK-07: дефект").run()
    next(
        button for button in app.button if button.key == "checks_note_add_TC-TASK-07"
    ).click().run()

    assert not app.exception
    assert [note["title"] for note in store.session.notes] == ["TC-TASK-07: дефект"]
    assert store.session.notes[0]["check_id"] == "TC-TASK-07"


# ---------------------------------------------------------------------------
# Консоль запросов: монитор задач (FR-T3 → T4)
# ---------------------------------------------------------------------------
def test_console_picker_substitutes_live_task_id(pult_with_session):
    """`task_id` в консоли подставляется из живого списка задач сервера."""
    app, _journal, _store = pult_with_session
    app = _open(app, "console")
    operation = _select_operation(app, "Task service", "/api/tasks/{task_id}", method="GET")

    picker_key = f"console_task_pick_{operation}"
    options = _widget(app, "selectbox", picker_key).options
    assert any(TASKS_SCREEN_ID in option for option in options)

    _widget(app, "selectbox", picker_key).select(options[0])
    app.run()
    next(button for button in app.button if button.key == f"{picker_key}_use").click().run()

    assert not app.exception
    assert app.session_state[f"console_pp_{operation}_task_id"] == TASKS_SCREEN_ID


def test_console_task_response_offers_monitor(pult_with_session):
    """Ответ с задачей предлагает наблюдение и переход в монитор (этап T4)."""
    app, _journal, store = pult_with_session
    app = _open(app, "console")
    operation = _select_operation(app, "Task service", "/api/tasks/{task_id}", method="GET")

    app.session_state[f"console_pp_{operation}_task_id"] = TASKS_SCREEN_ID
    app.run()
    _run_button(app, operation).click().run()

    assert not app.exception
    assert any("доступна монитору задач" in box.value for box in app.info)

    next(
        button for button in app.button if button.key == f"console_watch_{operation}"
    ).click().run()

    assert [task["task_id"] for task in store.session.tasks] == [TASKS_SCREEN_ID]

    next(
        button for button in app.button if button.key == f"console_monitor_{operation}"
    ).click().run()

    assert not app.exception
    assert app.session_state["pult_screen"] == "tasks"
    assert app.title[0].value == "Задачи"


# ---------------------------------------------------------------------------
# Экран «Записи (RAW + markup)» — этап T1
# ---------------------------------------------------------------------------
def _metric_values(app: AppTest) -> dict[str, str]:
    """Значения KPI экрана (подпись → значение)."""
    return {metric.label: str(metric.value) for metric in app.metric}


def _widget(app: AppTest, collection: str, key: str):
    """Виджет экрана по его ключу."""
    return next(widget for widget in getattr(app, collection) if widget.key == key)


def test_records_screen_groups_files_into_records(pult):
    app, _ = pult
    app = _open(app, "records")

    values = _metric_values(app)

    assert not app.exception
    assert app.title[0].value == "Записи (RAW + markup)"
    assert values["Записей"] == "2"
    assert values["С разметкой"] == "1 (50.0%)"
    assert values["Дублей"] == "2"
    assert values["Без разметки"] == "1"
    assert values["Разметка без RAW"] == "1"
    assert values["Прочих файлов"] == "1"


def test_records_screen_shows_duplicates_and_unpaired_sections(pult):
    app, _ = pult
    app = _open(app, "records")

    texts = " ".join(box.value for box in app.info) + " ".join(
        caption.value for caption in app.caption
    )

    assert not app.exception
    assert "дубли" in texts.lower()
    assert "Разметка без RAW" in texts or "разметки без RAW" in texts


def test_records_screen_saves_manual_decision(pult):
    """Привязка свободной разметки к записи без разметки (TC-REC-03)."""
    app, _ = pult
    app = _open(app, "records")
    target = record_id_for("Antminer_S19", UUID("33333333-3333-4333-8333-333333333333"))
    select_key = f"records_link_{target}"
    button_key = f"records_link_btn_{target}"

    options = [
        option for option in _widget(app, "selectbox", select_key).options if "Loose_Unit" in option
    ]
    assert options, "в кандидатах разметки нет свободного файла Loose_Unit.markup.csv"

    _widget(app, "selectbox", select_key).select(options[0])
    app.run()
    next(button for button in app.button if button.key == button_key).click().run()

    assert not app.exception
    assert any("Решение сохранено" in box.value for box in app.success)


def test_records_screen_marks_measured_duplicate_versions(pult):
    """Версии дублей видны оператору (в сводной таблице — все строки)."""
    app, _ = pult
    app = _open(app, "records")

    frames = app.dataframe
    assert frames, "экран не показал сводную таблицу записей"
    rows = frames[0].value.to_dict("records")

    assert sum(1 for row in rows if row["Актуальная"] == "да") == 1
    assert all("Версия" in row for row in rows)


def test_pult_started_event_is_written_to_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Проверяет реальный путь журналирования приложения (FR-T6).

    `state.ensure_logging` не подменяется: используется настоящий `setup_logging`,
    но файлы направляются во временный каталог, поэтому проверка герметична.
    """
    from acceptance.logging_setup import setup_logging

    journal = Journal(max_records=50)
    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(_handler), body_limit=500),
    )
    settings = TrainingServerSettings(base_url="http://test.local", timeout=5.0)
    runtime = Runtime(
        config=PultConfig(body_limit=500, log_bodies=True, journal_max=50),
        settings=settings,
        journal=journal,
        client=client,
        apis=Apis.build(client),
    )
    app_log = tmp_path / "app.log"
    session_log = tmp_path / "session.jsonl"

    monkeypatch.setattr(state, "get_runtime", lambda: runtime)
    monkeypatch.setattr(
        state,
        "ensure_logging",
        lambda level, session_id=None: setup_logging(
            level=level,
            session_id=session_id,
            app_log=app_log,
            session_log=session_log,
            console=False,
            enqueue=False,
        ),
    )

    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.session_state["pult_screen"] = "stand"
    app.run()

    assert not app.exception
    assert "event=pult_started" in app_log.read_text(encoding="utf-8")
    events = [
        json.loads(line)["record"]["extra"]["event"]
        for line in session_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "pult_started" in events


# ---------------------------------------------------------------------------
# Экран «Консоль запросов» (FR-T3) — этап T2
# ---------------------------------------------------------------------------
def _select_operation(app: AppTest, module: str, path_part: str, method: str = "") -> str:
    """Выбирает модуль и операцию консоли, возвращает ключ операции из реестра.

    `method` уточняет выбор, когда несколько операций модуля содержат один и тот же
    путь (например, `GET /api/tasks/{task_id}` и `POST /api/tasks/{task_id}/pause`).
    """
    _widget(app, "selectbox", "console_module").select(module)
    app.run()

    select_key = f"console_op_{module}"
    options = [
        option
        for option in _widget(app, "selectbox", select_key).options
        if path_part in option and (not method or option.startswith(method))
    ]
    assert options, f"в модуле {module} нет операции с «{method} {path_part}»"
    _widget(app, "selectbox", select_key).select(options[0])
    app.run()
    return next(spec.key for spec in ep.ENDPOINTS if spec.menu_label == options[0])


def _run_button(app: AppTest, key: str):
    """Кнопка запуска запроса консоли."""
    return next(button for button in app.button if button.key == f"console_run_{key}")


def test_console_screen_executes_reading_request(pult):
    """Читающий запрос из консоли: статус, заголовки, тело и запись журнала (FR-T3)."""
    app, journal = pult
    app = _open(app, "console")

    assert not app.exception
    assert app.title[0].value == "Консоль запросов"

    key = _select_operation(app, "Система", "/health")
    assert not _run_button(app, key).disabled
    _run_button(app, key).click().run()

    assert not app.exception
    assert any(record.path == "/health" for record in journal.records)
    values = _metric_values(app)
    assert values["Статус"] == "200"
    assert "Запись журнала" in values
    assert "Размер ответа" in values


def test_console_write_request_requires_confirmation(pult):
    """Изменяющий запрос выполняется только после подтверждения оператора (NFR-T4)."""
    app, journal = pult
    app = _open(app, "console")
    key = _select_operation(app, "Task service", "/api/tasks/test")

    assert _run_button(app, key).disabled
    _widget(app, "checkbox", f"console_confirm_{key}").check()
    app.run()
    assert not _run_button(app, key).disabled

    _run_button(app, key).click().run()

    assert not app.exception
    assert any(record.path == "/api/tasks/test" for record in journal.records)
    assert _metric_values(app)["Статус"] == "200"


def test_console_blocks_creation_without_test_prefix(pult):
    """Создание сущности без префикса `__TEST__` запрещено, с префиксом — разрешено."""
    app, journal = pult
    app = _open(app, "console")
    key = _select_operation(app, "Datasets", "/api/datasets/")

    assert key == "post /api/datasets/"
    assert _run_button(app, key).disabled
    assert any("__TEST__" in box.value for box in app.error)

    body_key = f"console_body_{key}"
    body = _widget(app, "text_area", body_key).value.replace("<name>", "__TEST__smoke")
    _widget(app, "text_area", body_key).set_value(body)
    _widget(app, "checkbox", f"console_confirm_{key}").check()
    app.run()

    assert not _run_button(app, key).disabled
    _run_button(app, key).click().run()

    assert not app.exception
    assert any(
        record.path == "/api/datasets/" and record.method == "POST" for record in journal.records
    )


def test_console_delete_requires_typed_id_for_foreign_file(pult):
    """Удаление файла без префикса `__TEST__` требует ввода id и слова DELETE (NFR-T4)."""
    app, journal = pult
    app = _open(app, "console")
    _widget(app, "selectbox", "console_module").select("File Import")
    app.run()

    key = "delete /api/{file_id}"
    options = [
        option
        for option in _widget(app, "selectbox", "console_op_File Import").options
        if option.startswith("DELETE /api/")
    ]
    _widget(app, "selectbox", "console_op_File Import").select(options[0])
    app.run()

    file_id = "22222222-2222-4222-8222-222222222222"  # markup-файл из подменённого реестра
    _widget(app, "text_input", f"console_pp_{key}_file_id").set_value(file_id)
    _widget(app, "checkbox", f"console_confirm_{key}").check()
    _widget(app, "text_input", f"console_delete_word_{key}").set_value("DELETE")
    app.run()

    assert _run_button(app, key).disabled, "без повторного ввода id удаление запрещено"

    _widget(app, "text_input", f"console_delete_id_{key}").set_value(file_id)
    app.run()

    assert not app.exception
    assert not _run_button(app, key).disabled


def test_console_registers_call_in_session(pult_with_session):
    """Вызов консоли попадает в сессию с меткой проверки и номером журнала (FR-T3/T5)."""
    app, journal, store = pult_with_session
    app = _open(app, "console")
    _widget(app, "text_input", "console_label").set_value("TC-SYS-01")
    app.run()

    key = _select_operation(app, "Система", "/health")
    _run_button(app, key).click().run()

    assert not app.exception
    assert store.session.console_calls
    call = store.session.console_calls[0]
    assert call["label"] == "TC-SYS-01"
    assert call["status"] == 200
    assert call["journal_seq"] == journal.records[-1].seq
    assert "console_call" in [item["event"] for item in store.session.history]
    assert any(record.label == "TC-SYS-01" for record in journal.records)


# ---------------------------------------------------------------------------
# Экран «Реестр замечаний к API» (FR-T7) — этап T2
# ---------------------------------------------------------------------------
def test_notes_screen_renders_registry(pult_with_session):
    """Экран замечаний: заголовок, KPI и подсказка о пустом реестре."""
    app, _, _ = pult_with_session
    app = _open(app, "notes")

    labels = [metric.label for metric in app.metric]

    assert not app.exception
    assert app.title[0].value == "Реестр замечаний к API"
    assert "Замечаний всего" in labels
    assert "P0 (блокирующие)" in labels
    assert any("Замечаний пока нет" in box.value for box in app.info)


def test_notes_screen_adds_template_and_manual_notes(pult_with_session):
    """Замечание из шаблона известного дефекта и ручное замечание попадают в сессию."""
    app, _, store = pult_with_session
    app = _open(app, "notes")

    next(button for button in app.button if button.key == "notes_add_template").click().run()

    assert not app.exception
    assert len(store.session.notes) == 1
    assert store.session.notes[0]["source"] == "авто"
    assert any("Шаблон добавлен в сессию" in box.value for box in app.success)

    _widget(app, "text_input", "notes_new_title").set_value("Замечание оператора (смоук)")
    _widget(app, "text_area", "notes_new_fact").set_value("Факт из смоук-теста")
    app.run()
    next(button for button in app.button if button.key == "notes_add_manual").click().run()

    assert len(store.session.notes) == 2
    assert any("Замечание добавлено в сессию" in box.value for box in app.success)
    assert "api_note" in [item["event"] for item in store.session.history]


def test_notes_screen_deletes_note_with_confirmation(pult_with_session):
    """Удаление замечания возможно только после подтверждения и фиксируется в истории."""
    app, _, store = pult_with_session
    app = _open(app, "notes")
    next(button for button in app.button if button.key == "notes_add_template").click().run()
    assert store.session.notes

    delete_button = next(button for button in app.button if button.key == "notes_delete")
    assert delete_button.disabled

    _widget(app, "checkbox", "notes_delete_confirm").check()
    app.run()
    next(button for button in app.button if button.key == "notes_delete").click().run()

    assert not app.exception
    assert store.session.notes == []
    assert any(item["event"] == "api_note_removed" for item in store.session.history)
    assert any("Замечание удалено из сессии" in box.value for box in app.success)
