"""Смоук-срез «данные К2»: файл → датасет → наполнение → отметка «проверен вне пульта».

Проверяется то, что тесты уровня `dispatch` не видят (`llm_tasks/64` §2): тело запроса —
это **файл**, а не JSON, поэтому срез идёт через живой сервер по HTTP и грузит байты
сырым телом (`urllib`, `Content-Type: text/csv`), как это сделает браузер оператора.

Шаги среза:

    1. сессия испытаний (без неё паспорт файла «ушёл бы в никуда»);
    2. загрузка RAW-файла: `sha256` и размер считает пульт, файл уходит на стенд;
    3. загрузка файла разметки (`LOADS`) и наполнение датасета — задача `dataset-fill`;
    4. отметка «датасет проверен вне пульта» (валидация внешняя, решение заказчика №6);
    5. `GET /api/train` и `GET /api/datasets/{id}`: паспорт и состав видны в интерфейсе;
    6. отказ на боевое имя без режима полной нагрузки (§9 ТЗ).

Стенд подменён `httpx.MockTransport`, сессии пишутся во временный каталог: тест не
зависит от живого стенда и не трогает `acceptance_data/`.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.api import Apis
from acceptance.config import PultConfig
from acceptance.http_log import Journal, LoggingTransport
from acceptance.logging_setup import setup_logging
from acceptance.ml_samples import RAW_COLUMNS, SAMPLE_SIGNAL_FIELDS, raw_row
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings
from pult.server import PultApp, serve
from pult.state import PultState, Runtime

BASE_URL = "http://test.local"
AUTHOR = "Петров П.П."
WEB_DIR = Path(__file__).resolve().parents[2] / "pult" / "web"

#: Тела загрузки — по каноническим заголовкам стенда: пульт проверяет формат до отправки
#: файла (`pult/datafile_format.py`), поэтому «минимальный» CSV в срезе больше не проходит.
RAW_BODY = (
    ";".join(RAW_COLUMNS) + "\n" + raw_row(dict.fromkeys(SAMPLE_SIGNAL_FIELDS, "AAAA")) + "\n"
).encode("utf-8")
MARKUP_BODY = b"markupId;chunkID;LoadID;Ph_n;Category\n1;1;Antminer_S19;A;miner\n"

#: Части multipart-запроса от `httpx`: поле, имя файла (если есть) и значение.
_PART = re.compile(
    rb'name="(?P<field>[^"]+)"(?:; filename="(?P<name>[^"]*)")?\r\n'
    rb"(?:Content-Type: [^\r\n]*\r\n)?\r\n(?P<value>.*?)\r\n--",
    re.S,
)


def _parts(body: bytes) -> tuple[dict[str, bytes], dict[str, str]]:
    """Разбирает multipart: значения полей и имена файловых частей (поле → имя файла)."""
    values: dict[str, bytes] = {}
    names: dict[str, str] = {}
    for match in _PART.finditer(body):
        field = match.group("field").decode()
        values[field] = match.group("value")
        file_name = match.group("name")
        if file_name is not None:
            names[field] = file_name.decode()
    return values, names


class Stand:
    """Подменённый стенд: реестр файлов, датасеты и задачи наполнения — в памяти."""

    def __init__(self) -> None:
        self.files: list[dict[str, Any]] = []
        self.datasets: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Ответ стенда на операции данных К2."""
        path = request.url.path
        if path.endswith("/version"):
            return httpx.Response(200, json={"branch": "dev", "revision": "83319ae1234"})
        if path == "/api/data/file" and request.method == "POST":
            return self._upload(*_parts(request.read()))
        if path == "/api/data/files":
            return httpx.Response(200, json={"files": self.files, "count": len(self.files)})
        if path == "/api/datasets/":
            if request.method == "POST":
                return self._create_dataset(json.loads(request.read().decode("utf-8")))
            return httpx.Response(
                200, json={"datasets": self.datasets, "count": len(self.datasets)}
            )
        if path.startswith("/api/datasets/fill/"):
            dataset_id = path.rsplit("/", 1)[-1]
            return self._fill(dataset_id, json.loads(request.read().decode("utf-8")))
        if path.startswith("/api/tasks/"):
            return httpx.Response(200, json={"tasks": self.tasks, "count": len(self.tasks)})
        if path.startswith("/api/loads/list"):
            return httpx.Response(200, json={"loads": [], "result_size": 0})
        if path.startswith("/api/ml_models/models"):
            return httpx.Response(200, json={"models": [], "count": 0})
        return httpx.Response(200, json={"status": "ok", "message": "test stand ready"})

    def _upload(self, values: dict[str, bytes], names: dict[str, str]) -> httpx.Response:
        """Приём файла: имя — из multipart, размер — по принятым байтам."""
        name = names.get("file") or "file.csv"
        content = values.get("file", b"")
        meta = {
            "id": str(uuid4()),
            "file_name": name,
            "size": len(content),
            "s3_path": f"{values.get('file_type', b'RAW').decode()}/{name}",
            "import_date": "2026-10-02T10:00:00",
            "file_type": values.get("file_type", b"RAW").decode(),
        }
        self.files.append(meta)
        return httpx.Response(201, json=meta)

    def _create_dataset(self, body: dict[str, Any]) -> httpx.Response:
        """Создание датасета: имя и описание такие, какими их передал пульт."""
        dataset = {
            "id": str(uuid4()),
            "name": body.get("name"),
            "description": body.get("description"),
            "type": body.get("type") or "direct_fill",
            "creation_date": "2026-10-02T10:00:00",
        }
        self.datasets.append(dataset)
        return httpx.Response(201, json=dataset)

    def _fill(self, dataset_id: str, body: dict[str, Any]) -> httpx.Response:
        """Наполнение датасета: 202 **без** `task_id` — как на живом стенде (`TC-DS-05`)."""
        self.fills.append({"dataset_id": dataset_id, **body})
        self.tasks = [
            {
                "type": "dataset-fill",
                "name": f"dataset-fill {dataset_id}",
                "description": "заполнение датасета из пульта",
                "id": str(uuid4()),
                "created_at": "2026-10-02T10:00:01",
            }
        ]
        return httpx.Response(202, json={"status": "ok", "message": "fill started"})


def _towards_store(name: str, store: Path) -> Any:
    """Функция сессии, работающая во временном каталоге (свой каталог не трогаем)."""
    original = getattr(session_api, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if len(args) > 1 or "directory" in kwargs:
            return original(*args, **kwargs)
        return original(*args, directory=store, **kwargs)

    return wrapped


@pytest.fixture
def pult(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Живой сервер пульта и подменённый стенд: (адрес, стенд)."""
    stand = Stand()
    store = tmp_path / "sessions"
    store.mkdir()
    journal = Journal(max_records=50)
    transport = LoggingTransport(journal, inner=httpx.MockTransport(stand.handler), body_limit=200)
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
        session_id="data-slice",
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

    server = serve(PultApp(state=PultState(), web_dir=WEB_DIR), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", stand
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


def upload(base: str, *, name: str, body: bytes, file_type: str = "RAW") -> dict[str, Any]:
    """Загрузка файла **сырым телом** (как браузер): параметры — в query-строке."""
    route = f"/api/data/files?file_name={name}&file_type={file_type}"
    request = urllib.request.Request(
        f"{base}{route}",
        data=body,
        headers={"Content-Type": "text/csv"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def failed(base: str, *, name: str, body: bytes) -> dict[str, Any]:
    """Ожидаемая ошибка API: конверт `{ok: false, error}` с кодом ответа."""
    route = f"/api/data/files?file_name={name}&file_type=RAW"
    request = urllib.request.Request(
        f"{base}{route}", data=body, headers={"Content-Type": "text/csv"}, method="POST"
    )
    try:
        urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        return {"status": int(exc.code), **json.loads(exc.read().decode("utf-8"))}
    raise AssertionError("ожидался отказ API, но запрос прошёл")


def _session(base: str) -> str:
    """Создаёт и запускает сессию испытаний, возвращает её идентификатор."""
    created = api(
        base,
        "/api/sessions",
        {"info": {"title": "Данные К2", "object_of_test": "Тренажёр", "operator_fio": AUTHOR}},
    )
    session_id = created["id"]
    api(base, f"/api/sessions/{session_id}/start", {})
    api(
        base,
        "/api/train",
        {
            "action": "save-profile",
            "object_of_test": "Стенд ML",
            "goal": "обучить stft_mlp",
            "responsible": AUTHOR,
        },
    )
    return str(session_id)


def test_data_slice_through_live_server(pult: Any) -> None:
    """Файлы, датасет и отметка валидации проходят через живой сервер пульта."""
    base, stand = pult
    _session(base)

    raw = upload(base, name="__TEST__raw-01.csv", body=RAW_BODY)
    assert raw["ok"] is True, raw
    assert raw["data"]["file"]["size"] == len(RAW_BODY)
    assert raw["data"]["file"]["sha256"] == hashlib.sha256(RAW_BODY).hexdigest()
    assert raw["data"]["file"]["size_text"].endswith("Б")
    assert stand.files[-1]["file_name"] == "__TEST__raw-01.csv"
    assert stand.files[-1]["size"] == len(RAW_BODY), "стенд получил именно эти байты"

    markup = upload(base, name="__TEST__loads-01.csv", body=MARKUP_BODY, file_type="LOADS")
    assert markup["data"]["data"]["upload_count"] == 2
    assert markup["data"]["data"]["raw_count"] == 1
    assert markup["data"]["data"]["markup_count"] == 1
    assert markup["data"]["data"]["uploads"][0]["role_label"] == "измерения (RAW)"

    created = api(
        base,
        "/api/datasets",
        {"action": "create", "name": "__TEST__dataset-smoke", "description": "срез Э2.1"},
    )
    dataset_id = created["dataset"]["id"]
    assert created["view"]["data"]["dataset_id"] == dataset_id
    assert stand.datasets[-1]["name"] == "__TEST__dataset-smoke"

    filled = api(base, "/api/datasets", {"action": "fill"})
    assert filled["raw_file_ids"] == [stand.files[0]["id"]], "в датасет ушли файлы измерений"
    assert filled["markup_file_ids"] == [stand.files[1]["id"]]
    assert filled["task"]["id"], "задача `dataset-fill` найдена в реестре $Задач"
    assert stand.fills[-1]["raw_file_ids"] == [stand.files[0]["id"]]
    steps = {step["key"]: step for step in filled["view"]["data"]["steps"]}
    assert steps["fill"]["status"] == "наполнение запущено"

    validated = api(base, "/api/datasets", {"action": "validate"})
    assert validated["manifest"]["file_count"] == 2
    steps = {step["key"]: step for step in validated["view"]["data"]["steps"]}
    assert steps["validated"]["status"] == "проверен вне пульта"

    train = api(base, "/api/train")
    states = {step["key"]: step for step in train["steps"]}
    assert states["dataset"]["status"] == "готово" and states["dataset"]["ready"] is True
    assert train["data"]["manifest_rows"][1] == {"label": "Файлов", "value": "2"}

    detail = api(base, f"/api/datasets/{dataset_id}")
    assert detail["meta"]["name"] == "__TEST__dataset-smoke"
    assert detail["composition"]["files"] == 2
    assert [row["role"] for row in detail["manifest_files"]] == ["RAW", "LOADS"]


def test_data_slice_refuses_battle_name_without_full_load(pult: Any) -> None:
    """Боевое имя без режима полной нагрузки отклоняется до обращения к стенду (§9 ТЗ)."""
    base, stand = pult
    _session(base)

    refused = failed(base, name="skfu-train-raw-01.csv", body=RAW_BODY)

    assert refused["ok"] is False
    assert refused["status"] == 422
    assert "__TEST__" in refused["error"]["message"]
    assert stand.files == [], "отказ произошёл до загрузки файла на стенд"


def test_data_slice_accepts_battle_name_in_full_load(pult: Any) -> None:
    """В режиме полной нагрузки боевое имя разрешено — так заказчик просил (решение 3)."""
    base, stand = pult
    _session(base)
    api(base, "/api/train", {"action": "save-profile", "full_load": True})

    allowed = upload(base, name="skfu-train-raw-02.csv", body=RAW_BODY)

    assert allowed["ok"] is True
    assert allowed["data"]["file"]["file_name"] == "skfu-train-raw-02.csv"
    assert stand.files[-1]["file_name"] == "skfu-train-raw-02.csv"
