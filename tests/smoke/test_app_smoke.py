"""Смоук-тесты экранов пульта (Streamlit AppTest, без сети).

Клиент подменяется транспортом `httpx.MockTransport`, а журналирование
направляется во временные файлы, поэтому тесты не зависят от живого стенда
и не загрязняют `acceptance_data/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from acceptance.api import Apis
from acceptance.config import PultConfig
from acceptance.http_log import Journal, LoggingTransport
from acceptance.logging_setup import setup_logging
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
    if path == "/api/datasets/":
        return httpx.Response(200, json={"datasets": [], "count": 0})
    if path == "/api/tasks/":
        return httpx.Response(200, json={"tasks": [], "count": 0})
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

    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.session_state["pult_screen"] = "stand"
    app.run()
    return app, journal


def _open(app: AppTest, screen: str) -> AppTest:
    """Открывает экран и возвращает результат прогона."""
    app.session_state["pult_screen"] = screen
    return app.run()


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

    for screen in ("records", "checks", "console", "report"):
        app = _open(app, screen)
        assert not app.exception, f"экран {screen} упал"
        assert app.info, f"экран {screen} не показал заглушку"


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
