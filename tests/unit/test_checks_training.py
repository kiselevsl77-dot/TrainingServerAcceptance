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
from acceptance.session import new_session
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
Handler = Callable[[httpx.Request], httpx.Response]

MODEL_ID = "3d2d1139-0000-4000-8000-000000000001"
DATASET_ID = "5f0a5b7e-0000-4000-8000-000000000001"


def spec_of(check_id: str) -> CheckSpec:
    """Описание проверки из каталога (в тестах оно всегда существует)."""
    spec = catalog.find(check_id)
    assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
    return spec


def model_payload() -> dict[str, Any]:
    """Обученная модель реестра (`ModelMetadata`)."""
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
        "metrics": {"epochs": 3},
        "pruning_mask": None,
        "created_at": "2026-09-14T19:08:10",
        "updated_at": "2026-09-14T19:08:10",
        "signals": [],
        "signal_aliases": {"0": "miner"},
    }


def _validation_error(field: str, message: str) -> dict[str, Any]:
    """Ответ 422 в формате `HTTPValidationError.detail[]` (NFR-2)."""
    return {"detail": [{"loc": ["body", field], "msg": message, "type": "value_error"}]}


class FakeTraining:
    """Подменённый стенд: реестр моделей и `train` с проверкой диапазонов."""

    def __init__(
        self,
        *,
        models: list[dict[str, Any]] | None = None,
        datasets: tuple[str, ...] = (DATASET_ID,),
        accept_all: bool = False,
    ) -> None:
        self.models = [dict(item) for item in (models if models is not None else [model_payload()])]
        self.datasets = datasets
        self.accept_all = accept_all
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого сервера: список моделей и обучение."""
        path = request.url.path
        if path == "/api/ml_models/models":
            return httpx.Response(200, json={"models": self.models, "count": len(self.models)})
        if path.endswith("/train") and request.method == "POST":
            return self._train(request)
        return httpx.Response(404, json={"detail": "Not Found"})

    def _train(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append(body)
        if self.accept_all:
            return httpx.Response(
                202,
                json={
                    "status": "accepted",
                    "model_id": MODEL_ID,
                    "dataset_id": str(body.get("dataset_id") or DATASET_ID),
                    "task_id": "task-1",
                    "batch_size": int(body.get("batch_size") or 32),
                    "epochs": int(body.get("epochs") or 1),
                    "learning_rate": float(body.get("learning_rate") or 1e-06),
                    "preprocessing_converter": "direct",
                    "validation_info": {},
                    "message": "Обучение запущено",
                    "augmentation": {},
                },
            )
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


def stand(handler: Handler, check_id: str, **params: Any) -> Any:
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
        session=new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=apis,
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
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
    assert outcome.evidence["started_tasks"] == ["task-1"] * 11
    assert "прервите" in outcome.note


def test_contract_skipped_without_models():
    """Реестр моделей пуст — «пропущена»: пробы обучения выполнить не на чем."""
    context = stand(FakeTraining(models=[]).handler, "TC-TR-01")

    outcome = check_training.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет модели" in outcome.verdict
