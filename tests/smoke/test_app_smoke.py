"""Смоук-тесты каркаса пульта (Streamlit `AppTest`, без сети).

Гейт этапа 2: все 15 экранов (`SCR-101`…`SCR-501`) открываются без исключений, экран
показывает свой код, группу и фазу, а запуск пульта попадает в журнал (`pult_started`).

Клиент подменяется транспортом `httpx.MockTransport`, журналирование направляется во
временные файлы, поэтому тесты не зависят от живого стенда и не загрязняют `acceptance_data/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from acceptance import glossary
from acceptance.api import Apis
from acceptance.config import PultConfig
from acceptance.http_log import Journal, LoggingTransport
from acceptance.logging_setup import setup_logging
from acceptance.ui import nav, state
from acceptance.ui.state import Runtime
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

APP_PATH = Path(__file__).resolve().parents[2] / "acceptance" / "app.py"
BASE_URL = "http://test.local"


def _handler(request: httpx.Request) -> httpx.Response:
    """Заглушка стенда: каркас экранов вызовов не делает, но клиент должен быть готов."""
    return httpx.Response(200, json={"status": "ok", "message": "test stand ready"})


@pytest.fixture
def pult(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AppTest, Path, Path]:
    """AppTest пульта с подменённым runtime и журналированием во временные файлы."""
    if str(APP_PATH.parents[1]) not in sys.path:
        sys.path.insert(0, str(APP_PATH.parents[1]))

    journal = Journal(max_records=50)
    client = ApiHttpClient(
        base_url=BASE_URL,
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(_handler), body_limit=200),
    )
    runtime = Runtime(
        config=PultConfig(body_limit=200, log_bodies=True, journal_max=50),
        settings=TrainingServerSettings(base_url=BASE_URL, timeout=5.0),
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
    app.run()
    return app, artifacts.app_log, artifacts.session_log


def _open(app: AppTest, key: str) -> AppTest:
    """Открывает экран по ключу маршрута и возвращает результат прогона."""
    app.session_state["pult_screen"] = key
    return app.run()


def test_default_screen_is_sets(pult: tuple[AppTest, Path, Path]) -> None:
    """Пульт открывается на «Наборах проверок» (`SCR-101`) — решение заказчика 22.09.2026."""
    app, _, _ = pult

    assert not app.exception
    assert app.session_state["pult_screen"] == nav.DEFAULT_SCREEN
    assert app.title[0].value == glossary.SCREEN_LABELS[nav.DEFAULT_SCREEN]


def test_every_screen_opens(pult: tuple[AppTest, Path, Path]) -> None:
    """Все 15 экранов открываются без исключений и показывают заголовок (гейт этапа 2)."""
    app, _, _ = pult

    for key in nav.screens():
        app = _open(app, key)
        assert not app.exception, f"экран {key} упал при открытии"
        assert app.title[0].value == glossary.SCREEN_LABELS[key]


def test_screen_shows_code_group_and_phase(pult: tuple[AppTest, Path, Path]) -> None:
    """Шапка экрана называет код, группу и фазу — так экран сличается с макетом `docs/16`."""
    app, _, _ = pult
    app = _open(app, "scr301_run")
    captions = [item.value for item in app.caption]
    subheaders = [item.value for item in app.subheader]

    assert any("SCR-301" in text and "Испытания" in text and "Ф1–Ф2" in text for text in captions)
    assert "Рабочая область" in subheaders, "нет рабочей области (третья зона макета)"
    assert "Панель контекста" in subheaders, "нет панели контекста (третья зона макета)"


def test_screen_shows_blocks_states_and_transitions(pult: tuple[AppTest, Path, Path]) -> None:
    """Скелет показывает блоки макета, состояния (`IR-P-8`) и переходы на другие экраны."""
    app, _, _ = pult
    app = _open(app, "scr301_run")
    markdown = " ".join(item.value for item in app.markdown)

    assert "Одна команда запуска" in markdown, "не показан блок макета"
    assert "Нет сессии" in markdown, "не показано состояние экрана"
    assert "SCR-102" in markdown, "не показан переход к программе сессии"


def test_sidebar_lists_five_groups_and_all_screens(pult: tuple[AppTest, Path, Path]) -> None:
    """Боковая панель: пять групп навигации и по кнопке на каждый из 15 экранов."""
    app, _, _ = pult
    sidebar_markdown = " ".join(item.value for item in app.sidebar.markdown)

    for group, _keys in nav.GROUPS:
        assert group in sidebar_markdown, f"в навигации нет группы «{group}»"
    assert len(app.sidebar.button) == 15, "в навигации не 15 экранов"


def test_sidebar_shows_stand_and_session_state(pult: tuple[AppTest, Path, Path]) -> None:
    """Шапка показывает стенд и честно сообщает, что сессия не выбрана (`IR-P-8`)."""
    app, _, _ = pult
    captions = " ".join(item.value for item in app.sidebar.caption)

    assert BASE_URL in captions, "в шапке нет адреса стенда"
    assert "Сессия испытаний не выбрана" in " ".join(item.value for item in app.sidebar.warning), (
        "шапка не сообщает об отсутствии сессии"
    )


def test_pult_started_event_is_written_to_logs(pult: tuple[AppTest, Path, Path]) -> None:
    """Запуск пульта попадает в журнал запуска, а событие с экраном — в JSONL сессии (FR-T6)."""
    app, app_log, session_log = pult

    assert not app.exception
    assert "event=pult_started" in app_log.read_text(encoding="utf-8")
    events = [
        json.loads(line)["record"]["extra"]
        for line in session_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    started = [item for item in events if item.get("event") == "pult_started"]

    assert started, "в JSONL сессии нет события pult_started"
    assert started[-1]["payload"]["screen"] == nav.DEFAULT_SCREEN
    assert started[-1]["payload"]["screen_code"] == nav.by_key(nav.DEFAULT_SCREEN).code


# ---------------------------------------------------------------------------
# Наполненные экраны (этап 3): экран показывает данные, а не скелет каркаса
# ---------------------------------------------------------------------------
def test_session_screen_offers_to_create_session(pult: tuple[AppTest, Path, Path]) -> None:
    """`SCR-203` без сессии предлагает создать её — состояние «нет сессии» (`IR-P-8`)."""
    app, _, _ = pult
    app = _open(app, "scr203_session")

    assert not app.exception
    assert "Создать сессию" in [button.label for button in app.button]
    assert any("Сессия не выбрана" in item.value for item in app.info), (
        "экран не объясняет, зачем нужна сессия"
    )
    assert "Новая сессия" in [item.value for item in app.subheader]


def test_stand_screen_shows_server_and_registries_blocks(pult: tuple[AppTest, Path, Path]) -> None:
    """`SCR-202` показывает адрес стенда, кнопку проверки и блок реестров (макет `docs/16`)."""
    app, _, _ = pult
    app = _open(app, "scr202_stand")

    assert not app.exception
    subheaders = [item.value for item in app.subheader]
    captions = " ".join(item.value for item in app.caption)

    assert "Сервер и сборка" in subheaders
    assert "Реестры стенда" in subheaders
    assert "🔄 Проверить" in [button.label for button in app.button]
    assert BASE_URL in captions, "экран не показывает адрес испытуемого стенда"


def test_filled_screens_have_no_stage_badge(pult: tuple[AppTest, Path, Path]) -> None:
    """Наполненный экран не показывает бейдж каркаса этапа 2: это признак заглушки."""
    app, _, _ = pult

    for key in ("scr202_stand", "scr203_session"):
        app = _open(app, key)
        assert not any("Каркас этапа 2" in item.value for item in app.info), (
            f"экран {key} всё ещё показывает заглушку каркаса"
        )
