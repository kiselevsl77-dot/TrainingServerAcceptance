"""Смоук-тесты экранов пульта (Streamlit AppTest, без сети).

Клиент подменяется транспортом `httpx.MockTransport`, а журналирование
направляется во временные файлы, поэтому тесты не зависят от живого стенда
и не загрязняют `acceptance_data/`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from acceptance import endpoints as ep
from acceptance import overrides as overrides_api
from acceptance import report as report_api
from acceptance import task_snapshot as task_snapshot_api
from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.config import PultConfig
from acceptance.exchange import build_console_client
from acceptance.http_log import HttpExchange, Journal, LoggingTransport
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


def test_logs_screen_filters_by_check_label(pult_with_session):
    """Журнал: метка проверки из журнала доступна в пикере и фильтрует записи (FR-T6, T3)."""
    app, journal, _store = pult_with_session
    app = _open(app, "checks")
    options = _widget(app, "selectbox", "checks_selected").options
    _widget(app, "selectbox", "checks_selected").select(
        next(option for option in options if option.startswith("TC-TASK-02"))
    )
    app.run()
    next(button for button in app.button if button.key == "checks_run_TC-TASK-02").click().run()
    assert journal.for_label("TC-TASK-02")

    app = _open(app, "logs")

    assert not app.exception
    labels = _widget(app, "selectbox", "logs_label_choice").options
    assert "TC-TASK-02" in labels
    assert _widget(app, "selectbox", "logs_task").options[0].startswith("— все задачи —")

    _widget(app, "selectbox", "logs_label_choice").select("TC-TASK-02")
    app.run()
    assert not app.exception
    assert not any("По заданным фильтрам записей нет" in box.value for box in app.warning)

    _widget(app, "text_input", "logs_text").set_value("/api/__nothing__").run()
    assert any("По заданным фильтрам записей нет" in box.value for box in app.warning)


def test_report_screen_renders_kit(pult_with_session):
    """Экран «Отчёт испытаний»: KPI готовности, «чего не хватает» и предпросмотр (FR-T8/T9)."""
    app, _journal, _store = pult_with_session
    app = _open(app, "report")

    assert not app.exception
    assert app.title[0].value == "Отчёт испытаний"
    values = _metric_values(app)
    assert values["Проверки"].startswith("0 / ")
    assert "Успех / отказ" in values
    assert any("Отчёт неполон" in box.label for box in app.expander)
    texts = " ".join(markdown.value for markdown in app.markdown)
    assert "Протокол испытаний сервера обучения" in texts  # предпросмотр отчёта сформирован


def test_report_screen_saves_kit_to_artifacts(
    pult_with_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Кнопка «в артефакты» сохраняет md/json с приложениями и регистрирует их в сессии."""
    app, _journal, store = pult_with_session
    monkeypatch.setattr(report_api, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(report_api, "ensure_dirs", lambda: None)
    app = _open(app, "report")

    next(button for button in app.button if button.key == "report_save_kit").click().run()

    assert not app.exception
    written = sorted(path.name for path in (tmp_path / "reports").iterdir())
    assert any(name.endswith(".md") for name in written)
    assert any(name.endswith(".json") for name in written)
    assert any(name.endswith("_checks.csv") for name in written)
    kinds = [item["kind"] for item in store.session.artifacts]
    assert any(kind.startswith("report:") for kind in kinds)


# ---------------------------------------------------------------------------
# Экран «Задачи» — монитор задач (FR-8, FR-T11) — этап T4
# ---------------------------------------------------------------------------
def test_tasks_screen_renders_kpi_list_and_observation(pult_with_session):
    """Экран «$Задачи»: KPI монитора, список сервера со статусами и вкладка наблюдения."""
    app, journal, _store = pult_with_session
    app = _open(app, "tasks")

    assert not app.exception
    assert app.title[0].value == "$Задачи"
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
    """Чек-лист: сводка по каталогу (48/69), фильтр по группе TC-TASK, шаги в карточке (FR-T4)."""
    app, _journal, _store = pult_with_session
    app = _open(app, "checks")

    assert not app.exception
    assert app.title[0].value == "Чек-лист проверок"
    values = _metric_values(app)
    assert values["Проверок в каталоге"] == "48 / 69"
    assert values["В выборке"] == "48"
    assert values["Не выполнено"] == "48"
    assert values["Выполнено"] == "0"

    options = _widget(app, "selectbox", "checks_group").options
    _widget(app, "selectbox", "checks_group").select(
        next(option for option in options if option.startswith("TC-TASK ·"))
    )
    app.run()

    assert not app.exception
    values = _metric_values(app)
    assert values["В выборке"] == "8"
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


def test_checks_screen_shows_datasets_group(pult_with_session):
    """Чек-лист: группа TC-DS (7 проверок), пикер датасета и режим состава (этап T5)."""
    app, _journal, _store = pult_with_session
    app = _open(app, "checks")

    options = _widget(app, "selectbox", "checks_group").options
    _widget(app, "selectbox", "checks_group").select(
        next(option for option in options if option.startswith("TC-DS ·"))
    )
    app.run()

    assert not app.exception
    rows = app.dataframe[0].value.to_dict("records")
    assert [row["ID"] for row in rows] == [f"TC-DS-0{index}" for index in range(1, 8)]
    assert _metric_values(app)["В выборке"] == "7"

    options = _widget(app, "selectbox", "checks_selected").options
    _widget(app, "selectbox", "checks_selected").select(
        next(option for option in options if option.startswith("TC-DS-03"))
    )
    app.run()

    assert not app.exception
    # реестр датасетов пуст → доступен ручной ввод `dataset_id` и выбор режима состава
    assert _widget(app, "text_input", "checks_datasetTC-DS-03") is not None
    assert _widget(app, "selectbox", "checks_composition_modeTC-DS-03") is not None
    texts = " ".join(markdown.value for markdown in app.markdown)
    assert "состав" in texts


def test_checks_screen_filters_by_class_and_search(pult_with_session):
    """Чек-лист: фильтры по классу и поиску отбирают только нужные проверки (этап T3)."""
    expected = [
        spec.check_id
        for group in catalog.groups(implemented_only=True)
        for spec in group.checks
        if str(spec.check_class) == "live"
    ]
    app, _journal, _store = pult_with_session
    app = _open(app, "checks")

    _widget(app, "multiselect", "checks_filter_class").select("live")
    app.run()

    assert not app.exception
    live_rows = app.dataframe[0].value.to_dict("records")
    assert [row["ID"] for row in live_rows] == expected
    assert _metric_values(app)["В выборке"] == str(len(expected))

    _widget(app, "text_input", "checks_filter_text").set_value("TC-LOAD-07").run()

    assert not app.exception
    assert _metric_values(app)["В выборке"] == "0"
    assert any("По заданным фильтрам проверок нет" in box.value for box in app.info)


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
    assert app.title[0].value == "$Задачи"


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


# ---------------------------------------------------------------------------
# Экран «Датасеты» — реестр, состав, наполнение и уборка (FR-4) — этап T5
# ---------------------------------------------------------------------------
def test_datasets_screen_shows_registry_and_controls(pult_with_session):
    """Датасеты: KPI, пустой реестр, режим состава и подтверждение наполнения (этап T5)."""
    app, _journal, _store = pult_with_session
    app = _open(app, "datasets")

    assert not app.exception
    assert app.title[0].value == "$Датасеты"
    values = _metric_values(app)
    assert values["Датасетов в реестре"] == "0"
    assert values["Создано пультом (`__TEST__`)"] == "0"
    assert values["Записей состава"] == "0"
    assert values["Задач `dataset-fill`"] == "0"

    # вкладка «Реестр»: пустой реестр — приглашение создать датасет
    assert any("По заданным фильтрам датасетов нет" in box.value for box in app.info)
    # вкладка «Создание»: имя по умолчанию начинается с обязательного префикса
    assert _widget(app, "text_input", "datasets_new_name").value.startswith("__TEST__")
    # вкладка «Состав»: ручной ввод `dataset_id` и режим чтения состава
    assert _widget(app, "text_input", "datasets_card_manual") is not None
    assert _widget(app, "selectbox", "datasets_card_mode") is not None
    # вкладка «Наполнение»: пары RAW+markup и обязательное подтверждение (NFR-T4)
    assert _widget(app, "multiselect", "datasets_fill_pairs") is not None
    assert _widget(app, "checkbox", "datasets_fill_confirm") is not None
    # вкладка «Уборка»: тестовых датасетов нет
    assert any(
        "Тестовых датасетов в реестре и в учёте сессии нет" in box.value for box in app.success
    )


def test_datasets_screen_requires_confirmation_for_fill(pult_with_session):
    """Наполнение без подтверждения не выполняется: оператор видит предупреждение (NFR-T4)."""
    app, _journal, _store = pult_with_session
    app = _open(app, "datasets")

    next(button for button in app.button if button.key == "datasets_fill").click().run()

    assert not app.exception
    assert any("Подтвердите наполнение" in box.value for box in app.warning)


# ---------------------------------------------------------------------------
# Экран «Монитор обмена» (этапы К3/M1): лента вызовов и ответов
# ---------------------------------------------------------------------------
def _journal_record(seq: int = 1) -> HttpExchange:
    """Запись журнала для ленты монитора (без обращения к сети)."""
    return HttpExchange(
        seq=seq,
        started_at="2026-09-21T10:00:00",
        method="GET",
        path="/api/data/files",
        query="file_name=Antminer",
        status=200,
        duration_ms=42.0,
        request_bytes=0,
        response_bytes=64,
        content_type="application/json",
        label="TC-FILE-01",
        response_body='{"count": 1}',
        request_headers=(("accept", "*/*"),),
        response_headers=(("content-type", "application/json"),),
    )


def _add_record(journal: Journal, record: HttpExchange) -> HttpExchange:
    """Кладёт запись в журнал с уникальным номером (в фикстуре уже есть обмены стенда)."""
    return journal.add(replace(record, seq=journal.next_seq()))


def test_monitor_screen_renders_feed_and_detail_controls(pult_with_session):
    """Экран «Монитор обмена»: лента вызовов, подробность запроса/ответа, `curl`."""
    app, journal, _store = pult_with_session
    _add_record(journal, _journal_record())

    app = _open(app, "monitor")

    assert not app.exception
    assert app.title[0].value == "Монитор обмена"
    labels = [box.label for box in app.selectbox]
    assert "Подробность запроса" in labels
    assert "Подробность ответа" in labels
    # лента показывает запись: строка вызова и команда `curl` для копирования
    markdown = " ".join(block.value for block in app.markdown)
    assert "GET /api/data/files?file_name=Antminer" in markdown
    codes = " ".join(block.value for block in app.code)
    assert "curl -X GET" in codes
    assert not app.error


def test_monitor_screen_filters_and_orders_the_feed(pult_with_session):
    """Фильтры ленты: «только ошибки» и метка проверки; порядок переключается."""
    app, journal, _store = pult_with_session
    journal.clear()
    _add_record(journal, _journal_record())
    _add_record(
        journal,
        HttpExchange(
            seq=0,
            started_at="2026-09-21T10:00:01",
            method="POST",
            path="/api/datasets/",
            query="",
            status=422,
            duration_ms=8.0,
            request_bytes=10,
            response_bytes=90,
            content_type="application/json",
            label="TC-DS-01",
            response_body='{"detail": "validation"}',
        ),
    )

    app = _open(app, "monitor")
    app = app.checkbox(key="monitor_only_errors").set_value(True).run()

    assert not app.exception
    markdown = " ".join(block.value for block in app.markdown)
    # лента использует путь с query (`record.url`), поэтому строка успешного чтения
    # отличается от строки плана с той же операцией
    assert "POST /api/datasets/" in markdown
    assert "GET /api/data/files?file_name=Antminer" not in markdown


def test_monitor_screen_shows_empty_feed_message(pult_with_session):
    """Пустая лента объясняет оператору, что вызовов ещё не было."""
    app, journal, _store = pult_with_session
    journal.clear()

    app = _open(app, "monitor")

    assert not app.exception
    assert any("Лента пуста" in box.value for box in app.info)


def test_monitor_plan_executes_one_call_at_a_time(pult_with_session):
    """Пожелание п. 2: «Выполнить следующую» отправляет **один** вызов и оценивает его."""
    app, journal, _store = pult_with_session
    app = _open(app, "monitor")
    headers = " ".join(block.value for block in app.subheader)
    markdown = " ".join(block.value for block in app.markdown)
    assert "Программа испытаний" in headers
    assert "Лента обмена" in headers
    assert "TC-SYS-01#1" in markdown, "в программе видны запланированные вызовы"

    app = app.radio(key="plan_mode").set_value("выполнять по вызовам").run()
    before = len(journal.records)
    app = app.button(key="plan_next").click().run()

    assert not app.exception
    assert len(journal.records) > before, "вызов ушёл на стенд"
    holder = app.session_state["pult_plan"]
    item = holder["plan"].find("TC-SYS-01#1")
    assert item is not None
    assert item.status != "ожидает", "пункт плана получил результат"
    assert item.verdict, "у вызова есть вердикт соответствия ожиданию"
    assert item.journal_from is not None


def test_monitor_plan_checkbox_and_reset_work(pult_with_session):
    """Галочку пункта можно снять, а результаты — сбросить кнопкой «↺ Сброс»."""
    app, _journal, _store = pult_with_session
    app = _open(app, "monitor")

    app = app.checkbox(key="plan_chk_TC-SYS-01").set_value(False).run()

    assert not app.exception
    plan_state = app.session_state["pult_plan"]["plan"]
    assert plan_state.find("TC-SYS-01").enabled is False
    assert all(call.enabled is False for call in plan_state.calls_of("TC-SYS-01"))

    app = app.button(key="plan_reset").click().run()

    assert not app.exception
    assert app.session_state["pult_plan"]["plan"].summary()["done"] == 0


def test_monitor_repeat_with_corrections_sends_request(pult_with_session):
    """Пожелание п. 1: вызов из ленты можно повторить с правками, не уходя в консоль."""
    app, journal, _store = pult_with_session
    journal.clear()
    record = _add_record(journal, _journal_record())
    app = _open(app, "monitor")
    before = len(journal.records)

    app = app.button(key=f"monitor_repeat_{record.seq}_send").click().run()

    assert not app.exception
    assert len(journal.records) > before, "повтор ушёл на стенд"
    assert journal.records[-1].method == record.method
    assert journal.records[-1].label == record.label, "повтор помечен той же меткой"


def test_monitor_note_from_record_goes_to_session(pult_with_session):
    """Замечание к API создаётся прямо из записи ленты (факт и `curl` уже подставлены)."""
    app, journal, store = pult_with_session
    journal.clear()
    record = _add_record(journal, _journal_record())
    app = _open(app, "monitor")

    app = app.button(key=f"monitor_note_{record.seq}_create").click().run()

    assert not app.exception
    assert store.session.notes, "замечание попало в сессию"
    note = store.session.notes[-1]
    assert note["endpoint"] == f"{record.method} {record.path}"
    assert "curl" in str(note["reproduction"])


def test_every_registered_screen_renders(pult_with_session):
    """Каждый экран пульта открывается без исключений — включая новый «Монитор обмена».

    Список повторяет `acceptance.app.SCREENS` (импортировать модуль в тесте нельзя: он
    выполняет вызовы Streamlit на импорте), поэтому при добавлении экрана его нужно
    внести и сюда — тест сразу покажет, что новый экран падает при открытии.
    """
    app, _journal, _store = pult_with_session
    screens = (
        "stand",
        "session",
        "records",
        "tasks",
        "datasets",
        "checks",
        "monitor",
        "console",
        "notes",
        "logs",
        "report",
    )

    for screen in screens:
        app = _open(app, screen)
        assert not app.exception, f"экран {screen} упал при открытии"
        assert app.title, f"экран {screen} не показал заголовок"
