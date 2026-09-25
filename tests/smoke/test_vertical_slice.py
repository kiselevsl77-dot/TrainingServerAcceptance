"""Смоук-«вертикальный срез»: сессия → программа → утверждение → «Следующая» → протокол.

Главный гейт этапа (`.clinerules`): проверяется не «страница открылась», а что **процесс
испытаний проходит через пульт**. Срез идёт через живой сервер (`pult.server`) по HTTP —
так же, как это делает браузер оператора:

    1. страница интерфейса и шапка (`GET /`, `GET /api/state`);
    2. сессия: создание и старт (снимок «Начало»);
    3. планирование: каталог с фильтром → набор из отмеченных проверок (массовый выбор,
       «обязательная») → утверждение набора → сборка программы по наборам → покрытие и
       происхождение пунктов → утверждение ревизии с обоснованием сокращения;
    4. прогон: «▶ Следующая» и результат проверки с диапазоном журнала;
    5. протокол: закрытый пункт, CSV-выгрузка и лента обмена;
    6. готовность отчёта и фаза Ф2 (прогон пройден, снимка «Окончание» ещё нет).

Стенд подменён `httpx.MockTransport`, сессии пишутся во временный каталог, библиотека
наборов собирается из каталога проверок: тест не зависит от живого стенда и не трогает
`acceptance_data/`.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import httpx
import pytest

from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.api import Apis
from acceptance.config import PultConfig
from acceptance.http_log import Journal, LoggingTransport
from acceptance.logging_setup import setup_logging
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings
from pult.server import PultApp, serve
from pult.state import PultState, Runtime

BASE_URL = "http://test.local"
AUTHOR = "Петров П.П."
WEB_DIR = Path(__file__).resolve().parents[2] / "pult" / "web"


def _handler(request: httpx.Request) -> httpx.Response:
    """Подменённый стенд: версия сборки, пустые реестры и успех на любой вызов."""
    path = request.url.path
    if path.endswith("/version"):
        return httpx.Response(200, json={"branch": "dev", "revision": "83319ae1234"})
    if path.startswith("/api/data/files"):
        return httpx.Response(200, json={"files": [], "count": 0})
    if path.startswith("/api/loads/list"):
        return httpx.Response(200, json={"loads": [], "result_size": 0})
    if path.startswith("/api/datasets"):
        return httpx.Response(200, json={"datasets": [], "count": 0})
    if path.startswith("/api/ml_models/models"):
        return httpx.Response(200, json={"models": [], "count": 0})
    if path.startswith("/api/tasks/"):
        return httpx.Response(200, json={"tasks": [], "count": 0})
    return httpx.Response(200, json={"status": "ok", "message": "test stand ready"})


def _towards_store(name: str, store: Path) -> Any:
    """Функция сессии, работающая во временном каталоге (свой каталог не трогаем).

    Каталог подставляется только тогда, когда вызывающий его не назвал: `list_sessions`
    сам зовёт `load_session` с готовым каталогом.
    """
    original = getattr(session_api, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if len(args) > 1 or "directory" in kwargs:
            return original(*args, **kwargs)
        return original(*args, directory=store, **kwargs)

    return wrapped


@pytest.fixture
def pult(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Поднимает сервер пульта на свободном порту и возвращает его базовый адрес."""
    store = tmp_path / "sessions"
    store.mkdir()
    journal = Journal(max_records=50)
    transport = LoggingTransport(journal, inner=httpx.MockTransport(_handler), body_limit=200)
    client = ApiHttpClient(base_url=BASE_URL, timeout=5.0, transport=transport)
    runtime = Runtime(
        config=PultConfig(body_limit=200, log_bodies=True, journal_max=50),
        settings=TrainingServerSettings(base_url=BASE_URL, timeout=5.0),
        journal=journal,
        client=client,
        apis=Apis.build(client),
    )
    console = httpx.Client(base_url=BASE_URL, timeout=5.0, transport=transport)
    library = sets_api.library_from_catalog(author=AUTHOR)
    artifacts = setup_logging(
        level="DEBUG",
        session_id="slice",
        app_log=tmp_path / "app.log",
        session_log=tmp_path / "session.jsonl",
        console=False,
        enqueue=False,
    )
    for name in ("save_session", "load_session", "session_exists", "list_sessions"):
        monkeypatch.setattr(session_api, name, _towards_store(name, store))
    monkeypatch.setattr(PultState, "journal", lambda self, max_records=None: journal)
    monkeypatch.setattr(PultState, "runtime", lambda self: runtime)
    monkeypatch.setattr(PultState, "console_client", lambda self: console)
    monkeypatch.setattr(PultState, "ensure_logging", lambda self, session_id=None: artifacts)
    monkeypatch.setattr(PultState, "sets_library", lambda self, refresh=False: library)
    monkeypatch.setattr(PultState, "save_sets_library", lambda self, value: tmp_path / "sets.json")

    server = serve(PultApp(state=PultState(), web_dir=WEB_DIR), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def api(base: str, route: str, payload: dict[str, Any] | None = None) -> Any:
    """Вызов API пульта по HTTP: JSON-конверт разворачивается в `data`."""
    request = urllib.request.Request(
        f"{base}{route}",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request) as response:
        body = json.loads(response.read().decode("utf-8"))
    assert body["ok"] is True, body
    return body["data"]


def text_of(base: str, route: str) -> str:
    """Текстовый ответ: HTML-страница интерфейса или CSV-выгрузка протокола."""
    with urllib.request.urlopen(f"{base}{route}") as response:
        return response.read().decode("utf-8")


def test_vertical_slice_through_live_server(pult: str) -> None:
    """Процесс испытаний проходит через пульт: от сессии до протокола и выгрузки."""
    with urllib.request.urlopen(f"{pult}/") as page:
        assert page.headers["Content-Type"].startswith("text/html")
        assert page.headers["Content-Disposition"] is None, (
            "страница интерфейса должна открываться в браузере, а не скачиваться"
        )
        assert "Пульт испытаний" in page.read().decode("utf-8")

    state = api(pult, "/api/state")
    assert state["stand"]["configured"] is True
    assert state["session"]["id"] == ""
    assert state["phase"]["code"] == "Ф0"

    created = api(
        pult,
        "/api/sessions",
        {"info": {"title": "Приёмка ТС", "object_of_test": "Тренажёр", "operator_fio": AUTHOR}},
    )
    session_id = created["id"]
    started = api(pult, f"/api/sessions/{session_id}/start", {})
    assert started["session"]["status"] == "идёт"
    assert started["snapshots"]["taken"]["start"] is True
    assert started["stand"]["build"] == "dev@83319ae1234"

    catalog = api(pult, "/api/catalog?q=TC-SYS")
    assert catalog["classes"], "фильтр классов приходит с сервера (справочник живёт в Python)"
    picked = [str(row["check_id"]) for row in catalog["rows"]][:2]
    assert picked, "каталог должен отдавать проверки раздела «Система»"

    created = api(
        pult,
        "/api/sets",
        {
            "action": "save",
            "title": "Смоук-срез",
            "section": "TC-SYS",
            "check_ids": picked,
            "mandatory": True,
        },
    )
    new_set = next(item for item in created["library"]["rows"] if item["title"] == "Смоук-срез")
    assert new_set["checks"] == len(picked)
    assert new_set["mandatory"] == len(picked), "массовый выбор ставит признак «обязательная»"
    assert new_set["items"][0]["title"], "состав набора приходит с названиями проверок"

    approved_set = api(
        pult, "/api/sets", {"action": "approve", "set_id": new_set["set_id"], "comment": "к работе"}
    )
    stored_set = next(
        item for item in approved_set["library"]["rows"] if item["set_id"] == new_set["set_id"]
    )
    assert stored_set["approved"] is True

    rebuilt = api(pult, "/api/programme", {"action": "rebuild", "set_ids": [new_set["set_id"]]})
    programme = rebuilt["programme"]
    assert programme["size"] == len(picked)
    assert programme["coverage"], "покрытие разделов считает сервер"
    assert programme["rows"][0]["sources"], "у пункта программы видно происхождение"

    approved = api(
        pult,
        "/api/programme",
        {
            "action": "approve",
            "comment": "к прогону",
            "reduction": "смоук-минимум: остальные разделы вне среза приёмки",
        },
    )
    assert approved["programme"]["approved"] is True

    run = api(pult, "/api/run")
    assert run["next"]["runnable"] is True
    assert run["next"]["check_id"]
    assert run["progress"]["total"] == len(run["rows"])

    result = api(pult, "/api/run", {"action": "next"})
    outcome = result["outcomes"][0]
    assert outcome["check_id"] == run["next"]["check_id"]
    assert outcome["journal"], "выполненный пункт должен ссылаться на журнал"

    protocol = api(pult, "/api/protocol")
    assert protocol["summary"]["closed"] >= 1
    row = next(item for item in protocol["rows"] if item["check_id"] == outcome["check_id"])
    assert row["in_programme"] is True and row["journal"]
    assert text_of(pult, "/api/protocol?format=csv").startswith("№;Проверка")

    journal = api(pult, "/api/journal")
    assert journal["rows"] and "curl -X" in journal["rows"][-1]["curl"]

    readiness = api(pult, "/api/report/readiness")
    assert isinstance(readiness["warnings"], list)
    assert api(pult, "/api/run")["progress"]["pending"] == 0, "незакрытых пунктов после прогона нет"
    assert api(pult, "/api/state")["phase"]["code"] == "Ф2", (
        "прогон пройден целиком: фаза — оценка и фиксация, снимка «Окончание» ещё нет"
    )
