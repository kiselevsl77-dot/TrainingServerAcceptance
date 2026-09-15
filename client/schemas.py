"""Pydantic-модели API сервера обучения Energomera.

Модели сформированы на основе OpenAPI 3.1-спецификации `SOM1.json`
(раздел `components.schemas`) для типизации запросов/ответов httpx-клиента.

Соглашения соответствия OpenAPI -> Pydantic:
    * `anyOf: [X, null]`           -> `X | None`
    * `format: uuid`               -> `uuid.UUID`
    * `format: date-time`          -> `datetime.datetime`
    * `type: integer`              -> `int`, `type: number` -> `float`
    * `const: "direct_fill"`       -> `typing.Literal`
    * `enum`                       -> `enum.StrEnum`
    * `additionalProperties:false` -> `ConfigDict(extra="forbid")`
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Перечисления
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    """Состояния асинхронных задач (FSM-1)."""

    NEW = "new"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    PAUSING = "pausing"
    PAUSED = "paused"
    NOT_FOUND = "not_found"


class TaskType(StrEnum):
    """Типы задач Celery."""

    CELERY_TEST = "celery-test"
    TRAINING = "training"
    DATASET_FILL = "dataset-fill"
    MODEL_TESTING = "model-testing"
    INFERENCE = "inference"


class FileType(StrEnum):
    """Типы файлов, принимаемые формой загрузки (UC-02, BR-F1).

    Замечено на живом сервисе: в хранилище встречаются также значения `H5` и
    `REPORT_ZIP` (модели и отчёты), которых нет в перечислении спецификации.
    Поэтому ответ `FileMetadataResponse.file_type` типизирован как `str`, а это
    перечисление используется только для формы загрузки (`RAW`/`LOADS`/`ONNX`).
    """

    RAW = "RAW"
    LOADS = "LOADS"
    ONNX = "ONNX"


#: Значения `file_type`, наблюдаемые в ответах `GET /api/data/files`.
KNOWN_FILE_TYPES: tuple[str, ...] = ("RAW", "LOADS", "ONNX", "H5", "REPORT_ZIP")


DatasetType = Literal["direct_fill"]


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


class ValidationError(BaseModel):
    """Элемент `HTTPValidationError.detail[]` (loc/msg/type)."""

    loc: list[str | int]
    msg: str
    type: str


class HTTPValidationError(BaseModel):
    """Ответ 422 (NFR-2: разбор ошибок у конкретных полей)."""

    detail: list[ValidationError] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# File Import (FR-2)
# ---------------------------------------------------------------------------


class BodyUploadDataFileApiDataFilePost(BaseModel):
    """multipart-форма загрузки файла данных (POST /api/data/file)."""

    file: bytes  # binary в multipart/form-data
    file_type: str
    description: str | None = None


class FileMetadataResponse(BaseModel):
    id: UUID
    file_name: str
    size: int
    s3_path: str
    import_date: datetime
    file_type: str


class FileMetadataListResponse(BaseModel):
    files: list[FileMetadataResponse]
    count: int


# ---------------------------------------------------------------------------
# Loads (FR-3)
# ---------------------------------------------------------------------------


class LoadDevice(BaseModel):
    """Тело запроса `POST /api/loads` — создание нагрузки (UC-07)."""

    load_id: str
    phase_connection: str
    category: str
    description: str | None = None


class UpdateLoadRequest(BaseModel):
    """Тело запроса `PUT /api/loads/{load_id}` — правка нагрузки (UC-08)."""

    phase_connection: str | None = None
    category: str | None = None
    description: str | None = None


class LoadItem(BaseModel):
    """Элемент реестра нагрузок (`GET /api/loads/list`, UC-06).

    Замечания к API (проверено на живом сервисе):
        * в ответе нет `phase_connection`, хотя `LoadDevice` (POST/PUT) требует его,
          а фильтр `ph_n` предусмотрен спецификацией — фактически он ничего не
          фильтрует (данных о фазе в реестре нет);
        * схема ответа в `SOM1.json` описана как пустая (`{}`), реально приходит
          `{loads, result_size, limit, offset}`.
    """

    load_id: str
    category: str
    description: str | None = None


class LoadsListResponse(BaseModel):
    """Ответ `GET /api/loads/list`: срез списка и общее число записей."""

    loads: list[LoadItem] = Field(default_factory=list)
    result_size: int = 0
    limit: int | None = None
    offset: int | None = None


# ---------------------------------------------------------------------------
# Datasets (FR-4)
# ---------------------------------------------------------------------------


class DatasetCreate(BaseModel):
    name: str
    description: str | None = None
    type: DatasetType


class DatasetFillRequest(BaseModel):
    raw_file_ids: list[UUID]
    markup_file_ids: list[UUID] = Field(default_factory=list)


class DatasetResponse(BaseModel):
    id: UUID
    creation_date: datetime
    name: str
    description: str | None = None
    type: DatasetType


class DatasetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class DatasetListResponse(BaseModel):
    datasets: list[DatasetResponse]
    count: int


# ---------------------------------------------------------------------------
# ML models (FR-5, FR-6)
# ---------------------------------------------------------------------------


class ModelArchitectureInfo(BaseModel):
    architecture: str
    description: str
    default_config: dict[str, Any]
    suitable_for_phases: list[str]


class ModelCreateRequest(BaseModel):
    architecture: str
    name: str
    output_size: int
    phase_type: str = "three"
    description: str | None = None
    signals: dict[str, list[str]] | None = None
    signal_aliases: dict[str, str] | None = None
    config: dict[str, Any] | None = None
    pruning_mask: str | None = None


class BodyUploadModelApiMlModelsModelsUploadPost(BaseModel):
    """multipart-форма загрузки модели (POST /api/ml_models/models/upload)."""

    file: bytes  # .h5
    metadata: str  # JSON-строка (ModelFileUploadRequest)


class ModelMetadata(BaseModel):
    id: UUID
    file_id: UUID
    architecture: str
    version: str
    name: str
    description: str | None = None
    phase_type: str
    output_size: int
    config: dict[str, Any]
    metrics: dict[str, Any] | None = None
    pruning_mask: str | None = None
    created_at: datetime
    updated_at: datetime
    signals: list[dict[str, Any]] | None = None
    signal_aliases: dict[str, str] | None = None


class ModelListResponse(BaseModel):
    models: list[ModelMetadata]
    count: int


class QuantizationParams(BaseModel):
    enabled: bool = False
    num_bits: int = 8
    symmetric: bool = True


class ModelTrainingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    batch_size: int = Field(default=32, ge=1, le=256)
    epochs: int = Field(default=50, ge=1, le=1000)
    learning_rate: float = Field(default=1e-7, gt=0.0, le=1.0)
    trained_model_name: str | None = None
    augmentation_noise_percentage: float | None = Field(default=None, ge=0.001, le=0.05)
    augmentation_num_samples: int | None = Field(default=None, ge=1, le=10)
    preprocessing_converter: str = "direct"
    quantization: QuantizationParams | None = None


class ModelTrainingResponse(BaseModel):
    status: str
    model_id: UUID
    dataset_id: UUID
    task_id: str
    batch_size: int
    epochs: int
    learning_rate: float
    trained_model_name: str | None = None
    preprocessing_converter: str
    validation_info: dict[str, Any]
    message: str
    augmentation: dict[str, Any]
    quantization: dict[str, Any] | None = None


class ModelCheckRequest(BaseModel):
    dataset_id: UUID
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class ModelCheckResponse(BaseModel):
    task_id: str
    status: str
    message: str
    validation_info: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Inference (FR-7)
# ---------------------------------------------------------------------------


class InferenceClass(BaseModel):
    signal_num: int
    device_ids: list[str]
    confidence: float
    detected: bool


class InferenceSingleRequest(BaseModel):
    U_A_signal: str
    U_B_signal: str
    U_C_signal: str
    I_A_signal: str
    I_B_signal: str
    I_C_signal: str
    U_mult: int = 721
    U_dev: int = 1
    I_mult: int = 25000
    I_dev: int = 66
    byte_order: str = "Little"
    signal_bytes: int = 3
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class InferenceSingleResponse(BaseModel):
    model_id: UUID
    threshold: float
    classes: list[InferenceClass]


class InferenceStartRequest(BaseModel):
    dataset_id: UUID
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class InferenceStartResponse(BaseModel):
    status: str
    model_id: UUID
    dataset_id: UUID
    task_id: str
    threshold: float
    message: str


# ---------------------------------------------------------------------------
# Task service (FR-8)
# ---------------------------------------------------------------------------


class CeleryTask(BaseModel):
    type: TaskType
    name: str
    description: str | None = None
    id: UUID
    created_at: datetime


class CeleryTaskRuntime(BaseModel):
    task_id: UUID
    status: TaskStatus = TaskStatus.NEW
    celery_task_id: str | None = None
    parameters: dict[str, Any]
    start_time: datetime | None = None
    end_time: datetime | None = None
    result: dict[str, Any] | None = None
    intermediate_result: dict[str, Any] | None = None
    id: UUID


class TaskWithRuntimes(BaseModel):
    type: TaskType
    name: str
    description: str | None = None
    id: UUID
    created_at: datetime
    runtimes: list[CeleryTaskRuntime] = Field(default_factory=list)


class TaskListResponse(BaseModel):
    tasks: list[CeleryTask]
    count: int
