"""Тесты клиента ML-моделей (FR-5…FR-7): архитектуры, создание, `.h5`, ONNX, обучение.

Модуль вырос на этапе T6–T8: сценарии `TC-MOD`/`TC-TR`/`TC-INF` работают через него,
поэтому тесты фиксируют соответствие спецификации `SOM1.json`:

    * `metadata` загрузки модели уходит **строкой JSON**, а `file` — частью multipart
      (расхождение спецификации и факта — замечание);
    * тело `train` не содержит `None`-полей (`augmentation`, `quantization`), а
      `trained_model_name` отправляется только когда задан;
    * `inference/single` получает Base64-сигналы и параметры шкал из строки RAW
      (`acceptance/ml_samples`), порог — обязательное поле запроса.
"""

from __future__ import annotations

import json

import httpx
import pytest

from client.errors import NotFoundError
from client.models import ModelsApi

MODEL_ID = "3d2d1139-0000-4000-8000-000000000001"
FILE_ID = "5932482e-0000-4000-8000-000000000002"
DATASET_ID = "5f0a5b7e-0000-4000-8000-000000000001"

ARCHITECTURE: dict[str, object] = {
    "architecture": "stft_mlp",
    "description": "MLP поверх STFT-признаков",
    "default_config": {"architecture": "stft_mlp", "phase_type": "single"},
    "suitable_for_phases": ["single", "three"],
}

MODEL: dict[str, object] = {
    "id": MODEL_ID,
    "file_id": FILE_ID,
    "architecture": "stft_mlp",
    "version": "stft_mlp.9bbb964f",
    "name": "stft_mlp-skfu-train-dev (trained)",
    "description": None,
    "phase_type": "single",
    "output_size": 2,
    "config": {"architecture": "stft_mlp"},
    "metrics": {"epochs": 3, "dataset_id": DATASET_ID},
    "pruning_mask": None,
    "created_at": "2026-09-14T19:08:10",
    "updated_at": "2026-09-14T19:08:10",
    "signals": [],
    "signal_aliases": {"0": "miner", "1": "not_miner"},
}

SIGNAL = "A" * 64


def sample_fields() -> dict[str, object]:
    """Поля сэмпла `inference/single` — как их отдаёт разбор строки RAW."""
    return {
        "U_A_signal": SIGNAL,
        "U_B_signal": SIGNAL,
        "U_C_signal": SIGNAL,
        "I_A_signal": SIGNAL,
        "I_B_signal": SIGNAL,
        "I_C_signal": SIGNAL,
        "U_mult": 721,
        "U_dev": 1,
        "I_mult": 25000,
        "I_dev": 66,
        "byte_order": "Little",
        "signal_bytes": 3,
    }


def test_list_architectures_parses_bare_list(client_factory):
    """Стенд отдаёт каталог архитектур JSON-массивом (проверено на живом API)."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        return httpx.Response(200, json=[ARCHITECTURE])

    rows = ModelsApi(client_factory(handler)).list_architectures()

    assert seen["method"] == "GET"
    assert seen["path"] == "/api/ml_models/architectures"
    assert [item.architecture for item in rows] == ["stft_mlp"]
    assert rows[0].default_config["phase_type"] == "single"


def test_list_architectures_parses_wrapped_list(client_factory):
    """Ответ с обёрткой `architectures` тоже принимается (спецификация не строгая)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"architectures": [ARCHITECTURE], "count": 1})

    rows = ModelsApi(client_factory(handler)).list_architectures()

    assert len(rows) == 1


def test_create_model_sends_only_filled_fields(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json=MODEL)

    created = ModelsApi(client_factory(handler)).create_model(
        architecture="stft_mlp",
        name="__TEST__model",
        output_size=2,
        phase_type="single",
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/ml_models/models"
    assert seen["body"] == {
        "architecture": "stft_mlp",
        "name": "__TEST__model",
        "output_size": 2,
        "phase_type": "single",
    }
    assert str(created.id) == MODEL_ID
    assert created.version == "stft_mlp.9bbb964f"


def test_upload_model_sends_file_and_json_metadata(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(201, json=MODEL)

    ModelsApi(client_factory(handler)).upload_model(
        file_name=r"C:\tmp\__TEST__model.h5",
        content=b"\x89HDF\r\n\x1a\n",
        name="__TEST__uploaded",
        output_size=2,
        architecture="stft_mlp",
        phase_type="single",
    )

    body = seen["body"]
    assert isinstance(body, bytes)
    assert seen["path"] == "/api/ml_models/models/upload"
    assert "multipart/form-data" in str(seen["content_type"])
    assert b'name="file"' in body
    # в теле — только базовое имя файла, без локального пути
    assert b'filename="__TEST__model.h5"' in body
    assert b'name="metadata"' in body

    metadata = _metadata_from_body(body)
    assert metadata["name"] == "__TEST__uploaded"
    assert metadata["architecture"] == "stft_mlp"
    assert "id" not in metadata and "version" not in metadata


def _metadata_from_body(body: bytes) -> dict[str, object]:
    """Достаёт JSON-строку `metadata` из multipart-тела запроса загрузки модели."""
    text = body.decode("utf-8", errors="replace")
    marker = 'name="metadata"'
    start = text.index(marker)
    start = text.index("\r\n\r\n", start) + 4
    end = text.index("\r\n", start)
    parsed = json.loads(text[start:end])
    assert isinstance(parsed, dict)
    return parsed


def test_list_models_sends_known_filters(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"models": [MODEL], "count": 1})

    rows = ModelsApi(client_factory(handler)).list_models(
        architecture="stft_mlp", phase_type="single", output_size=2
    )

    assert seen["query"] == "architecture=stft_mlp&phase_type=single&output_size=2"
    assert rows[0].name == MODEL["name"]

    def bare(request: httpx.Request) -> httpx.Response:
        seen["bare_query"] = request.url.query.decode()
        return httpx.Response(200, json={"models": [], "count": 0})

    ModelsApi(client_factory(bare)).list_models()
    assert seen["bare_query"] == ""


def test_get_model_propagates_404(client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Model not found"})

    with pytest.raises(NotFoundError):
        ModelsApi(client_factory(handler)).get_model(MODEL_ID)


def test_delete_model_uses_model_path(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"message": "Model deleted successfully"})

    ModelsApi(client_factory(handler)).delete_model(MODEL_ID)

    assert seen["method"] == "DELETE"
    assert seen["path"] == f"/api/ml_models/models/{MODEL_ID}"


def test_download_onnx_returns_binary_with_file_name(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            content=b"ONNX",
            headers={
                "content-type": "application/octet-stream",
                "content-length": "4",
                "content-disposition": "attachment; filename=stft_mlp.onnx",
            },
        )

    result = ModelsApi(client_factory(handler)).download_onnx(MODEL_ID)

    assert seen["path"] == f"/api/ml_models/models/{MODEL_ID}/download_onnx"
    assert result.size == 4
    assert result.file_name == "stft_mlp.onnx"


def test_train_sends_requested_parameters(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json=_training_response())

    response = ModelsApi(client_factory(handler)).train(
        MODEL_ID,
        dataset_id=DATASET_ID,
        epochs=3,
        batch_size=4,
        trained_model_name="__TEST__trained",
        augmentation_noise_percentage=0.01,
        augmentation_num_samples=2,
    )

    assert seen["path"] == f"/api/ml_models/models/{MODEL_ID}/train"
    assert seen["body"] == {
        "dataset_id": DATASET_ID,
        "batch_size": 4,
        "epochs": 3,
        "learning_rate": 1e-07,
        "trained_model_name": "__TEST__trained",
        "preprocessing_converter": "direct",
        "augmentation_noise_percentage": 0.01,
        "augmentation_num_samples": 2,
    }
    assert response.task_id == "task-1"


def test_train_defaults_and_quantization(client_factory):
    """Без явных параметров уходят значения спецификации; квантизация — по схеме."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json=_training_response())

    ModelsApi(client_factory(handler)).train(
        MODEL_ID,
        dataset_id=DATASET_ID,
        quantization={"enabled": True, "num_bits": 8, "symmetric": True},
    )

    assert seen["body"] == {
        "dataset_id": DATASET_ID,
        "batch_size": 32,
        "epochs": 50,
        "learning_rate": 1e-07,
        "preprocessing_converter": "direct",
        "quantization": {"enabled": True, "num_bits": 8, "symmetric": True},
    }


def _training_response() -> dict[str, object]:
    """Ответ `train` (202): задача обучения и параметры запуска."""
    return {
        "status": "accepted",
        "model_id": MODEL_ID,
        "dataset_id": DATASET_ID,
        "task_id": "task-1",
        "batch_size": 4,
        "epochs": 3,
        "learning_rate": 1e-07,
        "trained_model_name": "__TEST__trained",
        "preprocessing_converter": "direct",
        "validation_info": {"epochs": 3},
        "message": "Обучение запущено",
        "augmentation": {},
        "quantization": None,
    }


def test_check_model_sends_dataset_and_threshold(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            202,
            json={
                "task_id": "task-3",
                "status": "accepted",
                "message": "Проверка запущена",
                "validation_info": None,
            },
        )

    response = ModelsApi(client_factory(handler)).check_model(
        MODEL_ID, dataset_id=DATASET_ID, threshold=0.7
    )

    assert seen["path"] == f"/api/ml_models/{MODEL_ID}/check"
    assert seen["body"] == {"dataset_id": DATASET_ID, "threshold": 0.7}
    assert response.task_id == "task-3"


def test_inference_start_sends_dataset_and_threshold(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            202,
            json={
                "status": "accepted",
                "model_id": MODEL_ID,
                "dataset_id": DATASET_ID,
                "task_id": "task-4",
                "threshold": 0.5,
                "message": "Инференс запущен",
            },
        )

    response = ModelsApi(client_factory(handler)).inference(MODEL_ID, dataset_id=DATASET_ID)

    assert seen["path"] == f"/api/ml_models/models/{MODEL_ID}/inference"
    assert seen["body"] == {"dataset_id": DATASET_ID, "threshold": 0.5}
    assert str(response.model_id) == MODEL_ID


def test_inference_single_sends_sample_fields_and_threshold(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "model_id": MODEL_ID,
                "threshold": 0.5,
                "classes": [
                    {
                        "signal_num": 0,
                        "device_ids": ["Antminer S19"],
                        "confidence": 0.91,
                        "detected": True,
                    }
                ],
            },
        )

    response = ModelsApi(client_factory(handler)).inference_single(MODEL_ID, sample=sample_fields())

    assert seen["path"] == f"/api/ml_models/models/{MODEL_ID}/inference/single"
    body = seen["body"]
    assert body["U_A_signal"] == SIGNAL
    assert body["threshold"] == 0.5
    assert body["U_mult"] == 721
    assert body["byte_order"] == "Little"
    assert response.classes[0].signal_num == 0
    assert response.classes[0].confidence == 0.91
    assert response.classes[0].detected is True


def test_inference_single_requires_signals(client_factory):
    """Без обязательного сигнала запрос не собирается: ошибка схемы, а не пустой вызов."""
    from pydantic import ValidationError

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - не вызывается
        return httpx.Response(200, json={"model_id": MODEL_ID, "threshold": 0.5, "classes": []})

    fields = sample_fields()
    fields.pop("I_C_signal")

    with pytest.raises(ValidationError):
        ModelsApi(client_factory(handler)).inference_single(MODEL_ID, sample=fields)
