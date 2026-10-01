"""Тесты сценария `TC-TR-01` — контракт обучения без расхода ресурсов (этап T7).

Подменённый стенд повторяет поведение живого API: тело `train` проверяется схемой
(диапазоны `epochs`/`batch_size`/`learning_rate`/аугментации) **до** обращения к
датасету, поэтому все пробы идут с несуществующим `dataset_id` и обучения не
запускают. Флаг `accept_all` проверяет обратную ветку: если сервер всё же принял
запрос, сценарий ставит отказ и сохраняет `task_id` для прерывания задачи.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import runner as check_runner
from acceptance.checks import training as check_training
from acceptance.checks.registry import CheckSpec, CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import ARTIFACT_DELETED, ARTIFACT_KEPT, new_session
from acceptance.tasks_monitor import TaskMonitor
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
Handler = Callable[[httpx.Request], httpx.Response]

MODEL_ID = "3d2d1139-0000-4000-8000-000000000001"
DATASET_ID = "5f0a5b7e-0000-4000-8000-000000000001"

#: Модель-результат обучения: появляется в реестре после задачи `training`.
TRAINED_ID = "88888888-0000-4000-8000-000000000008"

#: Идентификатор задачи стенда: клиент проверяет его как UUID, поэтому он «настоящий».
TASK_UUID = "77777777-0000-4000-8000-000000000007"

#: Идентификатор прогона задачи (`runtimes[].id`) — тоже UUID по схеме.
RUNTIME_UUID = "66666666-0000-4000-8000-000000000006"


def spec_of(check_id: str) -> CheckSpec:
    """Описание проверки из каталога (в тестах оно всегда существует)."""
    spec = catalog.find(check_id)
    assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
    return spec


def model_payload(*, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    """Модель реестра (`ModelMetadata`); обученная — с заполненными `metrics`."""
    return {
        "id": MODEL_ID,
        "file_id": "5932482e-0000-4000-8000-000000000002",
        "architecture": "stft_mlp",
        "version": "stft_mlp.9bbb964f",
        "name": "stft_mlp-skfu-train-dev (trained)",
        "description": None,
        "phase_type": "single",
        "output_size": 2,
        "config": {"architecture": "stft_mlp"},
        "metrics": {"epochs": 3} if metrics is None else metrics,
        "pruning_mask": None,
        "created_at": "2026-09-14T19:08:10",
        "updated_at": "2026-09-14T19:08:10",
        "signals": [],
        "signal_aliases": {"0": "miner"},
    }


def _validation_error(field: str, message: str) -> dict[str, Any]:
    """Ответ 422 в формате `HTTPValidationError.detail[]` (NFR-2)."""
    return {"detail": [{"loc": ["body", field], "msg": message, "type": "value_error"}]}


def _train_response(accept_all: bool, body: dict[str, Any]) -> httpx.Response:
    """Ответ 202 на запуск обучения: задача и параметры запуска."""
    return httpx.Response(
        202,
        json={
            "status": "accepted",
            "model_id": MODEL_ID,
            "dataset_id": str(body.get("dataset_id") or DATASET_ID),
            "task_id": TASK_UUID,
            "batch_size": int(body.get("batch_size") or 32),
            "epochs": int(body.get("epochs") or 1),
            "learning_rate": float(body.get("learning_rate") or 1e-06),
            "trained_model_name": body.get("trained_model_name"),
            "preprocessing_converter": "direct",
            "validation_info": {"epochs": 3},
            "message": "Обучение запущено",
            "augmentation": {},
        },
    )


#: Содержимое ONNX в подменённом стенде: protobuf-подобное начало и домен `ai.onnx`.
ONNX_BYTES = b"\x08\x07\x12\x0bai.onnx.pytorch" + b"\x00" * 500


class FakeTraining:
    """Подменённый стенд: реестр моделей и `train` с проверкой диапазонов."""

    def __init__(
        self,
        *,
        models: list[dict[str, Any]] | None = None,
        datasets: tuple[str, ...] = (DATASET_ID,),
        accept_all: bool = False,
        onnx_content: bytes | None = None,
        onnx_file_name: str | None = "stft_mlp.onnx",
        onnx_status: int = 200,
        onnx_headers: dict[str, str] | None = None,
        create_trained_model: bool = True,
    ) -> None:
        self.models = [dict(item) for item in (models if models is not None else [model_payload()])]
        self.datasets = datasets
        self.accept_all = accept_all
        self.onnx_content = ONNX_BYTES if onnx_content is None else onnx_content
        self.onnx_file_name = onnx_file_name
        self.onnx_status = onnx_status
        self.onnx_headers = (
            {
                "content-type": "application/octet-stream",
                "content-length": "512",
                "accept-ranges": "bytes",
            }
            if onnx_headers is None
            else onnx_headers
        )
        self.calls: list[dict[str, Any]] = []
        #: Тип и статус задачи: наблюдаемые проверки читают карточку `GET /api/tasks/{id}`.
        self.task_type = "training"
        self.task_status = "completed"
        self.commands: list[str] = []
        self.deleted: list[str] = []
        #: Создавать ли модель-результат обучения (её ищут `TC-TR-02/04`).
        self.create_trained_model = create_trained_model

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого сервера: модели, датасеты, задачи и команды FSM-1."""
        path = request.url.path
        if path == "/api/ml_models/models":
            return httpx.Response(200, json={"models": self.models, "count": len(self.models)})
        if path == "/api/datasets/":
            return httpx.Response(
                200,
                json={
                    "datasets": [
                        {
                            "id": DATASET_ID,
                            "creation_date": "2026-09-01T10:00:00",
                            "name": "skfu-train",
                            "description": None,
                            "type": "direct_fill",
                        }
                    ],
                    "count": 1,
                },
            )
        if path.startswith("/api/tasks/"):
            return self._task(request)
        if path == "/api/data/files":
            return httpx.Response(200, json={"files": [], "count": 0})
        if path.endswith("/download_onnx") and request.method == "GET":
            return self._onnx()
        if path.endswith("/train") and request.method == "POST":
            return self._train(request)
        if path.endswith("/check") and request.method == "POST":
            return self._async_start("model-testing")
        if path.endswith("/inference") and request.method == "POST":
            return self._async_start("inference")
        if path.startswith("/api/ml_models/models/") and request.method == "GET":
            return self._model_card(path.rsplit("/", 1)[-1])
        if request.method == "DELETE":
            entity_id = path.rsplit("/", 1)[-1]
            self.deleted.append(entity_id)
            self.models = [row for row in self.models if str(row["id"]) != entity_id]
            return httpx.Response(200, json={"message": "deleted"})
        return httpx.Response(404, json={"detail": "Not Found"})

    def _task(self, request: httpx.Request) -> httpx.Response:
        """Карточка задачи и команды FSM-1 (`GET/POST /api/tasks/{task_id}[/action]`)."""
        parts = request.url.path.split("/")
        task_id = parts[3]
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "task_id": task_id,
                    "type": self.task_type,
                    "name": self.task_type,
                    "description": "",
                    "created_at": "2026-09-30T12:00:00",
                    "id": task_id,
                    "runtimes": [
                        {
                            "task_id": task_id,
                            "status": self.task_status,
                            "parameters": {},
                            "id": RUNTIME_UUID,
                        }
                    ],
                },
            )
        action = parts[-1]
        self.commands.append(action)
        self.task_status = {
            "pause": "paused",
            "resume": "running",
            "interrupt": "interrupted",
        }.get(action, self.task_status)
        return httpx.Response(
            200, json={"status": "ok", "message": f"{action}: {self.task_status}"}
        )

    def _async_start(self, task_type: str) -> httpx.Response:
        """Ответ 202 асинхронной операции проверки модели или инференса."""
        self.task_type = task_type
        return httpx.Response(
            202,
            json={
                "status": "accepted",
                "model_id": MODEL_ID,
                "dataset_id": DATASET_ID,
                "task_id": TASK_UUID,
                "threshold": 0.5,
                "message": f"{task_type} запущен",
                "validation_info": {"samples": 10},
            },
        )

    def _model_card(self, model_id: str) -> httpx.Response:
        """Карточка модели: результат обучения появляется в реестре после задачи."""
        found = next((item for item in self.models if str(item["id"]) == model_id), None)
        if found is None:
            return httpx.Response(404, json={"detail": "Model not found"})
        return httpx.Response(200, json=found)

    def _onnx(self) -> httpx.Response:
        if self.onnx_status != 200:
            return httpx.Response(self.onnx_status, json={"detail": "ONNX export failed"})
        headers = dict(self.onnx_headers)
        if self.onnx_file_name:
            headers["content-disposition"] = f'attachment; filename="{self.onnx_file_name}"'
        return httpx.Response(self.onnx_status, content=self.onnx_content, headers=headers)

    def _train(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append(body)
        if self.accept_all:
            if self.create_trained_model and body.get("trained_model_name"):
                self.models.append(
                    {
                        **model_payload(metrics=None),
                        "id": TRAINED_ID,
                        "name": str(body["trained_model_name"]),
                        "version": "stft_mlp.new0001",
                        "metrics": {"epochs": int(body.get("epochs") or 3), "accuracy": 0.98},
                        "updated_at": "2026-09-30T12:10:00",
                    }
                )
            return _train_response(True, body)
        bounds = (
            ("epochs", 1, 1000, "Input should be greater than or equal to 1"),
            ("batch_size", 1, 256, "Input should be less than or equal to 256"),
            ("augmentation_num_samples", 1, 10, "Input should be less than or equal to 10"),
        )
        for field, low, high, message in bounds:
            value = body.get(field)
            if value is not None and not low <= int(value) <= high:
                return httpx.Response(422, json=_validation_error(field, message))
        rate = body.get("learning_rate")
        if rate is not None and not 0 < float(rate) <= 1:
            return httpx.Response(
                422, json=_validation_error("learning_rate", "Input should be > 0")
            )
        noise = body.get("augmentation_noise_percentage")
        if noise is not None and not 0.001 <= float(noise) <= 0.05:
            return httpx.Response(
                422, json=_validation_error("augmentation_noise_percentage", "out of range")
            )
        if str(body.get("dataset_id")) not in self.datasets:
            return httpx.Response(404, json={"detail": "Dataset not found"})
        return httpx.Response(202, json={"task_id": "task-1", "status": "accepted"})


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
        monitor=TaskMonitor(apis.tasks, journal=journal, interval=0.0),
    )


def test_contract_rejects_all_invalid_bodies():
    """11 проб (датасет + десять границ) отклонены, задача обучения не создана (UC-21)."""
    fake = FakeTraining()
    context = stand(fake.handler, "TC-TR-01")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert len(outcome.evidence["cases"]) == 11
    assert len(outcome.evidence["range_cases"]) == 10
    assert outcome.evidence["cases"][0]["case"] == "несуществующий dataset_id"
    assert outcome.evidence["cases"][0]["status"] == 404
    assert {row["status"] for row in outcome.evidence["cases"][1:]} == {422}
    assert "задача обучения не создана" in outcome.verdict
    # пробы уходят с несуществующим датасетом — обучение невозможно даже при дефекте
    assert {str(call["dataset_id"]) for call in fake.calls} == {check_training.UNKNOWN_DATASET}


def test_contract_fails_and_reports_task_when_training_started():
    """Стенд принял неверное тело: отказ и `task_id` для прерывания задачи."""
    fake = FakeTraining(accept_all=True)
    context = stand(fake.handler, "TC-TR-01")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "принято сервером" in outcome.verdict
    assert outcome.evidence["started_tasks"] == [TASK_UUID] * 11
    assert "прервите" in outcome.note


def test_contract_skipped_without_models():
    """Реестр моделей пуст — «пропущена»: пробы обучения выполнить не на чем."""
    context = stand(FakeTraining(models=[]).handler, "TC-TR-01")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет модели" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-TR-06 — экспорт ONNX
# ---------------------------------------------------------------------------
def test_onnx_export_passes_with_headers():
    """Файл получен: имя, размер, содержимое и заголовки потока (UC-23)."""
    context = stand(FakeTraining().handler, "TC-TR-06")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["file_name"] == "stft_mlp.onnx"
    assert outcome.evidence["size"] == len(ONNX_BYTES)
    assert outcome.evidence["onnx_marker"] is True
    assert outcome.evidence["content_length"] == "512"
    assert outcome.evidence["accept_ranges"] == "bytes"
    assert outcome.evidence["status"] == 200
    assert outcome.note == ""
    assert outcome.evidence["sha256"]


def test_onnx_export_notes_streaming_defect():
    """Нет `Accept-Ranges` — примечание (замечание к потоковой отдаче), а не отказ."""
    headers = {"content-type": "application/octet-stream"}
    context = stand(FakeTraining(onnx_headers=headers).handler, "TC-TR-06")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "`Range`/`Accept-Ranges`" in outcome.note
    assert "TC-FILE-09" in outcome.note
    assert outcome.evidence["accept_ranges"] is None


def test_onnx_export_notes_missing_marker():
    """Файл без строки `onnx` — примечание: формат стоит проверить вручную."""
    context = stand(FakeTraining(onnx_content=b"\x00" * 128).handler, "TC-TR-06")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "нет строки `onnx`" in outcome.note


def test_onnx_export_fails_on_empty_file():
    """Пустой файл — отказ: экспорт не дал результата."""
    context = stand(FakeTraining(onnx_content=b"").handler, "TC-TR-06")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "пустой файл" in outcome.verdict


def test_onnx_export_fails_without_file_name():
    """Сервер не передал имя файла — отказ: результат нельзя опознать."""
    context = stand(FakeTraining(onnx_file_name=None).handler, "TC-TR-06")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не передал имя файла" in outcome.verdict


def test_onnx_export_fails_when_export_errors():
    """Сервер отказал в экспорте — отказ с текстом сервера."""
    context = stand(FakeTraining(onnx_status=500).handler, "TC-TR-06")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "экспорт ONNX не выполнен" in outcome.verdict


def test_onnx_export_uses_first_model_without_metrics():
    """Обученной модели нет — экспортируется первая модель реестра (без метрик)."""
    models = [model_payload(metrics=None)]
    context = stand(FakeTraining(models=models).handler, "TC-TR-06")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["model_id"] == MODEL_ID


def test_onnx_export_skipped_without_models():
    """Реестр моделей пуст — «пропущена»: экспортировать нечего."""
    context = stand(FakeTraining(models=[]).handler, "TC-TR-06")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет модели" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-TR-02/03/04/05 — обучение, управление, результат, проверка модели
# ---------------------------------------------------------------------------
def test_training_start_runs_task_and_registers_result():
    """Обучение запущено, задача отслежена, модель-результат зарегистрирована (UC-21)."""
    fake = FakeTraining(accept_all=True)
    context = stand(fake.handler, "TC-TR-02")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["response"]["task_id"] == TASK_UUID
    assert outcome.evidence["params"]["epochs"] == 3
    assert outcome.evidence["watch"]["final"] == "completed"
    assert outcome.evidence["watch"]["terminal"] is True
    assert outcome.evidence["trained_model"]["model_id"] == TRAINED_ID
    assert (
        outcome.evidence["trained_model"]["name"]
        == outcome.evidence["params"]["trained_model_name"]
    )
    entities = context.session.counters["test_entities"]
    assert [item["type"] for item in entities] == ["модель (результат обучения)"]
    assert fake.calls[0]["trained_model_name"].startswith("__TEST__TC_TR_02_")


def test_training_start_reports_unfinished_task():
    """Задача не дошла до терминального состояния — отказ с примечанием для оператора."""
    fake = FakeTraining(accept_all=True)
    fake.task_status = "running"
    context = stand(fake.handler, "TC-TR-02", timeout="0,2")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не дошла до терминального состояния" in outcome.verdict
    assert "остаётся на стенде" in outcome.note


def test_training_result_compares_card_and_removes_model():
    """Результат обучения: метрики заполнены, версия новая, модель убрана (по умолчанию)."""
    fake = FakeTraining(accept_all=True)
    started = stand(fake.handler, "TC-TR-02")
    first = check_runner.automate(started)
    assert first is not None and first.status == CheckStatus.PASSED, first.verdict
    name = first.evidence["params"]["trained_model_name"]

    context = stand(fake.handler, "TC-TR-04", session=started.session, trained_model_name=name)
    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["after"]["metrics_filled"] is True
    assert outcome.evidence["version_changed"] is True
    assert outcome.evidence["artifact"]["action"] == "удалена"
    assert TRAINED_ID in fake.deleted, "модель-результат удалена по решению по умолчанию"
    assert not any(str(row["id"]) == TRAINED_ID for row in fake.models)
    decision = started.session.artifacts[-1]
    assert decision["action"] == ARTIFACT_DELETED, "удаление тоже решение — вход TC-CLEAN-04"
    assert decision["entity_id"] == TRAINED_ID


def test_training_result_keeps_model_as_artifact():
    """Решение `keep_artifact` оставляет модель артефактом и фиксирует это в сессии."""
    fake = FakeTraining(accept_all=True)
    started = stand(fake.handler, "TC-TR-02")
    first = check_runner.automate(started)
    assert first is not None and first.status == CheckStatus.PASSED

    context = stand(
        fake.handler,
        "TC-TR-04",
        session=started.session,
        trained_model_name=first.evidence["params"]["trained_model_name"],
        keep_artifact="да",
    )
    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["artifact"]["action"] == "оставлена артефактом"
    assert fake.deleted == [], "модель не удаляется, если оператор оставил её артефактом"
    types = [item["type"] for item in started.session.counters["test_entities"]]
    assert "модель (артефакт)" in types
    decision = started.session.artifacts[-1]
    assert decision["action"] == ARTIFACT_KEPT, "решение видно отчёту (TC-CLEAN-04)"
    assert decision["entity_id"] == TRAINED_ID
    assert decision["check_id"] == "TC-TR-04"


def test_training_result_skipped_without_training():
    """Без запущенного обучения результат проверять не на чем — «пропущена»."""
    context = stand(FakeTraining().handler, "TC-TR-04")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "сначала выполните `TC-TR-02`" in outcome.verdict


def test_training_control_sends_fsm_commands():
    """`pause`/`resume`/`interrupt` отправляются на живой задаче и дают переходы FSM-1."""
    fake = FakeTraining(accept_all=True)
    fake.task_status = "running"
    context = stand(fake.handler, "TC-TR-03", command_timeout="5")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert fake.commands == ["pause", "resume", "interrupt"]
    rows = {row["action"]: row for row in outcome.evidence["commands"]}
    assert rows["pause"]["reached"] == "paused"
    assert rows["resume"]["reached"] == "running"
    assert rows["interrupt"]["reached"] == "interrupted"
    assert outcome.evidence["watch"]["final"] == "interrupted"


def test_training_control_requires_running_task():
    """Задача завершилась до команд — отказ: на терминальной задаче FSM-1 не проверить."""
    fake = FakeTraining(accept_all=True)
    fake.task_status = "completed"
    context = stand(fake.handler, "TC-TR-03", command_timeout="5")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не дошла до статуса `running`" in outcome.verdict


def test_training_check_starts_model_testing_task():
    """Проверка модели запускает задачу `model-testing` и фиксирует результат (UC-22)."""
    fake = FakeTraining()
    context = stand(fake.handler, "TC-TR-05")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["response"]["task_id"] == TASK_UUID
    assert outcome.evidence["watch"]["task_type"] == "model-testing"
    assert outcome.evidence["watch"]["final"] == "completed"
    assert outcome.evidence["threshold"] == 0.5
    assert outcome.evidence["model_after"]["model_id"] == MODEL_ID


def test_training_check_reports_failed_task():
    """Задача проверки модели завершилась ошибкой — отказ с финальным статусом."""
    fake = FakeTraining()
    fake.task_status = "failed"
    context = stand(fake.handler, "TC-TR-05")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "вместо completed" in outcome.verdict
