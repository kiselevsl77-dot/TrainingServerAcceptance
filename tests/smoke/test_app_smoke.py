"""Смоук-тесты экранов пульта (Streamlit AppTest, без сети).

Клиент подменяется транспортом `httpx.MockTransport`, а журналирование
направляется во временные файлы, поэтому тесты не зависят от живого стенда
и не загрязняют `acceptance_data/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from acceptance import endpoints as ep
from acceptance import overrides as overrides_api
from acceptance.api import Apis
from acceptance.config import PultConfig
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.logging_setup import setup_logging
from acceptance.overrides import OVERRIDES_FILENAME, Overrides
from acceptance.records import record_id_for
from acceptance.session import new_session
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
        return httpx.Response(200, json={"task_id": "task-smoke-1", "status": "new"})
    if path == "/api/tasks/":
        return httpx.Response(200, json={"tasks": [], "count": 0})
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
    # кэш реестра файлов общий для процесса: сбрасываем, чтобы тесты не влияли друг на друга
    state.load_files.clear()

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

    for screen in ("checks", "report"):
        app = _open(app, screen)
        assert not app.exception, f"экран {screen} упал"
        assert app.info, f"экран {screen} не показал заглушку"


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
def _select_operation(app: AppTest, module: str, path_part: str) -> str:
    """Выбирает модуль и операцию консоли, возвращает ключ операции из реестра."""
    _widget(app, "selectbox", "console_module").select(module)
    app.run()

    select_key = f"console_op_{module}"
    options = [
        option for option in _widget(app, "selectbox", select_key).options if path_part in option
    ]
    assert options, f"в модуле {module} нет операции с «{path_part}»"
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
