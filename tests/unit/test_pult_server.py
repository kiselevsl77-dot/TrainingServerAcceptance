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
import threading
import urllib.error
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
