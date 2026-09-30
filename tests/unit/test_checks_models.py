"""Тесты сценариев модуля «ML models» (TC-MOD, этап T6).

Подменённый стенд повторяет ключевые особенности живого API (`llm_tasks/50`):

    * каталог архитектур отдаётся **JSON-массивом** с `default_config` и
      `suitable_for_phases`;
    * у моделей заполнены `signals` (`signal_num` → `device_id`) и `signal_aliases`,
      поэтому ожидание «`signals` пусты» из `docs/02` §10 фиксируется примечанием;
    * список моделей серверного среза `limit`/`offset` не знает — проба фиксирует
      факт, а не падает;
    * пробы валидации (`TC-MOD-03`) не должны создавать модель; если стенд её
      создал, сценарий обязан удалить её и поставить отказ (NFR-T4).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx

from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import models as check_models
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckSpec, CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
Handler = Callable[[httpx.Request], httpx.Response]

MODEL_ID = "3d2d1139-0000-4000-8000-000000000001"
MODEL_2 = "36940f3b-0000-4000-8000-000000000002"
MODEL_FILE_ID = "5932482e-0000-4000-8000-000000000002"
DEVICE = "Antminer S19"


def spec_of(check_id: str) -> CheckSpec:
    """Описание проверки из каталога (в тестах оно всегда существует)."""
    spec = catalog.find(check_id)
    assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
    return spec


def architecture_payload(name: str, **overrides: Any) -> dict[str, Any]:
    """Элемент каталога архитектур (`ModelArchitectureInfo`)."""
    payload: dict[str, Any] = {
        "architecture": name,
        "description": f"архитектура {name}",
        "default_config": {"architecture": name, "phase_type": "single"},
        "suitable_for_phases": ["single", "three"],
    }
    payload.update(overrides)
    return payload


def model_payload(
    model_id: str,
    name: str,
    *,
    architecture: str = "stft_mlp",
    phase_type: str = "single",
    output_size: int = 2,
    version: str = "stft_mlp.9bbb964f",
    updated_at: str = "2026-09-14T19:08:10",
    metrics: dict[str, Any] | None = None,
    signals: list[dict[str, Any]] | None = None,
    file_id: str | None = MODEL_FILE_ID,
) -> dict[str, Any]:
    """Элемент реестра моделей (`ModelMetadata`)."""
    return {
        "id": model_id,
        "file_id": file_id or str(uuid4()),
        "architecture": architecture,
        "version": version,
        "name": name,
        "description": None,
        "phase_type": phase_type,
        "output_size": output_size,
        "config": {"architecture": architecture, "phase_type": phase_type},
        "metrics": metrics,
        "pruning_mask": None,
        "created_at": "2026-09-14T19:08:10",
        "updated_at": updated_at,
        "signals": signals if signals is not None else [{"signal_num": 0, "device_id": DEVICE}],
        "signal_aliases": {"0": "miner", "1": "not_miner"},
    }


def default_models() -> list[dict[str, Any]]:
    """Реестр стенда: обученная модель и модель без метрик."""
    return [
        model_payload(
            MODEL_ID,
            "stft_mlp-skfu-train-dev (trained)",
            metrics={"epochs": 3, "dataset_id": "5f0a5b7e-0000-4000-8000-000000000001"},
        ),
        model_payload(
            MODEL_2,
            "stft_mlp-fresh",
            updated_at="2026-09-20T10:00:00",
            metrics=None,
        ),
    ]


def default_files() -> list[dict[str, Any]]:
    """Реестр файлов: `.h5` модели-источника (её `file_id` указан в модели)."""
    return [
        {
            "id": MODEL_FILE_ID,
            "file_name": "stft_mlp.h5",
            "size": 178_760,
            "s3_path": "s3://bucket/stft_mlp.h5",
            "import_date": "2026-09-14T19:00:00",
            "file_type": "H5",
        }
    ]


def _validation_error(field: str, message: str) -> dict[str, Any]:
    """Ответ 422 в формате `HTTPValidationError.detail[]` (NFR-2)."""
    return {"detail": [{"loc": ["body", field], "msg": message, "type": "value_error"}]}


DEFAULT_ARCHITECTURES: list[dict[str, Any]] = [
    architecture_payload("stft_mlp"),
    architecture_payload("cnn_1d", suitable_for_phases=["three"]),
]


#: Содержимое `.h5`, которое отдаёт подменённый стенд (по факту — HDF5-магия).
H5_BYTES = b"\x89HDF\r\n\x1a\n" + b"__TEST__h5" * 8


def _metadata_from_body(body: bytes) -> dict[str, Any]:
    """Достаёт JSON-строку `metadata` из multipart-тела загрузки модели."""
    text = body.decode("utf-8", errors="replace")
    start = text.index('name="metadata"')
    start = text.index("\r\n\r\n", start) + 4
    end = text.index("\r\n", start)
    parsed = json.loads(text[start:end])
    assert isinstance(parsed, dict)
    return parsed


def _file_name_from_body(body: bytes) -> str:
    """Имя файла из multipart-тела загрузки модели (часть `file`)."""
    text = body.decode("utf-8", errors="replace")
    marker = 'name="file"'
    start = text.index(marker)
    start = text.index('filename="', start) + len('filename="')
    return text[start : text.index('"', start)]


class FakeModels:
    """Подменённый сервер моделей: каталог, реестр, карточка, создание, удаление.

    Флаги конструктора включают варианты поведения живого стенда, которые проверяются
    сценариями (`break_name_filter`, `break_count`, `limit_honoured`,
    `accept_unknown_architecture`, `accept_foreign_device`, `drop_file_id`).
    """

    def __init__(
        self,
        *,
        architectures: list[dict[str, Any]] | None = None,
        models: list[dict[str, Any]] | None = None,
        files: list[dict[str, Any]] | None = None,
        limit_honoured: bool = False,
        break_name_filter: bool = False,
        break_count: bool = False,
        accept_unknown_architecture: bool = False,
        accept_foreign_device: bool = False,
        drop_file_id: bool = False,
        delete_status: int = 200,
        signals_in_list: bool = True,
        break_device_filter: bool = False,
        fail_device_filter_status: int = 0,
        delete_file_cascade: bool = True,
        upload_status: int = 201,
        upload_without_file_id: bool = False,
        file_delete_status: int = 200,
        drop_created_model: bool = False,
        repeat_delete_success: bool = False,
    ) -> None:
        self.architectures = [
            dict(item)
            for item in (DEFAULT_ARCHITECTURES if architectures is None else architectures)
        ]
        self.models = [dict(item) for item in (default_models() if models is None else models)]
        self.files = [dict(item) for item in (default_files() if files is None else files)]
        self.limit_honoured = limit_honoured
        self.break_name_filter = break_name_filter
        self.break_count = break_count
        self.accept_unknown_architecture = accept_unknown_architecture
        self.accept_foreign_device = accept_foreign_device
        self.drop_file_id = drop_file_id
        self.delete_status = delete_status
        #: Живой стенд не отдаёт `signals` в списке — только в карточке модели.
        self.signals_in_list = signals_in_list
        #: Сервер игнорирует `device_id` (негативный вариант для TC-MOD-05).
        self.break_device_filter = break_device_filter
        #: Сервер отвечает ошибкой на фильтр `device_id` (факт живого стенда: 500).
        self.fail_device_filter_status = fail_device_filter_status
        #: Удаление модели убирает её файл (каскад) — иначе файл чистится отдельно.
        self.delete_file_cascade = delete_file_cascade
        self.upload_status = upload_status
        self.upload_without_file_id = upload_without_file_id
        self.file_delete_status = file_delete_status
        #: Дефект: модель «исчезает» из реестра сразу после создания (проверка «осталась»).
        self.drop_created_model = drop_created_model
        #: Повторное удаление отсутствующей модели отвечает успехом (идемпотентность).
        self.repeat_delete_success = repeat_delete_success
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.deleted_files: list[str] = []
        self.uploads: list[str] = []

    # -- маршруты ------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого сервера (каталог, модели, файлы модели)."""
        path = request.url.path
        if path == "/api/ml_models/architectures":
            return httpx.Response(200, json=self.architectures)
        if path == "/api/ml_models/models/upload" and request.method == "POST":
            return self._upload(request)
        if path == "/api/ml_models/models":
            if request.method == "POST":
                return self._create(request)
            return self._list(request)
        if path.startswith("/api/ml_models/models/"):
            model_id = path.rsplit("/", 1)[-1]
            if request.method == "GET":
                return self._card(model_id)
            if request.method == "DELETE":
                return self._delete(model_id)
        if path == "/api/data/files":
            return self._files(request)
        if path.startswith("/api/data/file/") and path.endswith("/download"):
            return self._file_download(path.split("/")[4])
        if request.method == "DELETE" and path.startswith("/api/") and path.count("/") == 2:
            return self._delete_file(path.rsplit("/", 1)[-1])
        return httpx.Response(404, json={"detail": "Not Found"})

    # -- файлы модели --------------------------------------------------------
    def _files(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        rows = list(self.files)
        if params.get("id"):
            rows = [item for item in rows if str(item["id"]) == params["id"]]
        return httpx.Response(200, json={"files": rows, "count": len(rows)})

    def _file_download(self, file_id: str) -> httpx.Response:
        found = next((item for item in self.files if str(item["id"]) == file_id), None)
        if found is None:
            return httpx.Response(404, json={"detail": "File not found"})
        return httpx.Response(
            200,
            content=H5_BYTES,
            headers={
                "content-type": "application/octet-stream",
                "content-disposition": f'attachment; filename="{found["file_name"]}"',
            },
        )

    def _delete_file(self, file_id: str) -> httpx.Response:
        if self.file_delete_status != 200:
            return httpx.Response(self.file_delete_status, json={"detail": "Не удалось удалить"})
        self.files = [item for item in self.files if str(item["id"]) != file_id]
        self.deleted_files.append(file_id)
        return httpx.Response(200, json={"message": "File deleted successfully"})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        if self.upload_status != 201:
            return httpx.Response(self.upload_status, json={"detail": "Upload rejected"})
        metadata = _metadata_from_body(body)
        file_name = _file_name_from_body(body)
        self.uploads.append(file_name)
        new_file_id = str(uuid4())
        self.files.append(
            {
                "id": new_file_id,
                "file_name": file_name,
                "size": len(body),
                "s3_path": f"s3://bucket/{file_name}",
                "import_date": "2026-09-30T12:00:00",
                "file_type": "H5",
            }
        )
        row = model_payload(
            str(uuid4()),
            str(metadata.get("name") or ""),
            architecture=str(metadata.get("architecture") or "stft_mlp"),
            phase_type=str(metadata.get("phase_type") or "single"),
            output_size=int(metadata.get("output_size") or 2),
            metrics=None,
            signals=[],
            file_id=new_file_id,
        )
        self.models.append(row)
        self.created.append(str(row["id"]))
        return httpx.Response(201, json=row)

    # -- реализация ----------------------------------------------------------
    def find(self, model_id: str) -> dict[str, Any] | None:
        """Модель реестра по идентификатору."""
        return next((item for item in self.models if str(item["id"]) == model_id), None)

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        rows = list(self.models)
        if params.get("model_id"):
            rows = [item for item in rows if str(item["id"]) == params["model_id"]]
        if params.get("architecture"):
            rows = [item for item in rows if str(item["architecture"]) == params["architecture"]]
        if params.get("version"):
            rows = [item for item in rows if str(item["version"]) == params["version"]]
        if params.get("name") and not self.break_name_filter:
            rows = [item for item in rows if str(params["name"]) in str(item["name"])]
        if params.get("phase_type"):
            rows = [item for item in rows if str(item["phase_type"]) == params["phase_type"]]
        if params.get("output_size"):
            rows = [item for item in rows if int(item["output_size"]) == int(params["output_size"])]
        if params.get("device_id"):
            if self.fail_device_filter_status:
                return httpx.Response(self.fail_device_filter_status, text="Internal Server Error")
            if not self.break_device_filter:
                rows = [
                    item
                    for item in rows
                    if any(
                        str(signal.get("device_id")) == params["device_id"]
                        for signal in (item.get("signals") or [])
                    )
                ]
        if self.limit_honoured and params.get("limit"):
            rows = rows[: int(params["limit"])]
        count = len(rows) + (5 if self.break_count else 0)
        payload = [dict(item) for item in rows]
        if not self.signals_in_list:
            # список моделей `signals` не отдаёт (факт живого стенда): связь `signal_num`
            # → `device_id` доступна только в карточке `GET /api/ml_models/models/{id}`
            for row in payload:
                row["signals"] = []
        return httpx.Response(200, json={"models": payload, "count": count})

    def _card(self, model_id: str) -> httpx.Response:
        found = self.find(model_id)
        if found is None:
            return httpx.Response(404, json={"detail": "Model not found"})
        if self.drop_file_id:
            return httpx.Response(200, json={**found, "file_id": None})
        return httpx.Response(200, json=found)

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        for field in ("architecture", "name", "output_size"):
            if not body.get(field):
                return httpx.Response(422, json=_validation_error(field, "Field required"))
        known = {str(item["architecture"]) for item in self.architectures}
        if body.get("architecture") not in known and not self.accept_unknown_architecture:
            return httpx.Response(
                422, json=_validation_error("architecture", "Unknown architecture")
            )
        signals = body.get("signals") or {}
        if signals and not self.accept_foreign_device:
            return httpx.Response(422, json=_validation_error("signals", "Unknown device_id"))
        row = model_payload(
            str(uuid4()),
            str(body["name"]),
            architecture=str(body["architecture"]),
            phase_type=str(body.get("phase_type") or "single"),
            output_size=int(body["output_size"]),
            metrics=None,
            signals=[],
            file_id=str(uuid4()),
        )
        self.created.append(str(row["id"]))
        # сервер создаёт и файл модели: у модели есть `file_id` в реестре файлов
        self.files.append(
            {
                "id": str(row["file_id"]),
                "file_name": f"{row['name']}.h5",
                "size": 4096,
                "s3_path": f"s3://bucket/{row['name']}.h5",
                "import_date": "2026-09-30T12:00:00",
                "file_type": "H5",
            }
        )
        if self.drop_created_model:
            return httpx.Response(201, json=row)
        self.models.append(row)
        return httpx.Response(201, json=row)

    def _delete(self, model_id: str) -> httpx.Response:
        if self.delete_status != 200:
            return httpx.Response(self.delete_status, json={"detail": "Не удалось удалить"})
        found = self.find(model_id)
        if found is None:
            if self.repeat_delete_success:
                return httpx.Response(200, json={"message": "Model deleted successfully"})
            return httpx.Response(404, json={"detail": "Model not found"})
        self.deleted.append(model_id)
        self.models = [item for item in self.models if str(item["id"]) != model_id]
        file_id = str(found.get("file_id") or "")
        if file_id and self.delete_file_cascade:
            self.files = [item for item in self.files if str(item["id"]) != file_id]
            self.deleted_files.append(file_id)
        return httpx.Response(200, json={"message": "Model deleted successfully"})


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
    return check_runner.AutomationContext(
        session=session or new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=apis,
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
    )


# ---------------------------------------------------------------------------
# TC-MOD-01 — каталог архитектур
# ---------------------------------------------------------------------------
def test_architectures_pass():
    """Каталог отдан массивом, поля заполнены — проверка пройдена (UC-15)."""
    context = stand(FakeModels().handler, "TC-MOD-01")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["count"] == 2
    assert outcome.evidence["architectures"] == ["stft_mlp", "cnn_1d"]
    assert outcome.evidence["empty_fields"] == []
    assert "stft_mlp" in outcome.verdict


def test_architectures_fail_when_fields_are_empty():
    """Пустой `default_config` в каталоге — отказ: по нему создаётся модель."""
    fake = FakeModels(architectures=[architecture_payload("stft_mlp", default_config={})])
    context = stand(fake.handler, "TC-MOD-01")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "default_config" in outcome.verdict


def test_architectures_skipped_on_empty_catalog():
    """Пустой каталог — «пропущена»: проверять нечего."""
    context = stand(FakeModels(architectures=[]).handler, "TC-MOD-01")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "пуст" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-MOD-03 — валидация архитектуры и signals
# ---------------------------------------------------------------------------
def test_create_validation_rejects_probes_without_creating_model():
    """Три пробы отклонены, модель не создана и не удаляется (BR-F4)."""
    fake = FakeModels()
    context = stand(fake.handler, "TC-MOD-03")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert [row["case"] for row in outcome.evidence["cases"]] == [
        "неизвестная архитектура",
        "signals на несуществующий device_id",
        "без обязательных полей",
    ]
    assert {row["status"] for row in outcome.evidence["cases"]} == {422}
    assert fake.created == [] and fake.deleted == []
    assert "модель не создаётся" in outcome.verdict


def test_create_validation_cleans_up_accepted_model():
    """Стенд принял неизвестную архитектуру: модель удалена, проверка — отказ (NFR-T4)."""
    fake = FakeModels(accept_unknown_architecture=True)
    context = stand(fake.handler, "TC-MOD-03")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "принято сервером" in outcome.verdict
    created = fake.created
    assert created and fake.deleted == created
    assert fake.models and all(str(item["id"]) not in created for item in fake.models)
    first = outcome.evidence["cases"][0]
    assert first["cleanup"] == "модель удалена"


def test_create_validation_reports_failed_cleanup():
    """Модель создана, но не удаляется — отказ с текстом неудачи уборки."""
    fake = FakeModels(accept_unknown_architecture=True, delete_status=500)
    context = stand(fake.handler, "TC-MOD-03")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "удалить не удалось" in str(outcome.evidence["cases"][0]["cleanup"])


# ---------------------------------------------------------------------------
# TC-MOD-05 — список моделей и фильтры
# ---------------------------------------------------------------------------
def test_list_filters_pass_with_device_id():
    """Шесть фильтров реестра и `device_id` из `signals` вернули только своих (UC-18)."""
    context = stand(FakeModels().handler, "TC-MOD-05")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    filters = [row["filter"] for row in outcome.evidence["filters"]]
    assert filters == [
        "model_id",
        "architecture",
        "version",
        "name",
        "phase_type",
        "output_size",
        "device_id",
    ]
    assert all(row["foreign"] == 0 for row in outcome.evidence["filters"])
    assert outcome.evidence["count"] == 2
    assert "7 из 7" in outcome.verdict


def test_list_filters_takes_device_id_from_card():
    """`device_id` берётся из карточки: список моделей `signals` не отдаёт (живой стенд)."""
    context = stand(FakeModels(signals_in_list=False, limit_honoured=True).handler, "TC-MOD-05")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["device_id"] == DEVICE
    assert [row["filter"] for row in outcome.evidence["filters"]][-1] == "device_id"
    assert "7 из 7" in outcome.verdict
    assert outcome.note == ""


def test_list_filters_notes_missing_device_bindings():
    """Привязок `signals` нет нигде — фильтр `device_id` не проверен, это примечание."""
    models = [model_payload(MODEL_ID, "fresh", signals=[])]
    context = stand(FakeModels(models=models, signals_in_list=False).handler, "TC-MOD-05")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["device_id"] == ""
    assert "не проверен" in outcome.note
    assert "6 из 6" in outcome.verdict


def test_list_filters_fail_when_device_filter_is_ignored():
    """Сервер игнорирует `device_id`: среди найденных — модели без привязки → отказ."""
    models = [
        model_payload(MODEL_ID, "trained", metrics={"epochs": 3}),
        model_payload(MODEL_2, "fresh", signals=[], updated_at="2026-09-20T10:00:00"),
    ]
    context = stand(
        FakeModels(models=models, break_device_filter=True, signals_in_list=False).handler,
        "TC-MOD-05",
    )

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "без привязки" in outcome.verdict
    device_row = next(row for row in outcome.evidence["filters"] if row["filter"] == "device_id")
    assert device_row["foreign"] == 1, "проверка по карточкам нашла чужую модель"
    assert device_row["checked_by_card"] == 2


def test_list_filters_fail_when_device_filter_answers_500():
    """Живой дефект: `device_id` отвечает 500 — отказ с кодом и текстом сервера."""
    context = stand(
        FakeModels(fail_device_filter_status=500, signals_in_list=False).handler, "TC-MOD-05"
    )

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "device_id не отвечает" in outcome.verdict
    device_row = next(row for row in outcome.evidence["filters"] if row["filter"] == "device_id")
    assert device_row["status"] == 500
    assert "Internal Server Error" in device_row["error"]


def test_list_filters_fail_when_filter_returns_foreign_models():
    """Фильтр `name` вернул чужие модели — отказ проверки."""
    context = stand(FakeModels(break_name_filter=True).handler, "TC-MOD-05")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не подходящих под условие" in outcome.verdict


def test_list_filters_fail_when_count_differs():
    """`count` списка не совпал с числом моделей — отказ."""
    context = stand(FakeModels(break_count=True).handler, "TC-MOD-05")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "`count`=7" in outcome.verdict


def test_list_filters_note_when_limit_is_ignored():
    """Серверного среза нет: `limit=1` игнорируется и это примечание, а не отказ."""
    context = stand(FakeModels(limit_honoured=False).handler, "TC-MOD-05")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "проигнорирован" in outcome.note
    assert outcome.evidence["limit_returned"] == 2


def test_list_filters_respect_honoured_limit():
    """Если сервер срез поддерживает, примечания о `limit` нет."""
    context = stand(FakeModels(limit_honoured=True).handler, "TC-MOD-05")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.note == ""
    assert outcome.evidence["limit_returned"] == 1


def test_list_filters_skipped_on_empty_registry():
    """Пустой реестр моделей — «пропущена»."""
    context = stand(FakeModels(models=[]).handler, "TC-MOD-05")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "пуст" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-MOD-06 — карточка модели и метрики
# ---------------------------------------------------------------------------
def test_card_passes_and_closes_signals_defect():
    """Карточка обученной модели: метрики заполнены, `signals` не пусты (замечание закрыто)."""
    context = stand(FakeModels().handler, "TC-MOD-06")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["model_id"] == MODEL_ID
    assert outcome.evidence["metrics_filled"] is True
    assert outcome.evidence["signals"] == 1
    assert outcome.evidence["missing"] == []
    assert "закрыто" in outcome.note
    assert "epochs" in outcome.evidence["metrics_keys"]


def test_card_notes_empty_signals_and_metrics():
    """Модель без обучения: метрики пусты, `signals` пусты — два примечания."""
    models = [model_payload(MODEL_2, "fresh", metrics=None, signals=[])]
    context = stand(FakeModels(models=models).handler, "TC-MOD-06")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["metrics_filled"] is False
    assert outcome.evidence["signals"] == 0
    assert "метрики пусты" in outcome.note
    assert "остаётся" in outcome.note


def test_card_fails_when_card_is_incomplete():
    """Карточка без `file_id` — отказ: по ней нельзя проверить файл модели."""
    context = stand(FakeModels(drop_file_id=True).handler, "TC-MOD-06")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "file_id" in outcome.verdict


def test_card_uses_operator_choice():
    """`model_id` из параметров проверки задаёт модель явно."""
    context = stand(FakeModels().handler, "TC-MOD-06", model_id=MODEL_2)

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["model_id"] == MODEL_2


def test_card_skipped_without_models():
    """Реестр моделей пуст — «пропущена»: проверять нечего."""
    context = stand(FakeModels(models=[]).handler, "TC-MOD-06")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED


# ---------------------------------------------------------------------------
# TC-MOD-02 — создание модели с самоочисткой
# ---------------------------------------------------------------------------
def test_create_passes_and_cleans_up():
    """Модель создана по архитектуре каталога, проверена реестром и удалена (NFR-T4)."""
    fake = FakeModels()
    context = stand(fake.handler, "TC-MOD-02")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["name"].startswith("__TEST__TC_MOD_02")
    assert outcome.evidence["architecture"] == "stft_mlp"
    assert outcome.evidence["created_architecture"] == "stft_mlp"
    assert outcome.evidence["created_phase_type"] == "single"
    assert outcome.evidence["cleanup"] == "модель удалена"
    assert outcome.evidence["left_in_registry"] is False
    assert fake.created == fake.deleted == [outcome.evidence["model_id"]]
    actions = [item["action"] for item in context.session.counters["test_entities"]]
    assert actions == ["создан", "удалён"]


def test_create_skipped_without_architecture_catalog():
    """Пустой каталог архитектур — «пропущена»: архитектуру выбрать нечем."""
    context = stand(FakeModels(architectures=[]).handler, "TC-MOD-02")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "каталог архитектур пуст" in outcome.verdict


def test_create_fails_when_cleanup_is_not_done():
    """Модель не удаляется — отказ: `__TEST__`-сущность осталась на стенде."""
    fake = FakeModels(delete_status=500)
    context = stand(fake.handler, "TC-MOD-02")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "самоочистка не выполнена" in outcome.verdict
    assert outcome.evidence["left_in_registry"] is True


def test_create_fails_when_model_is_not_in_registry():
    """Созданной модели нет в реестре — отказ с доказательством по ответу."""
    fake = FakeModels(drop_created_model=True)
    context = stand(fake.handler, "TC-MOD-02")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не видна в реестре" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-MOD-04 — загрузка готовой модели (`.h5` + metadata)
# ---------------------------------------------------------------------------
def test_upload_passes_and_cleans_up_model_and_file():
    """`.h5` загружен, модель создана и удалена; связанный файл убран (NFR-T4)."""
    fake = FakeModels()
    context = stand(fake.handler, "TC-MOD-04")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["uploaded_bytes"] == len(H5_BYTES)
    assert outcome.evidence["file_name"] == "stft_mlp.h5"
    assert outcome.evidence["in_registry"] is True
    assert outcome.evidence["cleanup"] == "модель удалена"
    # каскад: отдельная уборка файла не потребовалась, в реестре его нет
    assert "file_cleanup" not in outcome.evidence
    assert all(str(item["id"]) != outcome.evidence["model_file_id"] for item in fake.files)
    assert fake.uploads == ["stft_mlp.h5"]


def test_upload_cleans_file_when_there_is_no_cascade():
    """Каскада нет: файл загруженной модели удаляется сценарием отдельно."""
    fake = FakeModels(delete_file_cascade=False)
    context = stand(fake.handler, "TC-MOD-04")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["file_cleanup"] == "файл удалён"
    assert outcome.evidence["file_left_in_registry"] is False
    assert outcome.evidence["model_file_id"] in fake.deleted_files


def test_upload_fails_when_file_cleanup_is_not_done():
    """Файл не удаляется — отказ: след `.h5` на стенде недопустим."""
    fake = FakeModels(delete_file_cascade=False, file_delete_status=500)
    context = stand(fake.handler, "TC-MOD-04")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "файл `.h5`" in outcome.verdict


def test_upload_fails_when_server_rejects_upload():
    """Сервер отклонил загрузку — отказ с текстом сервера, убирать нечего."""
    fake = FakeModels(upload_status=400)
    context = stand(fake.handler, "TC-MOD-04")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "загрузка модели не выполнена" in outcome.verdict
    assert outcome.evidence["cleanup"] == "нечего удалять: модель не создана"
    assert fake.uploads == [] and fake.created == []


def test_upload_skipped_when_source_file_is_missing():
    """Файла модели нет в реестре — «пропущена»: скачать образец нечего."""
    models = [model_payload(MODEL_ID, "no-file", file_id=None)]
    context = stand(FakeModels(models=models, files=[]).handler, "TC-MOD-04")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "не скачан" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-MOD-07 — удаление модели и судьба её файла
# ---------------------------------------------------------------------------
def test_delete_reports_cascade_and_second_delete():
    """Удаление модели убрало её файл (каскад); карточка отвечает 404, повтор — 404."""
    fake = FakeModels()
    context = stand(fake.handler, "TC-MOD-07")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["delete_status"] == 200
    assert outcome.evidence["gone_from_registry"] is True
    assert outcome.evidence["card_after_delete"] == 404
    assert outcome.evidence["file_cascade"] is True
    assert outcome.evidence["second_delete_status"] == 404
    assert outcome.note == ""
    assert "файл `.h5` удалён вместе с моделью" in outcome.verdict
    assert fake.deleted == [outcome.evidence["model_id"]]


def test_delete_cleans_file_when_there_is_no_cascade():
    """Файл не удаляется вместе с моделью — сценарий убирает его и фиксирует это."""
    fake = FakeModels(delete_file_cascade=False)
    context = stand(fake.handler, "TC-MOD-07")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["file_cascade"] is False
    assert outcome.evidence["file_cleanup"] == "файл удалён"
    assert outcome.evidence["model_file_id"] in fake.deleted_files
    assert "удаляется отдельно" in outcome.verdict


def test_delete_fails_when_file_cleanup_is_not_done():
    """Файл модели не удаляется ни каскадом, ни вручную — отказ."""
    fake = FakeModels(delete_file_cascade=False, file_delete_status=500)
    context = stand(fake.handler, "TC-MOD-07")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "остался в реестре файлов" in outcome.verdict


def test_delete_fails_when_model_survives():
    """Модель не удаляется и остаётся в реестре — отказ."""
    fake = FakeModels(delete_status=500)
    context = stand(fake.handler, "TC-MOD-07")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "удаление модели отклонено сервером" in outcome.verdict
    assert outcome.evidence["gone_from_registry"] is False


def test_delete_notes_idempotent_second_delete():
    """Повторное удаление с успехом — примечание: идемпотентность не доказана."""
    fake = FakeModels(repeat_delete_success=True)
    context = stand(fake.handler, "TC-MOD-07")

    outcome = check_models.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["second_delete_status"] == 200
    assert "идемпотентность" in outcome.note
