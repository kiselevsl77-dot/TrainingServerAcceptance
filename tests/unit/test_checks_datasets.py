"""Тесты сценариев проверок модуля «Datasets» (TC-DS-01…07, этап T5).

Подменённый стенд повторяет живой API датасетов и его ключевые особенности:

    * `GET /api/datasets/{id}` отдаёт только метаданные — ни состава, ни агрегатов (P1);
    * `POST /api/datasets/fill/{id}` отвечает 202 **без `task_id`**, поэтому проверка
      ищет задачу `dataset-fill` в `GET /api/tasks/` (обходной путь монитора T4);
    * новые операции (`summary` состава) в стенд добавляются по ходу теста: так
      проверяется, что `TC-DS-03` сам переходит в строгий режим, когда замечание P1
      устранят, — без правок сценария и экрана.

Изменяющие проверки работают только с `__TEST__`-датасетами и обязаны убирать за собой:
тесты проверяют и результат, и самоочистку (NFR-T4).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from acceptance import dataset_composition as composition
from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import datasets as check_datasets
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckSpec, CheckStatus
from acceptance.dataset_composition import SOURCE_HEURISTIC, SOURCE_LOCAL, SOURCE_SERVER
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session
from acceptance.tasks_monitor import TaskMonitor
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
Handler = Callable[[httpx.Request], httpx.Response]

DATASET_ID = "5f0a5b7e-0000-4000-8000-000000000001"
DATASET_2 = "5f0a5b7e-0000-4000-8000-000000000002"
TASK_ID = "5f0a5b7e-0000-4000-8000-00000000000a"
RAW_ID = "11111111-1111-1111-1111-111111111111"
MARKUP_ID = "22222222-2222-2222-2222-222222222222"
RAW_2 = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _composition_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Локальный учёт состава в тестах не пишется в `acceptance_data` репозитория."""
    monkeypatch.setattr(composition, "DATA_DIR", tmp_path)


def file_payload(
    file_id: str, name: str, *, size: int = 4096, kind: str = "RAW", path: str = ""
) -> dict[str, Any]:
    """Файл реестра `GET /api/data/files`."""
    return {
        "id": file_id,
        "file_name": name,
        "size": size,
        "s3_path": path or f"s3://bucket/{name}",
        "import_date": "2026-09-15T10:00:00",
        "file_type": kind,
    }


def default_files() -> list[dict[str, Any]]:
    """Реестр файлов: пара RAW+markup и непарный файл."""
    return [
        file_payload(RAW_ID, "skfu-record-1.raw.csv"),
        file_payload(MARKUP_ID, "skfu-record-1.markup.csv", size=512),
        file_payload(RAW_2, "skfu-record-2.raw.csv", size=8192),
    ]


def dataset_payload(
    dataset_id: str,
    name: str,
    *,
    description: str = "",
    created: str = "2026-09-15T10:00:00",
) -> dict[str, Any]:
    """Элемент реестра датасетов (`DatasetResponse`)."""
    return {
        "id": dataset_id,
        "creation_date": created,
        "name": name,
        "description": description or None,
        "type": "direct_fill",
    }


def default_datasets() -> list[dict[str, Any]]:
    """Реестр датасетов стенда: два «боевых» датасета."""
    return [
        dataset_payload(DATASET_ID, "skfu-train", description="обучающий набор СКФУ"),
        dataset_payload(DATASET_2, "leti-train", created="2026-09-16T11:00:00"),
    ]


def spec_of(check_id: str) -> CheckSpec:
    """Описание проверки из каталога (в тестах оно всегда существует)."""
    spec = catalog.find(check_id)
    assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
    return spec


class FakeDatasets:
    """Подменённый сервер датасетов: реестр, метаданные, правка, удаление и наполнение.

    Флаги конструктора включают варианты поведения живого стенда, которые проверяются
    сценариями (`break_name_filter`, `keep_after_delete`, `create_task=False` и др.).
    """

    def __init__(
        self,
        *,
        datasets: list[dict[str, Any]] | None = None,
        files: list[dict[str, Any]] | None = None,
        fill_status: int = 202,
        fill_error_status: int = 0,
        create_task: bool = True,
        task_status: str = "completed",
        delete_status: int = 204,
        keep_after_delete: bool = False,
        break_name_filter: bool = False,
        break_count: bool = False,
        keep_update: bool = False,
        unknown_dataset_status: int = 404,
    ) -> None:
        self.datasets = [
            dict(item) for item in (default_datasets() if datasets is None else datasets)
        ]
        self.files = [dict(item) for item in (default_files() if files is None else files)]
        self.fill_status = fill_status
        self.fill_error_status = fill_error_status
        self.create_task = create_task
        self.task_status = task_status
        self.delete_status = delete_status
        self.keep_after_delete = keep_after_delete
        self.break_name_filter = break_name_filter
        self.break_count = break_count
        self.keep_update = keep_update
        self.unknown_dataset_status = unknown_dataset_status
        self.tasks: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.fill_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []

    # -- маршруты ------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого сервера (датасеты, файлы, задачи)."""
        path = request.url.path
        if path == "/api/data/files":
            return httpx.Response(200, json={"files": self.files, "count": len(self.files)})
        if path == "/api/tasks/":
            task_type = request.url.params.get("task_type") or ""
            rows = [task for task in self.tasks if not task_type or task["type"] == task_type]
            return httpx.Response(200, json={"tasks": rows, "count": len(rows)})
        if path.startswith("/api/tasks/"):
            return self._card(path.rsplit("/", 1)[-1])
        if path.startswith("/api/datasets/fill/"):
            return self._fill(path.rsplit("/", 1)[-1], request)
        if path == "/api/datasets/":
            if request.method == "POST":
                return self._create(request)
            return self._list(request)
        if path.startswith("/api/datasets/"):
            dataset_id = path.rsplit("/", 1)[-1]
            if request.method == "GET":
                return self._meta(dataset_id)
            if request.method == "PUT":
                return self._update(dataset_id, request)
            if request.method == "DELETE":
                return self._delete(dataset_id)
        return httpx.Response(404, json={"detail": "Not Found"})

    # -- реализация ----------------------------------------------------------
    def find(self, dataset_id: str) -> dict[str, Any] | None:
        """Датасет стенда по идентификатору."""
        return next((item for item in self.datasets if str(item["id"]) == dataset_id), None)

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        rows = list(self.datasets)
        if params.get("name") and not self.break_name_filter:
            rows = [item for item in rows if str(item["name"]) == params["name"]]
        if params.get("description"):
            rows = [item for item in rows if str(item["description"]) == params["description"]]
        if params.get("type"):
            rows = [item for item in rows if str(item["type"]) == params["type"]]
        if params.get("creation_date"):
            rows = [
                item
                for item in rows
                if str(item["creation_date"]).startswith(str(params["creation_date"]))
            ]
        count = len(rows) + (5 if self.break_count else 0)
        return httpx.Response(200, json={"datasets": rows, "count": count})

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        dataset_id = str(uuid4())
        row = dataset_payload(
            dataset_id,
            str(body.get("name") or ""),
            description=str(body.get("description") or ""),
            created=datetime.now().isoformat(timespec="seconds"),
        )
        self.datasets.append(row)
        self.created.append(dataset_id)
        return httpx.Response(201, json=row)

    def _meta(self, dataset_id: str) -> httpx.Response:
        found = self.find(dataset_id)
        if found is None:
            return httpx.Response(404, json={"detail": "Dataset not found"})
        return httpx.Response(200, json=found)

    def _update(self, dataset_id: str, request: httpx.Request) -> httpx.Response:
        found = self.find(dataset_id)
        if found is None:
            return httpx.Response(404, json={"detail": "Dataset not found"})
        body = json.loads(request.content) if request.content else {}
        self.put_calls.append(body)
        answer = dict(found)
        answer.update({key: value for key, value in body.items() if value is not None})
        if not self.keep_update:
            found.update({key: value for key, value in body.items() if value is not None})
        return httpx.Response(200, json=answer)

    def _delete(self, dataset_id: str) -> httpx.Response:
        if self.delete_status != 204:
            return httpx.Response(self.delete_status, json={"detail": "Не удалось удалить"})
        found = self.find(dataset_id)
        if found is None:
            return httpx.Response(404, json={"detail": "Dataset not found"})
        self.deleted.append(dataset_id)
        if not self.keep_after_delete:
            self.datasets = [item for item in self.datasets if str(item["id"]) != dataset_id]
        return httpx.Response(204)

    def _fill(self, dataset_id: str, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.fill_calls.append(body)
        # FastAPI проверяет тело запроса раньше, чем датасет: пустой или неполный
        # `raw_file_ids` даёт 422 даже для несуществующего датасета
        if "raw_file_ids" not in body:
            return httpx.Response(422, json=_validation_error("raw_file_ids", "Field required"))
        if not body.get("raw_file_ids"):
            return httpx.Response(
                422,
                json=_validation_error("raw_file_ids", "List should have at least 1 item"),
            )
        if self.find(dataset_id) is None:
            return httpx.Response(self.unknown_dataset_status, json={"detail": "Dataset not found"})
        if self.fill_error_status:
            return httpx.Response(self.fill_error_status, json={"detail": "fill failed"})
        if self.create_task:
            self.tasks.append(
                {
                    "type": "dataset-fill",
                    "name": "dataset-fill",
                    "id": TASK_ID,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
        return httpx.Response(self.fill_status, json={})

    def _card(self, task_id: str) -> httpx.Response:
        task = next((item for item in self.tasks if str(item["id"]) == task_id), None)
        if task is None:
            return httpx.Response(404, json={"detail": "Task not found"})
        runtime = {
            "task_id": task_id,
            "status": self.task_status,
            "parameters": {"files": 2},
            "id": str(uuid4()),
        }
        return httpx.Response(200, json={**task, "runtimes": [runtime]})


def _validation_error(field: str, message: str) -> dict[str, Any]:
    """Ответ 422 в формате `HTTPValidationError.detail[]` (NFR-2)."""
    return {"detail": [{"loc": ["body", field], "msg": message, "type": "value_error"}]}


def stand(handler: Handler, check_id: str, *, session: Any = None, **params: Any) -> Any:
    """Контекст сценария поверх подменённого транспорта (без реальной сети)."""
    journal = Journal(max_records=200)
    transport = httpx.MockTransport(handler)
    client = ApiHttpClient(
        base_url=BASE_URL, timeout=5.0, transport=LoggingTransport(journal, inner=transport)
    )
    settings = TrainingServerSettings(base_url=BASE_URL, timeout=5.0)
    console = build_console_client(settings, journal, inner=transport)
    apis = Apis.build(client)
    context = check_runner.AutomationContext(
        session=session or new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=apis,
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
        monitor=TaskMonitor(apis.tasks, journal=journal, interval=0.0),
    )
    return context, journal


# ---------------------------------------------------------------------------
# TC-DS-01 — создание датасета
# ---------------------------------------------------------------------------
def test_create_passes_and_cleans_up():
    """Создание: `type`, `creation_date`, подтверждение `GET` и самоочистка (NFR-T4)."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-01")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["type"] == "direct_fill"
    assert outcome.evidence["creation_date"]
    assert outcome.evidence["fetched"]["name"] == outcome.evidence["name"]
    assert outcome.evidence["name"].startswith("__TEST__")
    assert "удалён" in outcome.evidence["cleanup"]
    assert fake.deleted == fake.created == [outcome.evidence["dataset_id"]]
    assert fake.find(outcome.evidence["dataset_id"]) is None
    actions = [item["action"] for item in context.session.counters["test_entities"]]
    assert actions == ["создан", "удалён"]


def test_create_fails_when_cleanup_is_not_done():
    """Стенд не удаляет `__TEST__`-датасет — отказ: мусор на стенде недопустим."""
    fake = FakeDatasets(delete_status=500)
    context, _journal = stand(fake.handler, "TC-DS-01")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "самоочистка не выполнена" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-DS-02 — реестр и фильтры
# ---------------------------------------------------------------------------
def test_list_and_filters_pass():
    """Реестр: `count` согласован, все четыре фильтра работают (UC-12)."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-02")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["count"] == 2
    assert set(outcome.evidence["filters"]) == {"name", "description", "type", "creation_date"}
    assert outcome.evidence["filters"]["name"]["returned"] == 1
    assert outcome.evidence["filters"]["description"]["returned"] == 1
    assert outcome.evidence["filters"]["creation_date"]["returned"] == 1


def test_list_fails_when_name_filter_returns_foreign_datasets():
    """Фильтр `name` вернул весь реестр — отказ проверки."""
    fake = FakeDatasets(break_name_filter=True)
    context, _journal = stand(fake.handler, "TC-DS-02")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "вернул другие датасеты" in outcome.verdict


def test_list_fails_when_count_differs():
    """`count` не совпал с числом элементов — отказ."""
    fake = FakeDatasets(break_count=True)
    context, _journal = stand(fake.handler, "TC-DS-02")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "count=" in outcome.verdict


def test_list_is_skipped_on_empty_registry():
    """Пустой реестр датасетов — «пропущена»: проверять нечего."""
    fake = FakeDatasets(datasets=[])
    context, _journal = stand(fake.handler, "TC-DS-02")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "пуст" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-DS-03 — метаданные и состав датасета
# ---------------------------------------------------------------------------
def test_meta_records_missing_composition_as_note():
    """Состав и агрегаты в API отсутствуют: проверка проходит с замечанием P1."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-03")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["fields"] == list(check_datasets.DATASET_FIELDS)
    assert outcome.evidence["aggregates_in_metadata"] == []
    assert outcome.evidence["server_operation"] is None
    assert outcome.evidence["server_composition_expected"] is False
    assert outcome.evidence["composition_source"] == SOURCE_HEURISTIC
    assert "предполагается" in outcome.verdict and "P1" in outcome.verdict


def test_meta_uses_local_fact_of_fill():
    """Датасет наполняли через пульт: состав — достоверный факт, а не гипотеза."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-03")
    composition.record_fill(
        context.session,
        dataset_id=DATASET_ID,
        raw_file_ids=[RAW_ID],
        markup_file_ids=[MARKUP_ID],
        name="skfu-train",
        check_id="TC-DS-05",
    )

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["composition_source"] == SOURCE_LOCAL
    assert outcome.evidence["composition_files"] == 2
    assert "локальный факт" in outcome.verdict


def test_meta_accepts_server_composition_that_matches_fact(monkeypatch):
    """После устранения P1 проверка сама сверяет серверный состав с фактом пульта."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-03")
    monkeypatch.setattr(
        context.apis.datasets,
        "get_dataset_summary",
        lambda dataset_id: {
            "raw_file_ids": [RAW_ID],
            "markup_file_ids": [MARKUP_ID],
            "records": 1,
        },
        raising=False,
    )
    composition.record_fill(
        context.session,
        dataset_id=DATASET_ID,
        raw_file_ids=[RAW_ID],
        markup_file_ids=[MARKUP_ID],
        name="skfu-train",
        check_id="TC-DS-05",
    )

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["composition_source"] == SOURCE_SERVER
    assert outcome.evidence["comparison"]["match"] is True
    assert "совпал" in outcome.verdict


def test_meta_fails_when_server_composition_differs(monkeypatch):
    """Сервер сообщает не тот состав, который принял `fill`, — дефект API."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-03")
    monkeypatch.setattr(
        context.apis.datasets,
        "get_dataset_summary",
        lambda dataset_id: {"raw_file_ids": [RAW_2]},
        raising=False,
    )
    composition.record_fill(
        context.session,
        dataset_id=DATASET_ID,
        raw_file_ids=[RAW_ID],
        markup_file_ids=[MARKUP_ID],
        name="skfu-train",
        check_id="TC-DS-05",
    )

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не совпал" in outcome.verdict
    assert outcome.evidence["comparison"]["raw"]["only_server"] == [RAW_2]
    assert outcome.evidence["comparison"]["raw"]["only_local"] == [RAW_ID]


# ---------------------------------------------------------------------------
# TC-DS-04 — правка датасета
# ---------------------------------------------------------------------------
def test_update_passes_and_confirms_change():
    """Правка применена, подтверждена `GET`, `updated_at` в схеме нет (замечание)."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-04")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["updated_at_field"] is False
    assert "updated_at" in outcome.verdict
    assert outcome.evidence["after"]["name"].endswith("-upd")
    assert fake.find(outcome.evidence["dataset_id"]) is None


def test_update_fails_when_change_is_not_persisted():
    """Стенд отвечает новыми значениями, но `GET` отдаёт старые — отказ."""
    fake = FakeDatasets(keep_update=True)
    context, _journal = stand(fake.handler, "TC-DS-04")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "правка не сохранена" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-DS-05 — наполнение датасета (тяжёлая проверка)
# ---------------------------------------------------------------------------
def test_fill_finds_task_and_waits_for_terminal_status():
    """`fill` → 202 без `task_id`; задача найдена обходным поиском и отслежена (UC-14)."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-05", task_wait="0", task_timeout="0")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["found"] is True
    assert outcome.evidence["task_id"] == TASK_ID
    assert outcome.evidence["status"] == "completed"
    assert outcome.evidence["pairs"] == 1
    assert outcome.evidence["composition_source"] == SOURCE_LOCAL
    assert fake.fill_calls == [{"raw_file_ids": [RAW_ID], "markup_file_ids": [MARKUP_ID]}]

    stored = composition.composition_of(context.session, outcome.evidence["dataset_id"])
    assert stored is not None
    assert stored.source == SOURCE_LOCAL
    assert stored.task_id == TASK_ID and stored.task_status == "completed"
    assert [task["task_id"] for task in context.session.tasks] == [TASK_ID]
    assert fake.find(outcome.evidence["dataset_id"]) is None


def test_fill_fails_when_task_is_not_found():
    """Наполнение принято, но задачи `dataset-fill` нет — обходной путь P1 не работает."""
    fake = FakeDatasets(create_task=False)
    context, _journal = stand(fake.handler, "TC-DS-05", task_wait="0", task_timeout="0")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не найдена" in outcome.verdict
    assert fake.find(outcome.evidence["dataset_id"]) is None
    assert composition.composition_of(context.session, outcome.evidence["dataset_id"]) is not None


def test_fill_fails_when_task_completed_with_error():
    """Задача дошла до терминального статуса, но не `completed` — отказ."""
    fake = FakeDatasets(task_status="failed")
    context, _journal = stand(fake.handler, "TC-DS-05", task_wait="0", task_timeout="0")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "завершилась со статусом" in outcome.verdict


def test_fill_fails_when_api_rejects_request():
    """Сервер отклонил наполнение — отказ с самоочисткой."""
    fake = FakeDatasets(fill_error_status=500)
    context, _journal = stand(fake.handler, "TC-DS-05", task_wait="0", task_timeout="0")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "наполнение не принято" in outcome.verdict
    assert fake.find(outcome.evidence["dataset_id"]) is None


def test_fill_is_skipped_without_pairs():
    """В реестре нет пар RAW+markup — «пропущена»: наполнять нечем."""
    fake = FakeDatasets(files=[file_payload(RAW_ID, "skfu-record-1.raw.csv")])
    context, _journal = stand(fake.handler, "TC-DS-05", task_wait="0", task_timeout="0")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет пар RAW+markup" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-DS-06 — контракт `fill` на несуществующих идентификаторах
# ---------------------------------------------------------------------------
def test_fill_contract_checks_validation():
    """Валидация отвечает понятной ошибкой, боевой датасет в пробах не участвует."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-06")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    statuses = {label: value["status"] for label, value in outcome.evidence["probes"].items()}
    assert statuses == {
        "unknown_dataset": 404,
        "missing_raw_file_ids": 422,
        "empty_raw_file_ids": 422,
    }
    assert (
        outcome.evidence["error_format"]["missing_raw_file_ids"] == "HTTPValidationError.detail[]"
    )
    assert outcome.evidence["error_format"]["unknown_dataset"] == "detail: строка"
    assert fake.created == [] and fake.deleted == []


def test_fill_contract_fails_on_server_error():
    """Проба получила 5xx — отказ: валидация не должна падать."""
    fake = FakeDatasets(unknown_dataset_status=500)
    context, _journal = stand(fake.handler, "TC-DS-06")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "5xx" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-DS-07 — удаление датасета и самоочистка
# ---------------------------------------------------------------------------
def test_delete_passes_and_confirms_absence():
    """Удаление подтверждено `GET` и реестром; повторное удаление зафиксировано."""
    fake = FakeDatasets()
    context, _journal = stand(fake.handler, "TC-DS-07")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["visible_in_registry"] is True
    assert outcome.evidence["absent_in_registry"] is True
    assert outcome.evidence["count_before"] == outcome.evidence["count_after"] == 2
    assert outcome.evidence["get_after_delete"] == "HTTP 404"
    assert outcome.evidence["repeat_delete"] == "HTTP 404"
    assert fake.find(outcome.evidence["dataset_id"]) is None
    actions = [item["action"] for item in context.session.counters["test_entities"]]
    assert actions == ["создан", "удалён"]


def test_delete_fails_when_dataset_stays_in_registry():
    """Стенд принял удаление, но датасет остался — отказ."""
    fake = FakeDatasets(keep_after_delete=True)
    context, _journal = stand(fake.handler, "TC-DS-07")

    outcome = check_datasets.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "остался в реестре" in outcome.verdict
    assert "не вернулся к исходному" in outcome.verdict
