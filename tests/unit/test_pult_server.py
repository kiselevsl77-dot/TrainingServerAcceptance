"""Тесты тонкого API пульта: конверт ответа, маршруты, статика и вертикальный срез.

Стенд подменён `httpx.MockTransport`, сессии пишутся во временный каталог, библиотека
наборов собирается из каталога проверок: тесты не зависят от живого стенда и не трогают
`acceptance_data/`.

Проверяется то, что ломается незаметно:

* единый конверт `{ok, data}` / `{ok, error}` и код ответа (§4 ТЗ);
* маршруты и «нет сессии» → понятная ошибка, а не пустой экран;
* защита статики от выхода за `pult/web`;
* вертикальный срез через API: сессия → программа → утверждение → «▶ Следующая» →
  протокол с диапазоном журнала.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.config import PultConfig
from acceptance.http_log import HttpExchange, Journal, LoggingTransport
from acceptance.logging_setup import setup_logging
from client.http import ApiHttpClient
from client.schemas import FileMetadataResponse
from client.settings import TrainingServerSettings
from pult.server import ApiError, PultApp, Raw, compile_route, serve
from pult.state import PultState, Runtime

BASE_URL = "http://test.local"
AUTHOR = "Петров П.П."
WEB_DIR = Path(__file__).resolve().parents[2] / "pult" / "web"


def _handler(request: httpx.Request) -> httpx.Response:
    """Подменённый стенд: отвечает теми же формами, что испытуемый сервер.

    Пустые реестры вместо выдуманных данных: тест проверяет пульт, а не объём стенда.
    """
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


def _broken_handler(request: httpx.Request) -> httpx.Response:
    """Стенд отвечает не по схеме: пульт обязан показать ошибку, а не упасть."""
    return httpx.Response(200, json={"status": "ok"})


def _runtime(handler: Any = _handler, journal: Journal | None = None) -> Runtime:
    """Runtime стенда с подменённым транспортом и настоящим журналом обмена."""
    log = journal or Journal(max_records=50)
    client = ApiHttpClient(
        base_url=BASE_URL,
        timeout=5.0,
        transport=LoggingTransport(log, inner=httpx.MockTransport(handler), body_limit=200),
    )
    return Runtime(
        config=PultConfig(body_limit=200, log_bodies=True, journal_max=50),
        settings=TrainingServerSettings(base_url=BASE_URL, timeout=5.0),
        journal=log,
        client=client,
        apis=Apis.build(client),
    )


def _towards_store(name: str, store: Path) -> Any:
    """Обёртка функции сессии, работающей во временном каталоге.

    Каталог подставляется только тогда, когда вызывающий его не назвал: `list_sessions`
    сам зовёт `load_session` с готовым каталогом, и второй `directory` сломал бы вызов.
    """
    original = getattr(session_api, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if len(args) > 1 or "directory" in kwargs:
            return original(*args, **kwargs)
        return original(*args, directory=store, **kwargs)

    return wrapped


def _logging(tmp_path: Path, session_id: str) -> Any:
    """Настройка журналирования во временный каталог (журнал сессии — файл, `FR-T5`)."""
    return setup_logging(
        level="DEBUG",
        session_id=session_id,
        app_log=tmp_path / "app.log",
        session_log=tmp_path / "session.jsonl",
        console=False,
        enqueue=False,
    )


@pytest.fixture
def pult(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PultApp:
    """Пульт с подменённым стендом, временным каталогом сессий и готовыми наборами."""
    return _app(tmp_path, monkeypatch, _handler)


@pytest.fixture
def broken_stand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PultApp:
    """Пульт со стендом, отвечающим не по схеме: проверяется показ ошибки (`FR-P-25`)."""
    return _app(tmp_path, monkeypatch, _broken_handler)


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: Any) -> PultApp:
    """Собирает приложение пульта: подменённый стенд, временные сессии, свои наборы."""
    store = tmp_path / "sessions"
    store.mkdir()
    library = sets_api.library_from_catalog(author=AUTHOR)
    journal = Journal(max_records=50)
    runtime = _runtime(handler, journal)
    # Консоль запросов работает «сырым» httpx-клиентом (`FR-P-59`): в тесте его
    # транспорт тоже подменён, иначе повтор вызова ушёл бы в настоящую сеть.
    console = httpx.Client(
        base_url=BASE_URL,
        timeout=5.0,
        transport=LoggingTransport(journal, inner=httpx.MockTransport(handler), body_limit=200),
    )
    artifacts = _logging(tmp_path, "pult")
    for name in ("save_session", "load_session", "session_exists", "list_sessions"):
        monkeypatch.setattr(session_api, name, _towards_store(name, store))
    # Журнал обмена — один на процесс: лента и повтор вызова должны видеть одни записи.
    monkeypatch.setattr(PultState, "journal", lambda self, max_records=None: journal)
    monkeypatch.setattr(PultState, "runtime", lambda self: runtime)
    monkeypatch.setattr(PultState, "console_client", lambda self: console)
    monkeypatch.setattr(PultState, "ensure_logging", lambda self, session_id=None: artifacts)
    monkeypatch.setattr(PultState, "sets_library", lambda self, refresh=False: library)
    monkeypatch.setattr(PultState, "save_sets_library", lambda self, value: tmp_path / "sets.json")
    return PultApp(state=PultState(), web_dir=WEB_DIR)


@pytest.fixture
def no_stand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PultApp:
    """Пульт без адреса стенда: проверяются состояния «стенд не задан»."""
    store = tmp_path / "sessions"
    store.mkdir()
    for name in ("save_session", "load_session", "session_exists", "list_sessions"):
        monkeypatch.setattr(session_api, name, _towards_store(name, store))
    monkeypatch.setattr(PultState, "runtime", lambda self: None)
    monkeypatch.setattr(
        PultState,
        "ensure_logging",
        lambda self, session_id=None: _logging(tmp_path, session_id or "pult"),
    )
    monkeypatch.setattr(
        PultState, "sets_library", lambda self, refresh=False: sets_api.library_from_catalog()
    )
    return PultApp(state=PultState(), web_dir=WEB_DIR)


def get(app: PultApp, route: str, **query: str) -> Any:
    """`GET` через приложение (без сети) — как это делает браузер."""
    return app.dispatch("GET", route, query, {})


def post(app: PultApp, route: str, **payload: Any) -> Any:
    """`POST` через приложение (без сети)."""
    return app.dispatch("POST", route, {}, payload)


# ---------------------------------------------------------------------------
# Маршруты, конверт ответа и статика
# ---------------------------------------------------------------------------
def test_compile_route_extracts_identifiers() -> None:
    """Шаблон маршрута даёт именованную группу с идентификатором проверки."""
    pattern = compile_route("/api/checks/{check_id}")
    match = pattern.match("/api/checks/TC-SYS-01")
    assert match is not None
    assert match.group("check_id") == "TC-SYS-01"
    assert pattern.match("/api/checks/TC-SYS-01/extra") is None


def test_unknown_route_and_method_are_reported(pult: PultApp) -> None:
    """Неизвестный путь и неподходящий метод — ошибка `http`, а не молчаливый 200."""
    with pytest.raises(ApiError) as unknown:
        get(pult, "/api/nothing")
    assert unknown.value.status == 404
    assert unknown.value.kind == "http"
    with pytest.raises(ApiError) as wrong:
        post(pult, "/api/state")
    assert wrong.value.status == 404


def test_static_serves_interface_and_blocks_traversal(pult: PultApp) -> None:
    """Страница пульта отдаётся для показа, а выход за `pult/web` запрещён."""
    index = get(pult, "/")
    assert isinstance(index, Raw)
    assert "Пульт испытаний" in index.text
    assert index.content_type.startswith("text/html")
    assert index.download is False, "страницу отдаём для показа, а не вложением"
    assert index.filename == ""
    script = get(pult, "/app.js")
    assert isinstance(script, Raw)
    assert script.content_type.startswith("text/javascript")
    assert script.download is False
    with pytest.raises(ApiError) as escape:
        get(pult, "/../server.py")
    assert escape.value.status in (403, 404)


def test_state_without_session_names_the_first_step(no_stand: PultApp) -> None:
    """Шапка без сессии и стенда честно говорит, чего не хватает (`IR-P-8`)."""
    data = get(no_stand, "/api/state")
    assert data["stand"]["configured"] is False
    assert data["session"]["id"] == ""
    assert data["phase"]["code"] == "Ф0"
    assert data["next"]["runnable"] is False
    assert data["next"]["reason"] == "Сессия испытаний не выбрана"


def test_run_without_session_returns_empty_view(no_stand: PultApp) -> None:
    """Прогон без сессии отдаёт пустой вид с причиной, а не ошибку сервера."""
    data = get(no_stand, "/api/run")
    assert data["rows"] == []
    assert data["next"]["runnable"] is False
    assert data["next"]["reason"] == "Сессия испытаний не выбрана"


def test_protocol_without_session_is_validation_error(no_stand: PultApp) -> None:
    """Протокол без сессии — понятная ошибка 409 (`IR-P-8`), а не пустая таблица."""
    with pytest.raises(ApiError) as failure:
        get(no_stand, "/api/protocol")
    assert failure.value.status == 409
    assert "Сессия испытаний не выбрана" == failure.value.message


def test_session_create_requires_core_fields(no_stand: PultApp) -> None:
    """Сессию нельзя создать без наименования и объекта испытаний (`FR-P-3`)."""
    with pytest.raises(ApiError) as failure:
        post(no_stand, "/api/sessions", info={"title": "Т-1"})
    assert failure.value.status == 422
    assert "объект испытаний" in failure.value.message


def test_session_create_select_and_start(pult: PultApp) -> None:
    """Сессия создаётся, выбирается, стартует и попадает в список (`FR-P-3`, `FR-P-4`)."""
    created = post(
        pult,
        "/api/sessions",
        info={"title": "Испытания ТС", "object_of_test": "Тренажёр", "operator_fio": AUTHOR},
    )
    assert created["status"] == "черновик"
    assert created["filled"] is True

    listed = get(pult, "/api/sessions")
    assert listed["current"] == created["id"]
    assert [row["id"] for row in listed["sessions"]] == [created["id"]]
    assert listed["sessions"][0]["current"] is True

    selected = post(pult, "/api/sessions", action="select", session_id=created["id"])
    assert selected["session"]["id"] == created["id"]

    started = post(pult, f"/api/sessions/{created['id']}/start")
    assert started["session"]["status"] == "идёт"
    assert started["stand"]["build"] == "dev@83319ae1234"
    assert started["snapshots"]["taken"]["start"] is True
    assert started["problem"] == ""
    assert get(pult, "/api/state")["session"]["status"] == "идёт"


def test_start_without_stand_is_refused(no_stand: PultApp) -> None:
    """Без адреса стенда сессию начать нельзя: снимок «Начало» не снять (`DR-P-8`)."""
    created = post(
        no_stand,
        "/api/sessions",
        info={"title": "Т-1", "object_of_test": "Объект"},
    )
    with pytest.raises(ApiError) as failure:
        post(no_stand, f"/api/sessions/{created['id']}/start")
    assert failure.value.status == 409
    assert "Адрес испытуемого сервера не задан" in failure.value.message


# ---------------------------------------------------------------------------
# Вертикальный срез и лента обмена
# ---------------------------------------------------------------------------
def _ready_session(pult: PultApp) -> tuple[str, str]:
    """Готовит сессию с утверждённой программой: возвращает `(id сессии, id проверки)`."""
    created = post(
        pult,
        "/api/sessions",
        info={"title": "Срез", "object_of_test": "Объект", "operator_fio": AUTHOR},
    )
    session_id = str(created["id"])
    post(pult, f"/api/sessions/{session_id}/start")
    rows = get(pult, "/api/sets")["rows"]
    assert rows, "библиотека наборов должна собираться из каталога проверок"
    post(pult, "/api/programme", action="rebuild", set_ids=[str(rows[0]["set_id"])])
    approved = post(pult, "/api/programme", action="approve", comment="ревизия испытаний")
    assert approved["programme"]["approved"] is True
    run = get(pult, "/api/run")
    assert run["next"]["check_id"], "в утверждённой программе должна быть следующая проверка"
    return session_id, str(run["next"]["check_id"])


def test_vertical_slice_through_api(pult: PultApp) -> None:
    """Срез через API: сессия → наборы → программа → утверждение → «Следующая» → протокол."""
    session_id, check_id = _ready_session(pult)
    assert session_id

    run = get(pult, "/api/run")
    assert run["revision"] >= 1
    assert run["progress"]["total"] == len(run["rows"])
    assert run["rows"][0]["marker"]
    assert "Следующая" in run["next"]["label"]

    result = post(pult, "/api/run", action="next")
    assert len(result["outcomes"]) == 1
    outcome = result["outcomes"][0]
    assert outcome["check_id"] == check_id
    assert outcome["journal"], "у выполненного пункта должен быть диапазон журнала"

    protocol = get(pult, "/api/protocol")
    assert protocol["summary"]["programme_size"] == run["progress"]["total"]
    assert protocol["summary"]["closed"] >= 1
    row = next(item for item in protocol["rows"] if item["check_id"] == check_id)
    assert row["status"] != "не выполнена"
    assert row["in_programme"] is True
    assert row["journal"]

    csv = get(pult, "/api/protocol", format="csv")
    assert isinstance(csv, Raw)
    assert csv.text.startswith("№;Проверка")
    assert check_id in csv.text
    assert csv.filename == "protocol.csv"
    assert csv.download is True, "выгрузка протокола — файл на скачивание"
    assert csv.content_type.startswith("text/csv")

    journal = get(pult, "/api/journal")
    assert journal["rows"]
    assert "curl -X" in journal["rows"][-1]["curl"]
    assert journal["summary"]["total"] == journal["total"]


def test_check_suspend_resume_and_annotate(pult: PultApp) -> None:
    """Снятие пункта требует причины, возврат и отметка оператора видны в прогоне."""
    _session_id, check_id = _ready_session(pult)

    with pytest.raises(ApiError) as missing_reason:
        post(pult, f"/api/checks/{check_id}", action="suspend", reason="")
    assert missing_reason.value.status == 422

    suspended = post(pult, f"/api/checks/{check_id}", action="suspend", reason="стенд занят")
    row = next(item for item in suspended["run"]["rows"] if item["check_id"] == check_id)
    assert row["state"] == "снята"
    assert row["reason"] == "стенд занят"

    resumed = post(pult, f"/api/checks/{check_id}", action="resume")
    row = next(item for item in resumed["run"]["rows"] if item["check_id"] == check_id)
    assert row["state"] != "снята"

    annotated = post(
        pult,
        f"/api/checks/{check_id}",
        action="annotate",
        values={"status": "успех", "verdict": "проверено вручную"},
    )
    row = next(item for item in annotated["run"]["rows"] if item["check_id"] == check_id)
    assert row["status"] == "успех"
    assert row["origin"] == "оператор"


def test_run_card_must_be_filled(pult: PultApp) -> None:
    """Карточка запуска не принимается недозаполненной (`FR-P-19`)."""
    _session_id, check_id = _ready_session(pult)
    with pytest.raises(ApiError) as incomplete:
        post(pult, f"/api/checks/{check_id}", action="run-card", values={"goal": "проверить"})
    assert incomplete.value.status == 422
    assert "Карточка запуска заполнена не полностью" in incomplete.value.message

    saved = post(
        pult,
        f"/api/checks/{check_id}",
        action="run-card",
        values={"goal": "проверить", "data": "__TEST__", "responsible": AUTHOR, "confirm": True},
    )
    assert saved["card"]["filled"] is True
    assert saved["card"]["author"] == AUTHOR


def test_journal_repeat_appends_record(pult: PultApp) -> None:
    """Повтор вызова добавляет запись в ленту и отдаёт её строку с `curl` (`FR-P-24`)."""
    with pytest.raises(ApiError) as bad_path:
        post(pult, "/api/journal/repeat", method="GET", path="version")
    assert bad_path.value.status == 422

    data = post(pult, "/api/journal/repeat", method="GET", path="/version", label="проверка версии")
    assert data["status"] == 200
    assert data["row"]["method"] == "GET"
    assert data["row"]["label"] == "проверка версии"
    assert data["row"]["curl"].startswith("curl -X GET")
    assert data["row"]["response"]


def test_journal_rows_explain_empty_bodies(pult: PultApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """Через API: пустое тело объясняется словами, выключатель тел виден в ответе (`FR-P-21`)."""
    data = post(pult, "/api/journal/repeat", method="GET", path="/version", label="проверка версии")
    assert data["row"]["request"] == ""
    assert data["row"]["request_note"] == "тела нет: запрос без тела (GET/HEAD)"
    assert data["row"]["response"] and data["row"]["response_note"] == ""

    lenta = get(pult, "/api/journal")
    assert lenta["bodies_logged"] is True

    # Журналирование тел выключено: тело у стенда было, в ленту не попало.
    runtime = pult.state.runtime()
    assert runtime is not None
    silent = replace(runtime, config=replace(runtime.config, log_bodies=False))
    monkeypatch.setattr(PultState, "runtime", lambda self: silent)
    journal = pult.state.journal()
    journal.add(
        HttpExchange(
            seq=journal.next_seq(),
            started_at="2026-09-29T12:00:00",
            method="post",
            path="/api/data/files",
            query="",
            status=200,
            duration_ms=5.0,
            request_bytes=128,
            response_bytes=64,
            content_type="application/json",
            label="загрузка",
            request_body="",
            response_body="",
        )
    )
    off = get(pult, "/api/journal")
    assert off["bodies_logged"] is False
    assert "журналирование тел выключено" in off["rows"][-1]["request_note"]


def test_app_js_declares_functions_at_top_level(pult: PultApp) -> None:
    """Скрипт страницы: все функции объявлены на верхнем уровне (`IR-P-4`).

    Регресс-страж: вложенная функция не видна обработчикам, и страница «Результаты»
    упала с «refreshResults is not defined», хотя `node --check` молчал, а HTTP-тесты
    (они не исполняют JS) этого не видели. Поймала живая проверка браузером.
    """
    script = get(pult, "/app.js").text
    nested = [
        line.strip() for line in script.splitlines() if re.match(r"\s+(?:async )?function ", line)
    ]
    assert nested == [], f"вложенные функции в app.js: {nested[:3]}"
    for name in ("refreshResults", "renderResults", "renderProtocol", "renderJournal"):
        assert f"function {name}(" in script


def test_results_page_combines_three_zones(pult: PultApp) -> None:
    """`GET /api/results` отдаёт данные стенда, протокол и готовность отчёта (`SCR-401`)."""
    with pytest.raises(ApiError) as no_session:
        get(pult, "/api/results")
    assert no_session.value.status == 409, "без сессии страница результатов не рисуется"

    _session_id, check_id = _ready_session(pult)
    data = get(pult, "/api/results")
    assert data["session"]["id"] and data["session"]["can_finish"] is True
    assert [item["label"] for item in data["data_summary"]][:2] == ["файлов", "записей RAW+markup"]
    assert data["data"]["files"] == [] and data["data"]["errors"]["files"] == ""
    assert data["protocol"]["rows"]
    statuses = {item["value"] for item in data["protocol"]["statuses"]}
    assert statuses >= {"отказ"}, "фильтр результатов приходит значениями и подписями"
    assert all(item["label"] for item in data["protocol"]["statuses"])
    assert all(item["value"] for item in data["protocol"]["sections"])
    assert [item["label"] for item in data["report"]["points"]][0] == "Реквизиты сессии"
    assert data["snapshots"]["taken"]["start"] is True
    assert data["snapshots"]["taken"]["end"] is False

    # Фильтр протокола приходит вместе с данными: интерфейс не хранит выбор сам.
    filtered = get(pult, "/api/results", q=check_id)
    assert all(check_id in row["check_id"] for row in filtered["protocol"]["rows"])
    assert filtered["protocol"]["query"] == check_id


def test_data_zone_has_sections_counters_and_details(pult: PultApp) -> None:
    """Подразделы «Данных стенда»: счётчики из Python, `$Модели` и свёрнутая разметка.

    Список в свёрнутом `<details>` обязан показывать число сущностей реестра
    (`llm_tasks/49`, вопросы 2 = а и 4 = а), поэтому счётчики приходят в ответе API,
    а не досчитываются браузером; состояние сворачивания живёт в DOM (`open`).
    """
    _ready_session(pult)
    data = get(pult, "/api/data")

    assert [item["key"] for item in data["sections"]] == [
        "files",
        "records",
        "loads",
        "datasets",
        "models",
    ]
    assert [item["label"] for item in data["sections"]] == [
        "$Файлы · 0",
        "Записи RAW + markup · 0",
        "$Нагрузки · 0",
        "$Датасеты и состав · 0",
        "$Модели · 0",
    ]
    assert data["counts"] == {"files": 0, "records": 0, "loads": 0, "datasets": 0, "models": 0}
    assert data["pages"]["files"]["label"] == "показано 0 из 0"
    assert data["limit"] == 200, "лимит строк по умолчанию — 200"
    assert data["models"] == [] and data["errors"]["models"] == ""

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    details = re.findall(r'<details class="zone__sub"[^>]*>', html)
    assert len(details) == 5, "каждый подраздел — отдельный <details>"
    assert not any(" open" in tag for tag in details), "подразделы свёрнуты по умолчанию"
    assert 'id="results-sub-models"' in html and '<summary id="results-files-count">' in html


def test_data_lists_are_paged_with_counter(pult: PultApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """Файлы обрезаются лимитом 200, счётчик говорит «показано N из M», `?limit=0` — все.

    Прежний лимит 300 стоял в коде без счётчика: оператор считал список полным
    (`llm_tasks/49`, вопрос 6 = а).
    """
    files = [
        FileMetadataResponse(
            id=uuid4(),
            file_name=f"RAW_{number}.csv",
            size=100,
            s3_path=f"/raw/{number}",
            import_date=datetime(2026, 9, 20, 9, 0, 0),
            file_type="RAW",
        )
        for number in range(250)
    ]
    monkeypatch.setattr(PultState, "files", lambda self, refresh=False: (files, None))
    _ready_session(pult)

    data = get(pult, "/api/data")
    assert len(data["files"]) == 200
    assert data["counts"]["files"] == 250
    assert data["pages"]["files"] == {
        "shown": 200,
        "total": 250,
        "limited": True,
        "label": "показано 200 из 250",
    }
    assert data["sections"][0]["can_show_all"] is True

    everything = get(pult, "/api/data", limit="0")
    assert len(everything["files"]) == 250
    assert everything["pages"]["files"]["limited"] is False
    assert everything["sections"][0]["can_show_all"] is False

    with pytest.raises(ApiError) as bad_limit:
        get(pult, "/api/data", limit="много")
    assert bad_limit.value.status == 422, "неверный лимит не роняет страницу"


def test_session_finish_requires_conclusion(pult: PultApp) -> None:
    """Завершение сессии без итогового решения отказывает: отчёт был бы неполон (`FR-P-48`)."""
    session_id, _check = _ready_session(pult)
    with pytest.raises(ApiError) as missing:
        post(pult, f"/api/sessions/{session_id}/finish", close=True, conclusion="  ")
    assert missing.value.status == 422

    data = post(
        pult, f"/api/sessions/{session_id}/finish", close=True, conclusion="годен с замечаниями"
    )
    assert data["closed"] is True
    assert data["session"]["status"] == "завершена"
    assert data["snapshots"]["taken"]["end"] is True, "снимок «Окончание» снимает завершение"
    assert data["readiness"]["points"], "готовность отчёта приходит пунктами"


def _with_checks(pult: PultApp, *, check_class: str, check_id: str = "") -> str:
    """Сессия с утверждённой программой из проверок одного класса: id проверки.

    `check_id` задаётся явно там, где нужна конкретная проверка: например, ручная
    `TC-SYS-06` подтверждается чтением `/version` при старте сессии (схема v8,
    `UC-03`), поэтому для настоящей ручной отметки берётся `TC-TASK-08`.
    """
    created = post(
        pult,
        "/api/sessions",
        info={"title": "Класс", "object_of_test": "Объект", "operator_fio": AUTHOR},
    )
    post(pult, f"/api/sessions/{created['id']}/start")
    if not check_id:
        every = get(pult, "/api/catalog")["rows"]
        rows = [row for row in every if row["class_key"] == check_class]
        assert rows, f"в каталоге нет проверок класса {check_class}"
        check_id = str(rows[0]["check_id"])
    title = f"Класс {check_class}"
    saved = post(
        pult,
        "/api/sets",
        action="save",
        title=title,
        check_ids=[check_id],
        mandatory=True,
    )
    set_row = next(item for item in saved["library"]["rows"] if item["title"] == title)
    post(pult, "/api/programme", action="rebuild", set_ids=[str(set_row["set_id"])])
    approved = post(
        pult,
        "/api/programme",
        action="approve",
        comment="приёмка класса",
        reduction="срез по классу",
    )
    assert approved["programme"]["approved"] is True
    return check_id


def test_run_refuses_programme_that_is_not_approved(pult: PultApp) -> None:
    """Черновик программы не запускается: причина с сервера и отказ API — одна (`FR-P-13`)."""
    created = post(
        pult,
        "/api/sessions",
        info={"title": "Черновик", "object_of_test": "Объект", "operator_fio": AUTHOR},
    )
    post(pult, f"/api/sessions/{created['id']}/start")
    rows = get(pult, "/api/sets")["rows"]
    post(pult, "/api/programme", action="rebuild", set_ids=[str(rows[0]["set_id"])])

    run = get(pult, "/api/run")
    assert run["next"]["runnable"] is False
    assert run["readiness"]["blocked_by"] == "not-approved"
    assert run["next"]["fix"]["panel"] == "planning"
    assert "не утверждена" in run["next"]["reason"]

    with pytest.raises(ApiError) as refused:
        post(pult, "/api/run", action="next")
    assert refused.value.status == 409
    assert refused.value.message == run["next"]["reason"], "страница и API объясняют одно и то же"

    assert get(pult, "/api/state")["next"]["reason"] == run["next"]["reason"]


def test_run_mode_switch_is_saved_in_session(pult: PultApp) -> None:
    """«Только невыполненное / всё подряд» сохраняется в сессии и меняет очередь (`UC-09`)."""
    _ready_session(pult)
    post(pult, "/api/run", action="next")
    only = get(pult, "/api/run")
    assert only["progress"]["pending"] < only["progress"]["total"]

    all_mode = post(pult, "/api/run", action="mode", mode="all")
    assert all_mode["mode"] == "all" and all_mode["skip_done"] is False
    assert all_mode["progress"]["pending"] == all_mode["progress"]["total"]
    assert get(pult, "/api/run")["mode"] == "all", "режим переживает следующий запрос"

    back = post(pult, "/api/run", action="mode", mode="only-remaining")
    assert back["mode"] == "only-remaining" and back["skip_done"] is True
    assert back["progress"]["pending"] < back["progress"]["total"]

    assert post(pult, "/api/run", action="mode", mode="чепуха")["mode"] == "only-remaining"
    assert [item["value"] for item in back["modes"]["options"]] == ["only-remaining", "all"]


def test_catalog_lists_all_programme_checks_with_run_modes(pult: PultApp) -> None:
    """`GET /api/catalog` — все 69 проверок программы и способ выполнения каждой (`M0`)."""
    data = get(pult, "/api/catalog")
    rows = data["rows"]

    assert data["summary"]["checks_implemented"] == 69
    assert data["summary"]["groups_implemented"] == 10
    assert len(rows) == 69
    assert all(row["scenario"] for row in rows), "у каждой строки — способ выполнения"
    modes = {row["scenario"] for row in rows}
    assert modes <= {"сценарием", "по вызовам", "вручную"}
    assert "сценарием" in modes and "вручную" in modes
    fresh = {row["check_id"]: row for row in rows if row["check_id"].startswith("TC-MOD-")}
    assert fresh["TC-MOD-01"]["scenario"] == "сценарием", "сценарий T6 описан"
    assert fresh["TC-MOD-01"]["class_key"] == "tech"
    assert fresh["TC-MOD-02"]["confirm_required"] is True, "live-проверка требует карточку"
    clean = {row["check_id"]: row for row in rows if row["check_id"].startswith("TC-CLEAN-")}
    assert clean["TC-CLEAN-04"]["scenario"] == "вручную", "ручную проверку закрывает оператор"


def _without_scenario(monkeypatch: pytest.MonkeyPatch, check_id: str) -> None:
    """Показывает проверку как «сценарий ещё не описан» (состояние этапа M0).

    Сценарии групп T6–T9 описываются партиями, поэтому правило «проверка каталога без
    сценария не закрывается статусом» проверяется подменой описания.
    """
    original = catalog.find

    def find(value: str) -> Any:
        spec = original(value)
        if spec is not None and spec.check_id == check_id:
            return replace(spec, automation=None)
        return spec

    monkeypatch.setattr(catalog, "find", find)


def test_run_warns_and_refuses_check_without_scenario(
    pult: PultApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка без сценария (M0): предупреждение до нажатия и отказ без статуса.

    Отказ ядра не должен выглядеть как «проверка пропущена»: пункт остаётся
    следующим, результат в протоколе — «не выполнена», а оператор получает
    подсказку с доступными способами выполнения.
    """
    check_id = _with_checks(pult, check_class="tech", check_id="TC-MOD-01")
    _without_scenario(monkeypatch, check_id)

    run = get(pult, "/api/run")
    assert run["next"]["check_id"] == check_id
    assert "сценарий проверки ещё не описан" in run["next"]["notice"]
    assert run["next"]["item"]["scenario"] == "по вызовам"
    assert run["next"]["item"]["steps"], "операции проверки видны сразу"

    result = post(pult, "/api/run", action="next")
    outcome = result["outcomes"][0]
    assert outcome["check_id"] == check_id
    assert outcome["stopped"] is True
    assert outcome["scenario_missing"] is True
    assert "«по вызовам»" in outcome["hint"]
    assert not outcome["journal"], "на стенд ничего не ушло"

    protocol = get(pult, "/api/protocol")
    row = next(item for item in protocol["rows"] if item["check_id"] == check_id)
    assert row["status"] == "не выполнена", "статус «пропущена» проверке не ставится"
    assert get(pult, "/api/run")["next"]["check_id"] == check_id, "пункт остаётся следующим"


def test_pages_show_run_modes_of_checks(pult: PultApp) -> None:
    """Страницы несут способ выполнения проверки: колонка «Прогон» и предупреждение."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    catalog_body = script[
        script.index("function renderCatalog(") : script.index("function renderSets(")
    ]

    assert "<th>Прогон</th>" in html
    assert "row.scenario" in catalog_body, "колонка «Прогон» рисуется из view-модели"
    assert 'document.getElementById("run-notice")' in script, "предупреждение прогона на месте"


def test_run_view_describes_next_item_and_tools(pult: PultApp) -> None:
    """Вид прогона несёт странице всё: пункт, шаги, payload, формы и «Пачку tech» (`FR-P-20`)."""
    _ready_session(pult)
    run = get(pult, "/api/run")
    item = run["next"]["item"]
    assert item["check_id"] == run["next"]["check_id"]
    assert item["title"] and item["class_label"] and item["section"]
    assert item["steps"], "шаги проверки приходят готовыми"
    assert "summary" in item["payload"]
    assert item["card"]["required"] is False, "у tech-проверки карточка запуска не нужна"
    assert item["mark"]["required"] is False
    assert run["batch"]["count"] == len(run["batch"]["ids"])
    assert run["batch"]["label"].startswith("▶▶ Пачка tech")
    assert run["queue_mode_label"], "режим исполнения очереди виден отдельно от режима прогона"
    assert run["rows"][0]["details"]["steps"], "детали строки тоже готовы"


def test_manual_mark_needs_conclusion_and_closes_item(pult: PultApp) -> None:
    """Ручная проверка закрывается отметкой с заключением; без заключения — отказ (`FR-P-32`)."""
    check_id = _with_checks(pult, check_class="manual", check_id="TC-TASK-08")

    run = get(pult, "/api/run")
    assert run["next"]["check_id"] == check_id
    assert run["next"]["item"]["mark"]["required"] is True
    assert run["next"]["item"]["mark"]["statuses"]

    outcome = post(pult, "/api/run", action="next")["outcomes"][0]
    assert outcome["operator_required"] is True
    assert outcome["hint"]
    assert outcome["journal"] == "", "при ручной проверке запрос на стенд не уходит"

    with pytest.raises(ApiError) as no_note:
        post(
            pult,
            f"/api/checks/{check_id}",
            action="mark",
            status="выполнена вручную",
            note="   ",
        )
    assert no_note.value.status == 422
    assert "заключение" in no_note.value.message

    marked = post(
        pult,
        f"/api/checks/{check_id}",
        action="mark",
        status="выполнена вручную",
        note="проверено вручную по инструкции, п. 4",
    )
    row = next(item for item in marked["run"]["rows"] if item["check_id"] == check_id)
    assert row["status"] == "выполнена вручную"
    assert row["origin"] == "оператор"
    assert "инструкции" in row["reason"]
    assert marked["run"]["progress"]["pending"] == 0, "отметка закрывает пункт программы"


def test_run_card_is_kept_and_previewed(pult: PultApp) -> None:
    """Карточка `live`: предпросмотр payload до запуска и возврат значений в форму (`FR-P-19`)."""
    check_id = _with_checks(pult, check_class="live")

    run = get(pult, "/api/run")
    card = run["next"]["item"]["card"]
    assert card["required"] is True
    assert card["filled"] is False and card["missing"]
    assert run["next"]["item"]["payload"]["count"] >= 1, "видно, какие `__TEST__`-данные уйдут"
    assert run["card_ready"] is False

    with pytest.raises(ApiError) as incomplete:
        post(pult, f"/api/checks/{check_id}", action="run-card", values={"goal": "проверить"})
    assert incomplete.value.status == 422

    post(
        pult,
        f"/api/checks/{check_id}",
        action="run-card",
        values={"goal": "проверить", "data": "__TEST__", "responsible": AUTHOR, "confirm": True},
    )
    again = get(pult, "/api/run")
    assert again["card_ready"] is True
    assert again["next"]["item"]["card"]["filled"] is True
    assert again["next"]["item"]["card"]["saved"] is True
    confirm = next(
        field for field in again["next"]["item"]["card"]["fields"] if field["name"] == "confirm"
    )
    assert confirm["checked"] is True

    outcome = post(pult, "/api/run", action="next")["outcomes"][0]
    assert outcome["confirmation_required"] is False, "с карточкой запуск не блокируется"


def test_data_reports_registry_errors_instead_of_crashing(broken_stand: PultApp) -> None:
    """Ответ стенда не по схеме — ошибка видна в разделе `errors`, пульт не падает."""
    data = get(broken_stand, "/api/data")
    assert data["files"] == []
    assert data["errors"]["files"]
    assert data["errors"]["datasets"]


def test_data_reads_empty_registries(pult: PultApp) -> None:
    """Пустые реестры стенда читаются штатно: ошибок нет, разделы пустые (`SCR-204`)."""
    data = get(pult, "/api/data")
    assert data["files"] == []
    assert data["records"] == []
    assert data["loads"] == []
    assert data["datasets"] == []
    assert set(data["errors"].values()) == {""}


# ---------------------------------------------------------------------------
# Планирование: наборы (массовый выбор, порядок, ревизии) и программа
# ---------------------------------------------------------------------------
def _set_row(payload: Any, *, set_id: str = "", title: str = "") -> Any:
    """Строка набора из ответа `POST /api/sets`: по идентификатору или по названию."""
    rows = payload["library"]["rows"]
    if set_id:
        return next(item for item in rows if item["set_id"] == set_id)
    return next(item for item in rows if item["title"] == title)


def _programme(pult: PultApp) -> dict[str, Any]:
    """Вид программы текущей сессии (`GET /api/programme`)."""
    return get(pult, "/api/programme")["programme"]


def _open_session(pult: PultApp, title: str) -> str:
    """Создаёт и выбирает сессию: действия планирования пишут автора из её реквизитов."""
    created = post(
        pult,
        "/api/sessions",
        info={"title": title, "object_of_test": "Объект", "operator_fio": AUTHOR},
    )
    return str(created["id"])


def test_sets_action_edits_composition(pult: PultApp) -> None:
    """Массовый выбор, обязательность, порядок и удаление проверок набора (`UC-05`)."""
    _open_session(pult, "Наборы")
    created = post(
        pult,
        "/api/sets",
        action="save",
        title="Приёмка файлов",
        section="TC-FILE",
        check_ids=["TC-FILE-01", "TC-FILE-02"],
    )
    set_id = _set_row(created, title="Приёмка файлов")["set_id"]
    assert _set_row(created, set_id=set_id)["checks"] == 2
    assert _set_row(created, set_id=set_id)["approved"] is False

    added = post(
        pult, "/api/sets", action="save", set_id=set_id, check_ids=["TC-FILE-03"], mandatory=True
    )
    assert _set_row(added, set_id=set_id)["mandatory"] == 1

    marked = post(
        pult, "/api/sets", action="mandatory", set_id=set_id, check_id="TC-FILE-02", mandatory=True
    )
    assert _set_row(marked, set_id=set_id)["mandatory"] == 2

    moved = post(pult, "/api/sets", action="move", set_id=set_id, check_id="TC-FILE-03", delta=-1)
    assert _set_row(moved, set_id=set_id)["check_ids"] == ["TC-FILE-01", "TC-FILE-03", "TC-FILE-02"]

    reordered = post(
        pult, "/api/sets", action="reorder", set_id=set_id, check_ids=["TC-FILE-02", "TC-FILE-01"]
    )
    assert _set_row(reordered, set_id=set_id)["check_ids"] == [
        "TC-FILE-02",
        "TC-FILE-01",
        "TC-FILE-03",
    ]

    removed = post(pult, "/api/sets", action="remove", set_id=set_id, check_ids=["TC-FILE-01"])
    assert _set_row(removed, set_id=set_id)["check_ids"] == ["TC-FILE-02", "TC-FILE-03"]

    with pytest.raises(ApiError) as no_set:
        post(pult, "/api/sets", action="move", set_id="SET-NOPE", check_id="TC-FILE-01")
    assert no_set.value.status == 404
    with pytest.raises(ApiError) as unknown:
        post(pult, "/api/sets", action="сшить")
    assert unknown.value.status == 422


# ---------------------------------------------------------------------------
# Настоящий HTTP: конверт, статика и ошибки тела запроса
# ---------------------------------------------------------------------------
def test_http_layer_envelope_static_and_bad_body(pult: PultApp) -> None:
    """Через живой сервер: `{ok, data}`, HTML страницы и 400 на нечитаемом теле."""
    server = serve(pult, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)

    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/api/state") as response:
            assert response.status == 200
            body = json.loads(response.read().decode("utf-8"))
        assert body["ok"] is True
        assert body["data"]["stand"]["configured"] is True

        with urllib.request.urlopen(f"{base}/") as response:
            assert "Пульт испытаний" in response.read().decode("utf-8")

        broken = urllib.request.Request(
            f"{base}/api/sessions",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(broken)
        assert failure.value.code == 400
        error = json.loads(failure.value.read().decode("utf-8"))
        assert error["ok"] is False
        assert error["error"]["kind"] == "http"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_sets_action_approve_revise_and_restore(pult: PultApp) -> None:
    """Утверждённый набор правится только новой ревизией, архив возвращается (`FR-P-66`)."""
    _open_session(pult, "Ревизии")
    created = post(
        pult, "/api/sets", action="save", title="Смоук", section="TC-SYS", check_ids=["TC-SYS-01"]
    )
    set_id = _set_row(created, title="Смоук")["set_id"]

    approved = post(pult, "/api/sets", action="approve", set_id=set_id, comment="к работе")
    assert _set_row(approved, set_id=set_id)["approved"] is True

    with pytest.raises(ApiError) as locked:
        post(pult, "/api/sets", action="remove", set_id=set_id, check_ids=["TC-SYS-01"])
    assert locked.value.status == 409
    assert "ревизи" in locked.value.message

    revised = post(pult, "/api/sets", action="revise", set_id=set_id, comment="уточнение состава")
    assert _set_row(revised, set_id=set_id)["approved"] is False
    assert _set_row(revised, set_id=set_id)["revision"] >= 2

    emptied = post(pult, "/api/sets", action="remove", set_id=set_id, check_ids=["TC-SYS-01"])
    assert _set_row(emptied, set_id=set_id)["checks"] == 0

    restored = post(
        pult, "/api/sets", action="restore", set_id=set_id, revision=1, comment="вернуть состав"
    )
    assert _set_row(restored, set_id=set_id)["check_ids"] == ["TC-SYS-01"]

    with pytest.raises(ApiError) as no_revision:
        post(pult, "/api/sets", action="restore", set_id=set_id, revision=99)
    assert no_revision.value.status == 409


def test_static_is_served_for_rendering_not_download(pult: PultApp) -> None:
    """Страница и подресурсы уходят для показа: без `Content-Disposition`.

    Регресс-страж дефекта первого запуска: пока статика отдавалась вложением,
    браузер скачивал `index.html`, стили и скрипт не применялись, и пульт
    выглядел «мёртвой статической страницей».
    """
    server = serve(pult, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        expected = {
            "/": "text/html",
            "/app.js": "text/javascript",
            "/styles.css": "text/css",
        }
        for route, mime in expected.items():
            with urllib.request.urlopen(f"{base}{route}") as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith(mime), route
                assert response.headers["Content-Disposition"] is None, (
                    f"{route}: статика отдана вложением — браузер её скачает, а не покажет"
                )
                assert response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_programme_order_and_mandatory_are_editable_in_draft(pult: PultApp) -> None:
    """Порядок и обязательность правятся в черновике, а не в утверждённой (`FR-P-13`)."""
    _ready_session(pult)
    approved = _programme(pult)
    assert approved["approved"] is True
    order = [row["check_id"] for row in approved["rows"]]
    assert len(order) >= 2, "в программе должно быть не меньше двух пунктов"

    for payload in (
        {"action": "move", "check_id": order[-1], "delta": -1},
        {"action": "reorder", "check_ids": list(reversed(order))},
        {"action": "mandatory", "check_id": order[0], "mandatory": True},
    ):
        with pytest.raises(ApiError) as refused:
            post(pult, "/api/programme", **payload)
        assert refused.value.status == 409
        assert "ревизи" in refused.value.message
    assert [row["check_id"] for row in _programme(pult)["rows"]] == order

    revised = post(pult, "/api/programme", action="revise", comment="уточнение порядка")
    assert revised["programme"]["approved"] is False
    assert revised["programme"]["revision"] > approved["revision"]
    rows = revised["programme"]["rows"]
    mandatory_ids = [row["check_id"] for row in rows if row["mandatory"]]
    optional_ids = [row["check_id"] for row in rows if not row["mandatory"]]
    if mandatory_ids and optional_ids:
        with pytest.raises(ApiError) as boundary:
            post(pult, "/api/programme", action="move", check_id=optional_ids[0], delta=-1)
        assert boundary.value.status == 409

    reordered = post(
        pult,
        "/api/programme",
        action="reorder",
        check_ids=[*reversed(optional_ids), *reversed(mandatory_ids)],
    )
    assert [row["check_id"] for row in reordered["programme"]["rows"]] == [
        *reversed(mandatory_ids),
        *reversed(optional_ids),
    ]

    target = optional_ids[0] if optional_ids else mandatory_ids[0]
    marked = post(pult, "/api/programme", action="mandatory", check_id=target, mandatory=True)
    row = next(item for item in marked["programme"]["rows"] if item["check_id"] == target)
    assert row["mandatory"] is True

    with pytest.raises(ApiError) as missing:
        post(pult, "/api/programme", action="mandatory", check_id="TC-NO-SUCH")
    assert missing.value.status == 404
    with pytest.raises(ApiError) as unknown:
        post(pult, "/api/programme", action="пересобрать")
    assert unknown.value.status == 422


def test_protocol_csv_is_served_as_attachment(pult: PultApp) -> None:
    """Выгрузку протокола, наоборот, отдаём файлом: `attachment` + имя."""
    _ready_session(pult)
    server = serve(pult, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/api/protocol?format=csv") as response:
            assert response.headers["Content-Type"].startswith("text/csv")
            assert response.headers["Content-Disposition"] == 'attachment; filename="protocol.csv"'
    finally:
        server.shutdown()
        server.server_close()
    thread.join(timeout=5)


#: Порядок вкладок пульта, согласованный с заказчиком (`llm_tasks/49`): работа идёт
#: от подготовки (планирование, сессии) к прогону, результатам и разбору обмена.
#: Список один на HTML и JS: иначе кнопки на экране и `panelFromHash` разъедутся.
PANEL_ORDER: tuple[tuple[str, str], ...] = (
    ("planning", "Планирование"),
    ("sessions", "Сессии"),
    ("run", "Прогон"),
    ("results", "Результаты"),
    ("journal", "Лента обмена"),
)

#: Кнопка вкладки в разметке: класс (`is-active` только у стартовой), ключ панели и подпись.
TAB_BUTTON = re.compile(
    r'<button class="tab[^"]*" data-panel="([^"]+)" type="button">([^<]+)</button>'
)

#: Панель страницы: `id` обязан совпадать с ключом вкладки (`panel-<ключ>`).
PANEL_SECTION = re.compile(r'<section class="panel[^"]*" id="panel-([^"]+)"')


def test_tab_order_matches_agreed_sequence(pult: PultApp) -> None:
    """Порядок вкладок и стартовая панель совпадают с согласованными (`IR-P-4`).

    Регресс-страж: порядок «Прогон, Планирование, Результаты, Сессии, Лента обмена»
    остался от первой сборки страницы и разошёлся с порядком работы оператора
    (`llm_tasks/49` §«Порядок работ»). Проверяются разом три места, которые обязаны
    совпадать: разметка вкладок, активная панель и список панелей в JS.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    script = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    tabs = [match.groups() for match in TAB_BUTTON.finditer(html)]
    assert tabs == list(PANEL_ORDER), "порядок и подписи вкладок в index.html"
    assert '<button class="tab is-active" data-panel="planning"' in html, (
        "пульт открывается на «Планировании»"
    )
    assert 'class="panel is-active" id="panel-planning"' in html, "активная панель — «Планирование»"
    sections = [match.group(1) for match in PANEL_SECTION.finditer(html)]
    assert len(sections) == len(set(sections)), (
        "у каждой панели свой `id`: два одинаковых `id` показали бы не ту страницу "
        "(регресс, найденный проверкой отрисовки браузером)"
    )
    assert set(sections) == {key for key, _ in PANEL_ORDER}, (
        "каждой вкладке соответствует своя панель `panel-<ключ>`"
    )
    assert html.count('class="panel is-active"') == 1, "активная панель ровно одна"

    declared = re.search(r"const PANELS = \[([^\]]+)\];", script)
    assert declared is not None, "в app.js нет списка панелей"
    keys = [part.strip().strip('"') for part in declared.group(1).split(",")]
    assert keys == [key for key, _ in PANEL_ORDER], "список панелей в app.js"
    assert "PANELS.includes(name) ? name : PANELS[0]" in script, (
        "неизвестный адрес открывает стартовую вкладку, а не «Прогон»"
    )


def test_static_reads_binary_files_as_bytes(tmp_path: Path) -> None:
    """Не-текстовый файл (иконка, шрифт) отдаётся байтами, а не падает на чтении."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    payload = bytes(range(256))
    (web / "favicon.ico").write_bytes(payload)
    app = PultApp(state=PultState(), web_dir=web)
    icon = app.static("/favicon.ico")
    assert icon.data == payload
    assert icon.content_type == "image/x-icon"
    assert icon.download is False


# ---------------------------------------------------------------------------
# Фильтр «Данных стенда» по датасету (`llm_tasks/49`, задача 1а)
# ---------------------------------------------------------------------------
#: Датасет с задачей наполнения `dataset-fill`: состав восстанавливается по задаче.
FILLED_DATASET = "11111111-1111-1111-1111-111111111111"
#: Датасет, о котором пульту ничего не известно: состав недоступен (замечание P1).
UNKNOWN_DATASET = "22222222-2222-2222-2222-222222222222"
#: Копия «skfu-record-1.raw.csv» с другим `id` — тёзка. В состав датасета не входит,
#: хотя имя совпадает: живой стенд держит до трёх копий одного имени, и отбор по имени
#: показывал 312 строк вместо 104 (`llm_tasks/49`, задача 1а).
TWIN_FILE = "cccc0000-0000-0000-0000-000000000002"
FILL_TASK = "33333333-3333-3333-3333-333333333333"
RAW_ONE = "aaaa0000-0000-0000-0000-000000000001"
RAW_TWO = "aaaa0000-0000-0000-0000-000000000002"
MARKUP_ONE = "bbbb0000-0000-0000-0000-000000000001"
OUTSIDE_FILE = "cccc0000-0000-0000-0000-000000000001"
DATA_AT = "2026-09-20T09:00:00"

#: Реестр стенда: два RAW и markup датасета `skfu-train` плюс файл вне датасетов.
STAND_FILES: list[dict[str, Any]] = [
    {
        "id": RAW_ONE,
        "file_name": "skfu-record-1.raw.csv",
        "size": 2048,
        "s3_path": "s3://stand/skfu-record-1.raw.csv",
        "import_date": DATA_AT,
        "file_type": "RAW",
    },
    {
        "id": MARKUP_ONE,
        "file_name": "skfu-record-1.markup.csv",
        "size": 512,
        "s3_path": "s3://stand/skfu-record-1.markup.csv",
        "import_date": DATA_AT,
        "file_type": "markup",
    },
    {
        "id": RAW_TWO,
        "file_name": "skfu-record-2.raw.csv",
        "size": 4096,
        "s3_path": "s3://stand/skfu-record-2.raw.csv",
        "import_date": DATA_AT,
        "file_type": "RAW",
    },
    {
        "id": OUTSIDE_FILE,
        "file_name": "foreign-record-1.raw.csv",
        "size": 1024,
        "s3_path": "s3://stand/foreign-record-1.raw.csv",
        "import_date": DATA_AT,
        "file_type": "RAW",
    },
    {
        "id": TWIN_FILE,
        "file_name": "skfu-record-1.raw.csv",
        "size": 2048,
        "s3_path": "s3://stand/twin/skfu-record-1.raw.csv",
        "import_date": DATA_AT,
        "file_type": "RAW",
    },
]

STAND_DATASETS: list[dict[str, Any]] = [
    {
        "id": FILLED_DATASET,
        "creation_date": DATA_AT,
        "name": "skfu-train",
        "description": "обучающий датасет",
        "type": "direct_fill",
    },
    {
        "id": UNKNOWN_DATASET,
        "creation_date": DATA_AT,
        "name": "без состава",
        "description": "",
        "type": "direct_fill",
    },
]

FILL_TASK_SUMMARY: dict[str, Any] = {
    "type": "dataset-fill",
    "name": f"dataset-fill {FILLED_DATASET}",
    "description": f"наполнение датасета {FILLED_DATASET}",
    "id": FILL_TASK,
    "created_at": DATA_AT,
}

FILL_TASK_DETAIL: dict[str, Any] = {
    **FILL_TASK_SUMMARY,
    "runtimes": [
        {
            "task_id": FILL_TASK,
            "status": "completed",
            "parameters": {
                "dataset_id": FILLED_DATASET,
                "raw_file_ids": [RAW_ONE, RAW_TWO],
                "markup_file_ids": [MARKUP_ONE],
            },
            "result": {
                "raw_processing": {"total_records": 2},
                "markup_processing": {"total_records": 1},
            },
            "id": "44444444-4444-4444-4444-444444444444",
        }
    ],
}


def _data_handler(request: httpx.Request) -> httpx.Response:
    """Подменённый стенд с реестром файлов, датасетами и задачей наполнения."""
    path = request.url.path
    if path.endswith("/version"):
        return httpx.Response(200, json={"branch": "dev", "revision": "83319ae1234"})
    if path.startswith("/api/data/files"):
        return httpx.Response(200, json={"files": STAND_FILES, "count": len(STAND_FILES)})
    if path.startswith("/api/loads/list"):
        return httpx.Response(200, json={"loads": [], "result_size": 0})
    if path.startswith("/api/datasets"):
        return httpx.Response(200, json={"datasets": STAND_DATASETS, "count": len(STAND_DATASETS)})
    if path.startswith("/api/ml_models/models"):
        return httpx.Response(200, json={"models": [], "count": 0})
    if path.startswith("/api/tasks"):
        if path.rstrip("/") == "/api/tasks":
            return httpx.Response(200, json={"tasks": [FILL_TASK_SUMMARY], "count": 1})
        return httpx.Response(200, json=FILL_TASK_DETAIL)
    return httpx.Response(200, json={"status": "ok", "message": "test stand ready"})


@pytest.fixture
def data_stand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PultApp:
    """Пульт со стендом, у которого есть файлы, датасеты и задача наполнения."""
    return _app(tmp_path, monkeypatch, _data_handler)


def test_dataset_filter_narrows_files_and_records(data_stand: PultApp) -> None:
    """`?dataset=` отбирает строки по составу из задачи наполнения и подписывает источник.

    Проверяется главное обещание фильтра (`llm_tasks/49`, задача 1а): строки те же,
    но их состав — только файлы датасета, и рядом видно, чем это подтверждено
    (`server_task`), сколько файлов и что фильтр применён.
    """
    _ready_session(data_stand)
    everything = get(data_stand, "/api/results")
    assert len(everything["data"]["files"]) == len(STAND_FILES)
    assert everything["data"]["filter"]["applied"] is False
    assert everything["data"]["filter"]["mode"] == "off"
    assert everything["data"]["sections"][0]["label"] == f"$Файлы · {len(STAND_FILES)}"

    data = get(data_stand, "/api/results", dataset=FILLED_DATASET)["data"]
    assert [row["name"] for row in data["files"]] == [
        "skfu-record-1.raw.csv",
        "skfu-record-1.markup.csv",
        "skfu-record-2.raw.csv",
    ], "тёзка (то же имя, чужой id) в состав не попадает"
    assert len(data["records"]) == 2, "записи тоже отобраны по составу"
    assert data["filter"]["applied"] is True
    assert data["filter"]["source"] == "server_task"
    assert data["filter"]["trusted"] is True
    assert data["filter"]["files"] == 3
    assert data["filter"]["raw"] == 2 and data["filter"]["markup"] == 1
    assert data["filter"]["label"].startswith("датасет «skfu-train»")
    assert "файлов 3" in data["filter"]["label"]
    assert data["sections"][0]["label"] == f"$Файлы · 3 из {len(STAND_FILES)}"
    assert data["sections"][0]["page_label"] == "показано 3 из 3"
    assert data["filter"]["options"][0]["value"] == ""
    assert data["filter"]["options"][1]["value"] == "none"
    chosen = next(item for item in data["filter"]["options"] if item["value"] == FILLED_DATASET)
    assert "файлов 3" in chosen["label"] and "достоверно" in chosen["label"]
    assert len(data["filter"]["sources"]) == 5, "источник состава можно переключить"


def test_dataset_filter_says_when_composition_is_unavailable(data_stand: PultApp) -> None:
    """Состав недоступен → фильтр **не применён**, и в интерфейсе это сказано прямо.

    Показать пустой список вместо реестра было бы обманом: оператор решил бы, что
    в датасете нет файлов (`FR-P-25`, `llm_tasks/49`, задача 1а).
    """
    _ready_session(data_stand)
    data = get(data_stand, "/api/results", dataset=UNKNOWN_DATASET)["data"]

    assert len(data["files"]) == len(STAND_FILES), "показан весь реестр"
    assert data["filter"]["applied"] is False
    assert data["filter"]["trusted"] is False
    assert "фильтр не применён" in data["filter"]["label"]
    assert "состав недоступен" in data["filter"]["label"]
    assert data["sections"][0]["label"] == f"$Файлы · {len(STAND_FILES)}"


def test_outside_filter_shows_files_not_in_any_dataset(data_stand: PultApp) -> None:
    """«Файлы вне датасетов»: чужой файл и тёзка, которых нет ни в одном составе (ответ 1 = б)."""
    _ready_session(data_stand)
    data = get(data_stand, "/api/results", dataset="none")["data"]

    assert sorted(row["name"] for row in data["files"]) == [
        "foreign-record-1.raw.csv",
        "skfu-record-1.raw.csv",
    ], "тёзка с чужим id — вне датасетов, хотя имя совпадает с файлом состава"
    assert data["filter"]["mode"] == "outside"
    assert data["filter"]["applied"] is True
    assert data["filter"]["label"].startswith("файлы вне датасетов")
    assert "учтено датасетов: 2" in data["filter"]["label"]
    assert data["filter"]["unavailable"] == ["без состава"]
    assert data["filter"]["note"] == "состав не прочитан у без состава"
    assert "состав не прочитан у 1" in data["filter"]["label"]


def test_dataset_filter_can_force_source(data_stand: PultApp) -> None:
    """`source=heuristic` заставляет читать состав как гипотезу (`read_composition(mode=…)`).

    Гипотеза знает **только имена**: отличить тёзку (то же имя, другой `id`) нечем, поэтому
    файлов больше, а подпись честно говорит «требует проверки» — именно для этого источник
    и показывается рядом с составом (`llm_tasks/49`, задача 1а).
    """
    _ready_session(data_stand)
    data = get(data_stand, "/api/results", dataset=FILLED_DATASET, source="heuristic")["data"]

    assert data["filter"]["source"] == "heuristic"
    assert data["filter"]["trusted"] is False
    assert "требует проверки" in data["filter"]["label"]
    assert [row["name"] for row in data["files"]] == [
        "skfu-record-1.raw.csv",
        "skfu-record-1.markup.csv",
        "skfu-record-2.raw.csv",
        "skfu-record-1.raw.csv",
    ], "состав по именам включает тёзку: id у гипотезы нет"


def test_dataset_filter_rejects_unknown_dataset(data_stand: PultApp) -> None:
    """Датасета нет в реестре → 404 с понятным текстом, а не пустой экран."""
    _ready_session(data_stand)
    with pytest.raises(ApiError) as unknown:
        get(data_stand, "/api/data", dataset="99999999-0000-0000-0000-000000000000")
    assert unknown.value.status == 404
    assert "не найден" in unknown.value.message


# ---------------------------------------------------------------------------
# Параметры ML-проверки: датасет и модель в вызове (`llm_tasks/49`, задача 1б)
# ---------------------------------------------------------------------------
ML_MODEL = "dddd0000-0000-0000-0000-000000000001"
ML_TASK = "eeee0000-0000-0000-0000-000000000001"


MODEL_FILE = "ffff0000-0000-0000-0000-000000000001"


def _ml_handler(sent: list[httpx.Request]) -> Any:
    """Стенд с датасетом и моделью: запросы обучения складываются в `sent`."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/version"):
            return httpx.Response(200, json={"branch": "dev", "revision": "83319ae1234"})
        if path.startswith("/api/data/files"):
            return httpx.Response(200, json={"files": STAND_FILES, "count": len(STAND_FILES)})
        if "/train" in path:
            sent.append(request)
            return httpx.Response(202, json={"task_id": ML_TASK, "status": "queued"})
        if path.startswith("/api/ml_models/models"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": ML_MODEL,
                            "file_id": MODEL_FILE,
                            "architecture": "cnn",
                            "version": "3",
                            "name": "skfu-net",
                            "phase_type": "train",
                            "output_size": 4,
                            "config": {},
                            "created_at": DATA_AT,
                            "updated_at": DATA_AT,
                        }
                    ],
                    "count": 1,
                },
            )
        if path.startswith("/api/datasets"):
            return httpx.Response(
                200, json={"datasets": STAND_DATASETS, "count": len(STAND_DATASETS)}
            )
        if path.startswith("/api/tasks"):
            return httpx.Response(200, json={"tasks": [], "count": 0})
        return httpx.Response(200, json={"status": "ok", "message": "test stand ready"})

    return handler


@pytest.fixture
def ml_stand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PultApp, list[Any]]:
    """Пульт с сессией на `TC-TR-01` и списком ушедших на стенд запросов обучения."""
    sent: list[httpx.Request] = []
    pult = _app(tmp_path, monkeypatch, _ml_handler(sent))
    _with_checks(pult, check_class="tech", check_id="TC-TR-01")
    return pult, sent


def test_ml_check_shows_params_panel_with_registry_options(
    ml_stand: tuple[PultApp, list[Any]],
) -> None:
    """Проверка обучения `tech`: пульт предлагает датасеты и модели стенда, а не пустую форму.

    Датасет обязателен (он ключ тела `train`), модель — параметр пути; выбор сохраняется
    через `ml-params`, потому что карточки запуска у `tech`-проверки нет (решение 2 = а).
    """
    pult, _sent = ml_stand
    run = get(pult, "/api/run")

    assert run["next"]["check_id"] == "TC-TR-01"
    ml = run["next"]["ml"]
    assert ml["needs"] == {"dataset": True, "model": True}
    assert ml["panel"] == "params" and ml["save_as"] == "ml-params"
    assert [field["name"] for field in ml["fields"]] == ["dataset_id", "model_id"]
    assert [option["value"] for option in ml["fields"][0]["options"]] == [
        FILLED_DATASET,
        UNKNOWN_DATASET,
    ]
    assert ml["fields"][1]["options"] == [{"value": ML_MODEL, "label": "skfu-net · cnn · v3"}]
    assert ml["data_hint"] == "датасет не выбран: выберите его в поле «Датасет для проверки»"
    assert "датасет не выбран" in "\n".join(run["next"]["item"]["payload"]["lines"]).lower(), (
        "предпросмотр не молчит: без датасета вызов не отправляется"
    )


def test_ml_call_refuses_without_dataset_and_model(ml_stand: tuple[PultApp, list[Any]]) -> None:
    """Без выбранных данных вызов не уходит: отказ называет поля и где их выбрать."""
    pult, sent = ml_stand

    with pytest.raises(ApiError) as refused:
        post(pult, "/api/run", action="call")

    assert refused.value.status == 422
    assert "Датасет для проверки" in refused.value.message
    assert "Модель" in refused.value.message
    assert sent == [], "на стенд ничего не ушло"


def test_ml_params_reach_the_train_call(ml_stand: tuple[PultApp, list[Any]]) -> None:
    """Выбранные датасет и модель действительно уходят в `train` (`llm_tasks/48` §1б).

    Проверяется главное: `dataset_id` попадает в **тело** запроса вместо заглушки
    `"<uuid>"`, а `model_id` — в **путь**. Раньше пульт отправлял заготовку как есть.
    """
    pult, sent = ml_stand
    saved = post(
        pult,
        "/api/checks/TC-TR-01",
        action="ml-params",
        values={"dataset_id": FILLED_DATASET, "model_id": ML_MODEL},
    )

    assert saved["run"]["next"]["ml"]["values"]["dataset_id"] == FILLED_DATASET
    assert saved["run"]["next"]["ml"]["values"]["model_id"] == ML_MODEL
    assert saved["run"]["card"]["fields"][2]["value"] == FILLED_DATASET, "выбор виден в форме"

    answer = post(pult, "/api/run", action="call")

    assert answer["outcomes"], "вызов выполнен"
    assert len(sent) == 1, "на стенд ушёл ровно один запрос обучения"
    request = sent[0]
    assert request.url.path == f"/api/ml_models/models/{ML_MODEL}/train", "модель — в пути"
    raw = request.content.decode("utf-8")
    assert json.loads(raw)["dataset_id"] == FILLED_DATASET
    assert "<uuid>" not in raw, "заглушка не ушла на стенд"
