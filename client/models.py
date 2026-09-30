"""Методы модуля «ML models» (FR-5…FR-7, UC-15…UC-25).

Эндпоинты `SOM1.json` (реестр операций — `acceptance/endpoints.py`):

    GET    /api/ml_models/architectures                       — каталог архитектур (UC-15);
    POST   /api/ml_models/models                              — создание модели (UC-16);
    POST   /api/ml_models/models/upload                       — загрузка `.h5` (UC-17, multipart);
    GET    /api/ml_models/models                              — список с фильтрами (UC-18);
    GET    /api/ml_models/models/{model_id}                   — карточка модели (UC-19);
    DELETE /api/ml_models/models/{model_id}                   — удаление модели (UC-20);
    GET    /api/ml_models/models/{model_id}/download_onnx     — экспорт ONNX (UC-23);
    POST   /api/ml_models/models/{model_id}/train             — обучение (UC-21, async);
    POST   /api/ml_models/{model_id}/check                    — проверка модели (UC-22, async);
    POST   /api/ml_models/models/{model_id}/inference         — инференс по датасету (UC-24, async);
    POST   /api/ml_models/models/{model_id}/inference/single  — предсказ одного сэмпла (UC-25).

Замечания к API, важные для испытаний (проверяются сценариями `TC-MOD`…`TC-INF`):

    * `POST /api/ml_models/models/upload` принимает `metadata` **строкой JSON**
      (`ModelFileUploadRequest` — те же поля, что у создания модели, без `version`);
    * `download_onnx` отдаёт `application/octet-stream` с `Content-Disposition`
      и `Content-Length`, но без `Range`/`Accept-Ranges`;
    * `train`/`check`/`inference` асинхронные: отвечают `task_id`, статус задачи
      отслеживает монитор (`acceptance.tasks_monitor`), а не этот модуль;
    * `inference/single` принимает **Base64-сигналы** (`U_A_signal`…`I_C_signal`)
      и параметры шкал — как колонки raw_data CSV (разбор — `acceptance/ml_samples`);
    * `signals` у живых моделей может быть заполнен (`signal_num` → `device_id`),
      а классы описаны в `signal_aliases` (`{"0": "miner", "1": "not_miner"}`).

Модуль остаётся «холодным» (без интерфейса и состояния): сценарии проверок берут
отсюда запросы, а мониторинг задач и учёт `__TEST__`-сущностей живут в `acceptance`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from client.files import guess_content_type
from client.http import ApiHttpClient, BinaryResponse
from client.schemas import (
    InferenceSingleRequest,
    InferenceSingleResponse,
    InferenceStartRequest,
    InferenceStartResponse,
    ModelArchitectureInfo,
    ModelCheckRequest,
    ModelCheckResponse,
    ModelCreateRequest,
    ModelListResponse,
    ModelMetadata,
    ModelTrainingRequest,
    ModelTrainingResponse,
    QuantizationParams,
)

#: Значения по умолчанию из спецификации (`ModelTrainingRequest`).
DEFAULT_BATCH_SIZE = 32
DEFAULT_EPOCHS = 50
DEFAULT_LEARNING_RATE = 1e-7
DEFAULT_PREPROCESSING = "direct"

#: Порог классификации по умолчанию (`ModelCheckRequest`, `InferenceStartRequest`).
DEFAULT_THRESHOLD = 0.5


def _drop_empty(params: dict[str, Any]) -> dict[str, Any]:
    """Убирает незаполненные фильтры, чтобы не отправлять пустые query-параметры."""
    return {key: value for key, value in params.items() if value not in (None, "")}


class ModelsApi:
    """Доступ к каталогу моделей, обучению, проверке модели и инференсу."""

    def __init__(self, client: ApiHttpClient, *, download_timeout: float | None = None) -> None:
        self._client = client
        self._download_timeout = download_timeout

    # -- каталог архитектур (UC-15) -------------------------------------------
    def list_architectures(self) -> list[ModelArchitectureInfo]:
        """GET /api/ml_models/architectures — перечень архитектур (TC-MOD-01)."""
        payload = self._client.get("/api/ml_models/architectures")
        if payload is None:
            return []
        rows = payload.get("architectures") if isinstance(payload, dict) else payload
        return [ModelArchitectureInfo.model_validate(item) for item in (rows or [])]

    # -- создание модели (UC-16) ----------------------------------------------
    def create_model(
        self,
        *,
        architecture: str,
        name: str,
        output_size: int,
        phase_type: str = "three",
        description: str | None = None,
        signals: dict[str, list[str]] | None = None,
        signal_aliases: dict[str, str] | None = None,
        config: dict[str, Any] | None = None,
        pruning_mask: str | None = None,
    ) -> ModelMetadata:
        """POST /api/ml_models/models — создание модели (UC-16).

        Returns:
            Метаданные созданной модели: `id`, `version`, `architecture` (ответ 201).
        """
        body = self._request_body(
            architecture=architecture,
            name=name,
            output_size=output_size,
            phase_type=phase_type,
            description=description,
            signals=signals,
            signal_aliases=signal_aliases,
            config=config,
            pruning_mask=pruning_mask,
        )
        payload = self._client.post("/api/ml_models/models", json=body)
        return ModelMetadata.model_validate(payload)

    # -- загрузка готовой модели (UC-17) --------------------------------------
    def upload_model(
        self,
        *,
        file_name: str,
        content: bytes,
        name: str,
        output_size: int,
        architecture: str = "",
        phase_type: str = "three",
        description: str | None = None,
        signals: dict[str, list[str]] | None = None,
        signal_aliases: dict[str, str] | None = None,
        config: dict[str, Any] | None = None,
        pruning_mask: str | None = None,
    ) -> ModelMetadata:
        """POST /api/ml_models/models/upload — загрузка готовой модели `.h5` (UC-17).

        Args:
            file_name: имя файла модели (в теле — только базовое имя).
            content: содержимое `.h5`; в испытаниях берётся со стенда
                (`GET /api/data/file/{file_id}/download`, проверка `TC-MOD-04`).
            name: имя создаваемой модели — в испытаниях с префиксом `__TEST__`.
            architecture: архитектура; пусто — сервер выводит её из `.h5`.
            metadata: уходит строкой JSON (`ModelFileUploadRequest`), собранной здесь
                из тех же полей, что и создание модели.

        Returns:
            Метаданные загруженной модели (`file_id` + `version`).
        """
        body = self._request_body(
            architecture=architecture,
            name=name,
            output_size=output_size,
            phase_type=phase_type,
            description=description,
            signals=signals,
            signal_aliases=signal_aliases,
            config=config,
            pruning_mask=pruning_mask,
        )
        metadata = json.dumps(body, ensure_ascii=False)
        payload: dict[str, Any] = self._client.post(
            "/api/ml_models/models/upload",
            files={"file": (Path(file_name).name, content, guess_content_type(file_name))},
            data={"metadata": metadata},
        )
        return ModelMetadata.model_validate(payload)

    @staticmethod
    def _request_body(
        *,
        architecture: str,
        name: str,
        output_size: int,
        phase_type: str,
        description: str | None,
        signals: dict[str, list[str]] | None,
        signal_aliases: dict[str, str] | None,
        config: dict[str, Any] | None,
        pruning_mask: str | None,
    ) -> dict[str, Any]:
        """Тело создания модели: незаполненные поля не отправляются."""
        request = ModelCreateRequest(
            architecture=architecture,
            name=name,
            output_size=output_size,
            phase_type=phase_type,
            description=description or None,
            signals=signals,
            signal_aliases=signal_aliases,
            config=config,
            pruning_mask=pruning_mask,
        )
        return request.model_dump(exclude_none=True)

    def list_models(
        self,
        *,
        model_id: str | None = None,
        architecture: str | None = None,
        version: str | None = None,
        name: str | None = None,
        phase_type: str | None = None,
        output_size: int | None = None,
        device_id: str | None = None,
    ) -> list[ModelMetadata]:
        """GET /api/ml_models/models — список моделей (UC-18)."""
        params: dict[str, Any] = {
            "model_id": model_id or None,
            "architecture": architecture or None,
            "version": version or None,
            "name": name or None,
            "phase_type": phase_type or None,
            "output_size": output_size,
            "device_id": device_id or None,
        }

        payload = self._client.get("/api/ml_models/models", params=_drop_empty(params))
        if payload is None:
            return []

        return ModelListResponse.model_validate(payload).models

    def get_model(self, model_id: UUID | str) -> ModelMetadata:
        """GET /api/ml_models/models/{model_id} — карточка модели и метрики (UC-19).

        Карточка отдаёт `config`, `metrics`, `created_at`/`updated_at`, `signals` и
        `signal_aliases` — по ним проверяются `TC-MOD-06` и результат обучения
        (`TC-TR-04`: обновление `metrics`/`version`/`updated_at`).
        """
        payload = self._client.get(f"/api/ml_models/models/{model_id}")
        return ModelMetadata.model_validate(payload)

    def delete_model(self, model_id: UUID | str) -> Any:
        """DELETE /api/ml_models/models/{model_id} — удаление модели (UC-20, TC-MOD-07).

        Судьба связанного файла `.h5` (каскад или отдельное удаление) фиксируется
        сценарием по факту ответа и состоянию реестра файлов.
        """
        return self._client.delete(f"/api/ml_models/models/{model_id}")

    def download_onnx(self, model_id: UUID | str) -> BinaryResponse:
        """GET /api/ml_models/models/{model_id}/download_onnx — экспорт ONNX (UC-23, TC-TR-06)."""
        return self._client.get_binary(
            f"/api/ml_models/models/{model_id}/download_onnx",
            timeout=self._download_timeout,
        )

    # -- обучение (UC-21) ------------------------------------------------------
    def train(
        self,
        model_id: UUID | str,
        *,
        dataset_id: UUID | str,
        epochs: int = DEFAULT_EPOCHS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        learning_rate: float = DEFAULT_LEARNING_RATE,
        trained_model_name: str | None = None,
        preprocessing_converter: str = DEFAULT_PREPROCESSING,
        augmentation_noise_percentage: float | None = None,
        augmentation_num_samples: int | None = None,
        quantization: QuantizationParams | dict[str, Any] | None = None,
    ) -> ModelTrainingResponse:
        """POST /api/ml_models/models/{model_id}/train — запуск обучения (UC-21, async).

        Args:
            model_id: модель-основа обучения; в испытаниях — `__TEST__`-модель,
                чтобы обучение не меняло боевую модель стенда.
            dataset_id: датасет обучения (`dataset_id` в теле запроса).
            trained_model_name: имя результата обучения; для проверок — с префиксом
                `__TEST__`, иначе созданное испытаниями не отличить от боевого.
            augmentation_noise_percentage: шум аугментации, допустимо 0.001…0.05.
            augmentation_num_samples: сэмплов аугментации, допустимо 1…10.
            quantization: параметры квантизации (`QuantizationParams`) либо `None`.

        Returns:
            Ответ 202 (`task_id`, `validation_info`); статус задачи — через монитор
            (`TC-TR-02` отслеживает задачу типа `training` до терминального состояния).
        """
        request = ModelTrainingRequest(
            dataset_id=UUID(str(dataset_id)),
            epochs=epochs,
            batch_size=batch_size,
            learning_rate=learning_rate,
            trained_model_name=trained_model_name or None,
            preprocessing_converter=preprocessing_converter,
            augmentation_noise_percentage=augmentation_noise_percentage,
            augmentation_num_samples=augmentation_num_samples,
            quantization=_quantization(quantization),
        )
        payload = self._client.post(
            f"/api/ml_models/models/{model_id}/train",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return ModelTrainingResponse.model_validate(payload)

    # -- проверка модели (UC-22) ----------------------------------------------
    def check_model(
        self,
        model_id: UUID | str,
        *,
        dataset_id: UUID | str,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> ModelCheckResponse:
        """POST /api/ml_models/{model_id}/check — проверка модели (UC-22, async, TC-TR-05)."""
        request = ModelCheckRequest(dataset_id=UUID(str(dataset_id)), threshold=threshold)
        payload = self._client.post(
            f"/api/ml_models/{model_id}/check",
            json=request.model_dump(mode="json"),
        )
        return ModelCheckResponse.model_validate(payload)

    # -- инференс (UC-24, UC-25) ----------------------------------------------
    def inference(
        self,
        model_id: UUID | str,
        *,
        dataset_id: UUID | str,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> InferenceStartResponse:
        """POST /api/ml_models/models/{model_id}/inference — инференс по датасету (UC-24).

        Асинхронная операция: `TC-INF-01` отслеживает задачу типа `inference` монитором.
        """
        request = InferenceStartRequest(dataset_id=UUID(str(dataset_id)), threshold=threshold)
        payload = self._client.post(
            f"/api/ml_models/models/{model_id}/inference",
            json=request.model_dump(mode="json"),
        )
        return InferenceStartResponse.model_validate(payload)

    def inference_single(
        self,
        model_id: UUID | str,
        *,
        sample: dict[str, Any],
        threshold: float = DEFAULT_THRESHOLD,
    ) -> InferenceSingleResponse:
        """POST …/{model_id}/inference/single — предсказ одного сэмпла (UC-25).

        Args:
            sample: поля сэмпла — шесть Base64-сигналов (`U_A_signal`…`I_C_signal`)
                и, при наличии, параметры шкал (`U_mult`, `U_dev`, `I_mult`, `I_dev`,
                `byte_order`, `signal_bytes`). Собирается из строки RAW-файла
                (`acceptance/ml_samples.RawSample.request_fields`).
            threshold: порог классификации; границы 0.0…1.0 проверяет `TC-INF-03`.

        Returns:
            `classes[]` с `signal_num`, `device_ids`, `confidence`, `detected` (`TC-INF-02`).
        """
        request = InferenceSingleRequest.model_validate({**sample, "threshold": threshold})
        payload = self._client.post(
            f"/api/ml_models/models/{model_id}/inference/single",
            json=request.model_dump(mode="json"),
        )
        return InferenceSingleResponse.model_validate(payload)


def _quantization(
    value: QuantizationParams | dict[str, Any] | None,
) -> QuantizationParams | None:
    """Приводит параметры квантизации к схеме (или `None`, если они не заданы)."""
    if value is None or isinstance(value, QuantizationParams):
        return value
    return QuantizationParams.model_validate(value)
