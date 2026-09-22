"""Смоук-«вертикальный срез»: сессия → программа → утверждение → очередь прогона.

Этот тест — главный гейт этапа 3 (`.clinerules`): он проверяет не «экран открылся», а что
**процесс испытаний проходит через экраны**. Срез идёт через интерфейс (`AppTest`), как это
делал бы оператор:

    1. `SCR-203` — создать сессию и начать её;
    2. `SCR-102` — собрать программу из наборов, утвердить ревизию, собрать очередь прогона;
    3. `SCR-301` — «▶ Следующая» и результат проверки;
    4. `SCR-302` — карточка проверки с шагами, payload, доказательствами и отметкой;
    5. `SCR-401` — протокол: вердикт, диапазон журнала, KPI и выгрузка выборки.

Стенд подменён `httpx.MockTransport`, сессии пишутся во временный каталог, библиотека наборов
собирается из каталога проверок: тест не зависит от живого стенда и не трогает `acceptance_data/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.api import Apis
from acceptance.config import PultConfig
from acceptance.http_log import Journal, LoggingTransport
from acceptance.logging_setup import setup_logging
from acceptance.session import TestSession as SessionModel
from acceptance.ui import state
from acceptance.ui.state import Runtime
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

APP_PATH = Path(__file__).resolve().parents[2] / "acceptance" / "app.py"
BASE_URL = "http://test.local"
AUTHOR = "Петров П.П."


def _handler(request: httpx.Request) -> httpx.Response:
    """Подменённый стенд: версия сборки и health (срез планирования читает именно их)."""
    if request.url.path == "/version":
        return httpx.Response(200, json={"branch": "dev", "revision": "83319ae1234"})
    return httpx.Response(200, json={"status": "ok", "message": "test stand ready"})


@pytest.fixture
def pult(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AppTest, Path]:
    """Пульт с подменённым стендом, временным каталогом сессий и готовой библиотекой наборов."""
    root = APP_PATH.parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

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
        session_id="slice",
        app_log=tmp_path / "app.log",
        session_log=tmp_path / "session.jsonl",
        console=False,
        enqueue=False,
    )
    store = tmp_path / "sessions"
    library = sets_api.library_from_catalog(author=AUTHOR)

    def towards_store(name: str) -> object:
        """Обёртка функции сессии, читающей/пишущей во временный каталог."""
        original = getattr(session_api, name)

        def wrapped(*args: object, **kwargs: object) -> object:
            return original(*args, **{**kwargs, "directory": store})

        return wrapped

    monkeypatch.setattr(state, "get_runtime", lambda: runtime)
    monkeypatch.setattr(state, "ensure_logging", lambda level, session_id=None: artifacts)
    monkeypatch.setattr(state, "sets_library", lambda: library)
    monkeypatch.setattr(state, "save_sets_library", lambda value: tmp_path / "check_sets.json")
    for name in ("save_session", "load_session", "session_exists"):
        patched = towards_store(name)
        monkeypatch.setattr(session_api, name, patched)
        monkeypatch.setattr(state, name, patched)
    monkeypatch.setattr(session_api, "list_sessions", lambda directory=None: [])

    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.run()
    return app, store


def _open(app: AppTest, key: str) -> AppTest:
    """Открывает экран по ключу маршрута."""
    app.session_state["pult_screen"] = key
    return app.run()


def _click(app: AppTest, label: str) -> AppTest:
    """Нажимает кнопку экрана по её подписи (подписи кнопок — часть макета)."""
    button = next(item for item in app.button if item.label == label)
    return button.click().run()


def _captions(app: AppTest) -> str:
    """Все подписи экрана одной строкой — по ним проверяются итоги шагов."""
    return " | ".join(item.value for item in app.caption)


def test_vertical_slice_sets_catalog_and_membership(pult: tuple[AppTest, Path]) -> None:
    """Срез начинается с наборов: каталог слева, состав набора справа (`SCR-101`)."""
    app, _store = pult
    app = _open(app, "scr101_sets")
    app.selectbox[0].set_value(sets_api.SMOKE_SET_ID)
    app = app.run()
    frames = [item.value for item in app.dataframe]
    subheaders = [item.value for item in app.subheader]

    assert not app.exception
    assert any(text.startswith("Каталог проверок") for text in subheaders), "нет каталога проверок"
    catalog_table = next(frame for frame in frames if "member" in frame.columns)
    members_table = next(frame for frame in frames if "order" in frame.columns)

    assert len(catalog_table) > len(members_table), "каталог шире состава набора"
    assert members_table["check_id"].iloc[0] == "TC-SYS-01", "порядок состава потерян"
    assert str(members_table["mark"].iloc[0]) == "★", "смоук-пункт обязан быть обязательным"
    assert any(item.label.startswith("→ К программе сессии (SCR-102)") for item in app.button), (
        "нет перехода к сборке программы"
    )
    assert not any("Следующая" in item.label for item in app.button), (
        "в планировании нет команды запуска (IR-P-18)"
    )


def _messages(app: AppTest) -> str:
    """Сообщения экрана (успех/внимание/ошибка/подсказка) одной строкой."""
    groups = (app.success, app.warning, app.error, app.info)
    return " | ".join(item.value for group in groups for item in group)


def _stored(store: Path, session_id: str) -> SessionModel:
    """Сессия, записанная экранами на диск (временный каталог теста)."""
    return session_api.load_session(session_id, directory=store)


def test_vertical_slice_planning_to_queue(pult: tuple[AppTest, Path]) -> None:
    """Срез: сессия → программа → утверждение → очередь прогона — всё через экраны."""
    app, store = pult

    # 1. SCR-203: создаём сессию и начинаем её.
    app = _open(app, "scr203_session")
    app = _click(app, "Создать сессию")
    session_id = app.session_state["pult_session_id"]

    assert session_id, "сессия не стала текущей после создания"

    app = _click(app, "▶ Начать сессию")
    session = _stored(store, session_id)

    assert session.status == session_api.STATUS_RUNNING, "сессия не перешла в статус «идёт»"
    assert session.server_build == "dev@83319ae1234", "сборка стенда не зафиксирована в сессии"

    # 2. SCR-102: собираем программу из наборов (смоук-минимум + полный раздел).
    app = _open(app, "scr102_programme")
    library = sets_api.library_from_catalog(author=AUTHOR)
    section_set = library.by_section("TC-SYS")[0].set_id
    app.multiselect[0].set_value([sets_api.SMOKE_SET_ID, section_set])
    app = _click(app, "🧩 Собрать программу (ИЛИ)")

    assert "Включено по ИЛИ:" in _captions(app), "программа не собрана объединением по ИЛИ"
    session = _stored(store, session_id)

    assert session.programme["items"], "состав программы не сохранён в сессии"

    # 3. Утверждаем ревизию: без автора утверждение не проходит.
    app = _click(app, "✅ Утвердить программу")

    assert "укажите, кто утверждает" in _messages(app).lower(), (
        "утверждение без автора должно быть отклонено"
    )

    app.text_input[0].set_value(AUTHOR)
    app = _click(app, "✅ Утвердить программу")
    session = _stored(store, session_id)

    assert session.programme["status"] == "утверждена", "ревизия программы не утверждена"
    assert session.programme["approved"]["author"] == AUTHOR

    # 4. Собираем очередь прогона — снимок утверждённой ревизии.
    app = _click(app, "🧾 Собрать очередь прогона")
    session = _stored(store, session_id)
    queue = session.queue

    assert queue["items"], "очередь прогона пуста"
    assert queue["approved"] is True, "очередь собрана не по утверждённой ревизии"
    assert queue["revision"] == session.programme["revision"]
    assert len(queue["items"]) == len(session.programme["items"])
    assert "очередь прогона:" in _captions(app), "экран не показывает собранную очередь"


def test_slice_programme_screen_without_session_says_so(pult: tuple[AppTest, Path]) -> None:
    """Без сессии экран программы честно сообщает об этом и не даёт команд запуска."""
    app, _ = pult
    app = _open(app, "scr102_programme")

    assert any("Сессия испытаний не выбрана" in item.value for item in app.warning)
    assert "▶ Следующая" not in " ".join(item.label for item in app.button)
    assert "Наборы проверок" in [item.value for item in app.subheader], (
        "выбор наборов виден даже без сессии — это контекст планирования"
    )


def _plan(app: AppTest) -> tuple[AppTest, str]:
    """Проходит планирование: сессия → программа из наборов → утверждение → очередь прогона."""
    app = _open(app, "scr203_session")
    app = _click(app, "Создать сессию")
    session_id = str(app.session_state["pult_session_id"])
    app = _click(app, "▶ Начать сессию")

    app = _open(app, "scr102_programme")
    section_set = sets_api.library_from_catalog().by_section("TC-SYS")[0].set_id
    app.multiselect[0].set_value([sets_api.SMOKE_SET_ID, section_set])
    app = _click(app, "🧩 Собрать программу (ИЛИ)")
    app.text_input[0].set_value(AUTHOR)
    app = _click(app, "✅ Утвердить программу")
    app = _click(app, "🧾 Собрать очередь прогона")
    return app, session_id


def test_vertical_slice_run_next_check(pult: tuple[AppTest, Path]) -> None:
    """Срез продолжается прогоном: «▶ Следующая» отправляет один пункт и пишет результат."""
    app, store = pult
    app, session_id = _plan(app)

    app = _open(app, "scr301_run")
    subheaders = [item.value for item in app.subheader]

    assert any(text.startswith("Очередь программы") for text in subheaders), "нет зоны очереди"
    assert "Управление" in subheaders, "нет зоны управления прогоном"
    assert "Лента обмена" in subheaders, "нет ленты обмена"
    assert "Вердикт и след" in subheaders, "нет панели контекста"

    labels = [item.label for item in app.button]
    assert any(label.startswith("▶ Следующая: TC-SYS-01") for label in labels), (
        "единственная команда запуска должна называть пункт очереди (IR-P-4)"
    )

    app = _click(
        app,
        next(label for label in labels if label.startswith("▶ Следующая:")),
    )
    session = _stored(store, session_id)
    checks = {str(item["check_id"]): item for item in session.checks}

    assert checks["TC-SYS-01"]["status"] == "успех", "результат прогона не записан в сессию"
    assert checks["TC-SYS-01"]["origin"] == "прогон"
    assert session.queue["items"][0]["state"] == "выполнена", "пункт очереди не закрыт"
    assert session.queue["items"][0]["journal_to"], "диапазон журнала не зафиксирован"
    assert "Вердикт" in " ".join(_captions(app)) or "успех" in _messages(app)


def test_vertical_slice_check_card_shows_result(pult: tuple[AppTest, Path]) -> None:
    """После прогона карточка проверки показывает шаги, доказательства и след обменов."""
    app, store = pult
    app, session_id = _plan(app)
    app = _open(app, "scr301_run")
    labels = [item.label for item in app.button]
    app = _click(app, next(label for label in labels if label.startswith("▶ Следующая:")))

    app = _open(app, "scr302_check")
    subheaders = [item.value for item in app.subheader]
    markdown = " ".join(item.value for item in app.markdown)

    assert not app.exception
    assert "TC-SYS-01" in _captions(app), "карточка не называет пункт очереди"
    assert "Шаги проверки" in markdown, "карточка не показывает шаги проверки"
    assert "Что будет отправлено" in subheaders, "нет блока предпросмотра payload (FR-P-20)"
    assert "Доказательства" in subheaders, "нет блока доказательств (FR-P-33)"
    assert "Отметка оператора" in subheaders, "нет блока отметки оператора (FR-P-32)"
    assert not any(item.label.startswith("▶ Следующая") for item in app.button), (
        "карточка проверки не должна давать вторую команду запуска (IR-P-4)"
    )


def test_vertical_slice_journal_shows_run_exchange(pult: tuple[AppTest, Path]) -> None:
    """Прогон оставляет машинный след: журнал показывает обмен по метке проверки (`SCR-402`)."""
    app, _store = pult
    app, _session_id = _plan(app)
    app = _open(app, "scr301_run")
    labels = [item.label for item in app.button]
    app = _click(app, next(label for label in labels if label.startswith("▶ Следующая:")))

    app = _open(app, "scr402_journal")
    frames = [item.value for item in app.dataframe]

    assert not app.exception
    table = next(frame for frame in frames if "seq" in frame.columns)
    assert "TC-SYS-01" in list(table["label"]), "метка проверки не попала в машинный след"
    assert "Обменов" in _captions(app), "нет сводки журнала"
    assert not any(item.label.startswith("▶ Следующая") for item in app.button), (
        "журнал не даёт второй команды запуска (IR-P-4)"
    )


def test_vertical_slice_protocol_shows_result(pult: tuple[AppTest, Path]) -> None:
    """Срез завершается протоколом: результат прогона виден в таблице, KPI и выгрузке."""
    app, store = pult
    app, session_id = _plan(app)
    app = _open(app, "scr301_run")
    labels = [item.label for item in app.button]
    app = _click(app, next(label for label in labels if label.startswith("▶ Следующая:")))

    app = _open(app, "scr401_protocol")
    subheaders = [item.value for item in app.subheader]
    table = app.dataframe[0].value

    assert not app.exception
    assert any(text.startswith("Протокол (") for text in subheaders), "нет таблицы протокола"
    assert "KPI выборки" in subheaders, "нет панели KPI протокола"
    assert "TC-SYS-01" in list(table["check_id"]), "результат прогона не попал в протокол"
    row = table[table["check_id"] == "TC-SYS-01"].iloc[0]
    assert str(row["status"]).endswith("успех")
    assert str(row["journal_range"]).startswith("#"), "диапазон журнала потерян"
    assert "Готовность протокола" in _captions(app)
    assert not any(item.label.startswith("▶ Следующая") for item in app.button), (
        "протокол не должен давать вторую команду запуска (IR-P-4)"
    )

    session = _stored(store, session_id)
    assert session.checks, "результат прогона потерялся до протокола"

    session = _stored(store, session_id)
    assert session.checks, "результат прогона потерялся при переходе между экранами"
