"""Реестр эндпоинтов испытуемого сервера — шаблоны консоли запросов (FR-T3).

Реестр **не пишется руками**: он выводится из спецификации `docs/SOM1.json`
генератором `tools/gen_endpoints.py` (блок между маркерами в конце файла).
Так консоль запросов гарантированно содержит все операции испытуемого API
(контракт 17.09.2026 — 40 операций),
а расхождение со спецификацией ловится тестом `tests/unit/test_endpoints.py`
и командой `python -m tools.gen_endpoints --check`.

Кроме описания операции реестр хранит **класс безопасности** (`Safety`) и
известную особенность эндпоинта (замечание к API из `SOM1. Комментарии.md`).
Класс безопасности определяет, что пульт требует перед запуском (NFR-T4):

    * `read` — безопасное чтение, подтверждение не нужно;
    * `write` — изменяет данные стенда: подтверждение + префикс `__TEST__`
      у создаваемых сущностей;
    * `destructive` — удаление: двойное подтверждение (чекбокс + слово `DELETE`),
      а для сущностей без префикса `__TEST__` — ещё и ввод полного `id`;
    * `heavy` — ресурсоёмкая операция (`fill`, `train`, `check`, async-инференс):
      карточка запуска с целью, данными, ответственным и подтверждением (FR-T10).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: Префикс имён сущностей, создаваемых пультом на общем стенде (§9 ТЗ).
TEST_PREFIX = "__TEST__"


class Safety(StrEnum):
    """Класс безопасности эндпоинта (влияет на требования к запуску)."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    HEAVY = "heavy"


SAFETY_LABELS: dict[str, str] = {
    Safety.READ: "только чтение",
    Safety.WRITE: "изменяет данные стенда",
    Safety.DESTRUCTIVE: "удаление данных",
    Safety.HEAVY: "ресурсоёмкая операция",
}

SAFETY_ICONS: dict[str, str] = {
    Safety.READ: "🟢",
    Safety.WRITE: "🟠",
    Safety.DESTRUCTIVE: "🔴",
    Safety.HEAVY: "🟣",
}

#: Порядок отображения классов в легенде (от безопасного к опасному).
SAFETY_ORDER: tuple[Safety, ...] = (Safety.READ, Safety.WRITE, Safety.HEAVY, Safety.DESTRUCTIVE)

#: Виды тела запроса.
BODY_NONE = "none"
BODY_JSON = "json"
BODY_MULTIPART = "multipart"

#: Виды ответа.
RESPONSE_JSON = "json"
RESPONSE_BINARY = "binary"
RESPONSE_CSV = "csv"
RESPONSE_EMPTY = "empty"

PATH = "path"
QUERY = "query"


@dataclass(frozen=True)
class ParamSpec:
    """Параметр эндпоинта (path или query) по спецификации."""

    name: str
    location: str = QUERY
    type: str = "string"
    format: str = ""
    required: bool = False
    description: str = ""
    enum: tuple[str, ...] = ()
    default: str = ""

    @property
    def is_path(self) -> bool:
        """True для path-параметра (подставляется в путь)."""
        return self.location == PATH

    @property
    def is_enum(self) -> bool:
        """True, если параметр принимает значения из перечисления."""
        return bool(self.enum)

    @property
    def is_boolean(self) -> bool:
        """True для параметра типа boolean."""
        return self.type == "boolean"

    @property
    def is_number(self) -> bool:
        """True для числового параметра (integer/number)."""
        return self.type in ("integer", "number")

    @property
    def label(self) -> str:
        """Подпись поля ввода для интерфейса."""
        marks = [self.type]
        if self.format:
            marks.append(self.format)
        if self.required:
            marks.append("обязательный")
        if self.enum:
            marks.append("из перечня")
        return f"{self.name} ({', '.join(marks)})"

    @property
    def format_hint(self) -> str:
        """Подсказка формата значения (`format` из спецификации) для поля ввода.

        Нужна там, где человек вводит значение руками: `GET /api/tasks/`
        принимает `start_date`/`end_date` только как `date-time`, и строка из
        одной даты отвечает 422 (`datetime_parsing`).
        """
        if self.format == "date-time":
            return (
                "Формат: `YYYY-MM-DDTHH:MM:SS` (дата-время; строка только с датой "
                "стендом отклоняется — 422)"
            )
        if self.format == "date":
            return "Формат: `YYYY-MM-DD`"
        if self.format == "uuid":
            return "Формат: UUID"
        return ""


@dataclass(frozen=True)
class EndpointSpec:
    """Описание одной операции испытуемого сервера (шаблон консоли запросов)."""

    key: str
    module: str
    method: str
    path: str
    summary: str
    operation_id: str = ""
    params: tuple[ParamSpec, ...] = ()
    body_kind: str = BODY_NONE
    body_required: bool = False
    body_fields: tuple[str, ...] = ()
    body_sample: str = ""
    response_kind: str = RESPONSE_JSON
    safety: Safety = Safety.READ
    note: str = ""

    @property
    def title(self) -> str:
        """`МЕТОД путь` — заголовок операции."""
        return f"{self.method} {self.path}"

    @property
    def menu_label(self) -> str:
        """Подпись операции в списке консоли."""
        return f"{self.method} {self.path} · {self.summary}"

    @property
    def safety_label(self) -> str:
        """Человекочитаемый класс безопасности."""
        return SAFETY_LABELS.get(self.safety, str(self.safety))

    @property
    def safety_icon(self) -> str:
        """Индикатор класса безопасности."""
        return SAFETY_ICONS.get(self.safety, "⚪")

    @property
    def requires_confirmation(self) -> bool:
        """True, если перед запуском нужно подтверждение оператора (NFR-T4)."""
        return self.safety != Safety.READ

    @property
    def is_heavy(self) -> bool:
        """True для ресурсоёмких операций (карточка запуска, FR-T10)."""
        return self.safety == Safety.HEAVY

    @property
    def is_destructive(self) -> bool:
        """True для операций удаления (двойное подтверждение)."""
        return self.safety == Safety.DESTRUCTIVE

    @property
    def is_binary(self) -> bool:
        """True, если операция отдаёт файл (не JSON)."""
        return self.response_kind == RESPONSE_BINARY

    @property
    def is_csv(self) -> bool:
        """True, если операция отдаёт текстовую выгрузку CSV."""
        return self.response_kind == RESPONSE_CSV

    @property
    def is_file(self) -> bool:
        """True, если ответ — файл (двоичный или CSV-выгрузка)."""
        return self.response_kind in (RESPONSE_BINARY, RESPONSE_CSV)

    @property
    def has_body(self) -> bool:
        """True, если у операции есть тело запроса."""
        return self.body_kind != BODY_NONE

    @property
    def body_fields_label(self) -> str:
        """Поля тела запроса одной строкой (подсказка оператору)."""
        return ", ".join(self.body_fields)

    def path_params(self) -> tuple[ParamSpec, ...]:
        """Path-параметры операции."""
        return tuple(param for param in self.params if param.is_path)

    def query_params(self) -> tuple[ParamSpec, ...]:
        """Query-параметры операции."""
        return tuple(param for param in self.params if not param.is_path)

    def param(self, name: str) -> ParamSpec | None:
        """Параметр по имени (или None)."""
        return next((param for param in self.params if param.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        """Представление операции для журнала и отчёта."""
        return {
            "key": self.key,
            "module": self.module,
            "method": self.method,
            "path": self.path,
            "summary": self.summary,
            "operation_id": self.operation_id,
            "safety": str(self.safety),
            "body_kind": self.body_kind,
            "response_kind": self.response_kind,
            "note": self.note,
        }


# --- BEGIN GENERATED: ENDPOINTS ---
# Блок сформирован командой `python -m tools.gen_endpoints` из `docs/SOM1.json`.
# Руками не правится: изменения теряются при следующем запуске генератора;
# расхождение со спецификацией проверяется тестом tests/unit/test_endpoints.py.
ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec(
        key="get /health",
        module="Система",
        method="GET",
        path="/health",
        summary="Проверка работоспособности сервиса",
        operation_id="health_check_health_get",
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
    ),
    EndpointSpec(
        key="get /version",
        module="Система",
        method="GET",
        path="/version",
        summary="Информация о текущей сборке",
        operation_id="version_version_get",
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
    ),
    EndpointSpec(
        key="post /api/data/file",
        module="File Import",
        method="POST",
        path="/api/data/file",
        summary="Upload Data File",
        operation_id="upload_data_file_api_data_file_post",
        body_kind=BODY_MULTIPART,
        body_required=True,
        body_fields=(
            "file: binary",
            "file_type: string",
            "description: string",
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
    ),
    EndpointSpec(
        key="get /api/data/files",
        module="File Import",
        method="GET",
        path="/api/data/files",
        summary="Get Files List",
        operation_id="get_files_list_api_data_files_get",
        params=(
            ParamSpec(
                name="id",
                location=QUERY,
                type="string",
                format="uuid",
                description="Filter by file ID",
            ),
            ParamSpec(
                name="file_name",
                location=QUERY,
                type="string",
                description="Filter by file name",
            ),
            ParamSpec(
                name="import_date",
                location=QUERY,
                type="string",
                format="date",
                description="Filter by import date (YYYY-MM-DD)",
            ),
            ParamSpec(
                name="file_type",
                location=QUERY,
                type="string",
                description="Filter by file type (RAW, LOADS, ONNX)",
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="limit/offset игнорируются сервером (P2) — пагинация клиентская",
    ),
    EndpointSpec(
        key="delete /api/{file_id}",
        module="File Import",
        method="DELETE",
        path="/api/{file_id}",
        summary="Удалить файл",
        operation_id="delete_file_api__file_id__delete",
        params=(
            ParamSpec(
                name="file_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.DESTRUCTIVE,
        note="маршрут удаления в корне /api/; batch-удаления пары нет",
    ),
    EndpointSpec(
        key="get /api/data/file/{file_id}/download",
        module="File Import",
        method="GET",
        path="/api/data/file/{file_id}/download",
        summary="Download File",
        operation_id="download_file_api_data_file__file_id__download_get",
        params=(
            ParamSpec(
                name="file_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_BINARY,
        safety=Safety.READ,
        note="ответ специфицирован как application/json со схемой {} — фактически application/octet-stream с Content-Disposition; нет Content-Length/Range/ETag; не-ASCII имена → 404 (дефект latin-1, P0)",
    ),
    EndpointSpec(
        key="post /api/datasets/",
        module="Datasets",
        method="POST",
        path="/api/datasets/",
        summary="Create Dataset",
        operation_id="create_dataset_api_datasets__post",
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "name: string",
            "description: string",
            "type: string",
        ),
        body_sample="""{
  "name": "<name>",
  "type": "<type>"
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
    ),
    EndpointSpec(
        key="get /api/datasets/",
        module="Datasets",
        method="GET",
        path="/api/datasets/",
        summary="List Datasets",
        operation_id="list_datasets_api_datasets__get",
        params=(
            ParamSpec(
                name="name",
                location=QUERY,
                type="string",
            ),
            ParamSpec(
                name="creation_date",
                location=QUERY,
                type="string",
                format="date-time",
            ),
            ParamSpec(
                name="description",
                location=QUERY,
                type="string",
            ),
            ParamSpec(
                name="type",
                location=QUERY,
                type="string",
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
    ),
    EndpointSpec(
        key="get /api/datasets/{dataset_id}",
        module="Datasets",
        method="GET",
        path="/api/datasets/{dataset_id}",
        summary="Get Dataset",
        operation_id="get_dataset_api_datasets__dataset_id__get",
        params=(
            ParamSpec(
                name="dataset_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="состав датасета и агрегаты (записи/чанки) отсутствуют (P1)",
    ),
    EndpointSpec(
        key="put /api/datasets/{dataset_id}",
        module="Datasets",
        method="PUT",
        path="/api/datasets/{dataset_id}",
        summary="Update Dataset",
        operation_id="update_dataset_api_datasets__dataset_id__put",
        params=(
            ParamSpec(
                name="dataset_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "name: string",
            "description: string",
        ),
        body_sample="""{
  "name": "<name>",
  "description": "<description>"
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
        note="состав датасета и агрегаты (записи/чанки) отсутствуют (P1)",
    ),
    EndpointSpec(
        key="delete /api/datasets/{dataset_id}",
        module="Datasets",
        method="DELETE",
        path="/api/datasets/{dataset_id}",
        summary="Delete Dataset",
        operation_id="delete_dataset_api_datasets__dataset_id__delete",
        params=(
            ParamSpec(
                name="dataset_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_EMPTY,
        safety=Safety.DESTRUCTIVE,
        note="состав датасета и агрегаты (записи/чанки) отсутствуют (P1)",
    ),
    EndpointSpec(
        key="post /api/datasets/fill/{dataset_id}",
        module="Datasets",
        method="POST",
        path="/api/datasets/fill/{dataset_id}",
        summary="Fill Dataset Async",
        operation_id="fill_dataset_async_api_datasets_fill__dataset_id__post",
        params=(
            ParamSpec(
                name="dataset_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "raw_file_ids: array",
            "markup_file_ids: array",
        ),
        body_sample="""{
  "raw_file_ids": [
    "<uuid>"
  ],
  "markup_file_ids": []
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.HEAVY,
        note="ответ без task_id (P1) — созданную задачу искать в GET /api/tasks/",
    ),
    EndpointSpec(
        key="get /api/loads/list",
        module="Loads",
        method="GET",
        path="/api/loads/list",
        summary="List Loads",
        operation_id="list_loads_api_loads_list_get",
        params=(
            ParamSpec(
                name="category",
                location=QUERY,
                type="string",
            ),
            ParamSpec(
                name="load_id",
                location=QUERY,
                type="string",
            ),
            ParamSpec(
                name="description_search",
                location=QUERY,
                type="string",
            ),
            ParamSpec(
                name="limit",
                location=QUERY,
                type="integer",
            ),
            ParamSpec(
                name="offset",
                location=QUERY,
                type="integer",
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="контракт 17.09.2026: параметр `ph_n` убран; в ответе нет `phase_connection`, схема ответа по-прежнему пуста (P1)",
    ),
    EndpointSpec(
        key="post /api/loads",
        module="Loads",
        method="POST",
        path="/api/loads",
        summary="Create Load",
        operation_id="create_load_api_loads_post",
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "load_id: string",
            "category: string",
            "description: string",
        ),
        body_sample="""{
  "load_id": "<load_id>",
  "category": "<category>"
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
    ),
    EndpointSpec(
        key="put /api/loads/{load_id}",
        module="Loads",
        method="PUT",
        path="/api/loads/{load_id}",
        summary="Update Load",
        operation_id="update_load_api_loads__load_id__put",
        params=(
            ParamSpec(
                name="load_id",
                location=PATH,
                type="string",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "category: string",
            "description: string",
        ),
        body_sample="""{
  "category": "<category>",
  "description": "<description>"
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
    ),
    EndpointSpec(
        key="post /api/ml_models/models",
        module="ML models: initialization",
        method="POST",
        path="/api/ml_models/models",
        summary="Create Model",
        operation_id="create_model_api_ml_models_models_post",
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "architecture: string",
            "name: string",
            "output_size: integer",
            "phase_type: string",
            "description: string",
            "signals: object",
            "signal_aliases: object",
            "config: object",
            "pruning_mask: string",
        ),
        body_sample="""{
  "architecture": "<architecture>",
  "name": "<name>",
  "output_size": 1,
  "phase_type": "three"
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
        note="поле signals не заполнено (P2); типы H5/REPORT_ZIP вне enum",
    ),
    EndpointSpec(
        key="get /api/ml_models/models",
        module="ML models: initialization",
        method="GET",
        path="/api/ml_models/models",
        summary="Get Models",
        operation_id="get_models_api_ml_models_models_get",
        params=(
            ParamSpec(
                name="model_id",
                location=QUERY,
                type="string",
                format="uuid",
                description="Фильтр по ID модели",
            ),
            ParamSpec(
                name="architecture",
                location=QUERY,
                type="string",
                description="Фильтр по архитектуре",
            ),
            ParamSpec(
                name="version",
                location=QUERY,
                type="string",
                description="Фильтр по версии",
            ),
            ParamSpec(
                name="name",
                location=QUERY,
                type="string",
                description="Фильтр по названию",
            ),
            ParamSpec(
                name="phase_type",
                location=QUERY,
                type="string",
                description="Фильтр по типу фазы",
            ),
            ParamSpec(
                name="output_size",
                location=QUERY,
                type="integer",
                description="Фильтр по количеству выходов",
            ),
            ParamSpec(
                name="device_id",
                location=QUERY,
                type="string",
                description="Фильтр по ID устройства",
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="поле signals не заполнено (P2); типы H5/REPORT_ZIP вне enum",
    ),
    EndpointSpec(
        key="post /api/ml_models/models/upload",
        module="ML models: initialization",
        method="POST",
        path="/api/ml_models/models/upload",
        summary="Upload Model",
        operation_id="upload_model_api_ml_models_models_upload_post",
        body_kind=BODY_MULTIPART,
        body_required=True,
        body_fields=(
            "file: binary",
            "metadata: string",
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
        note="multipart: файл модели (.h5) + поля формы",
    ),
    EndpointSpec(
        key="get /api/ml_models/architectures",
        module="ML models: initialization",
        method="GET",
        path="/api/ml_models/architectures",
        summary="Get Supported Architectures",
        operation_id="get_supported_architectures_api_ml_models_architectures_get",
        params=(
            ParamSpec(
                name="architecture",
                location=QUERY,
                type="string",
                description="Фильтр по конкретной архитектуре",
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
    ),
    EndpointSpec(
        key="delete /api/ml_models/models/{model_id}",
        module="ML models: initialization",
        method="DELETE",
        path="/api/ml_models/models/{model_id}",
        summary="Delete Model",
        operation_id="delete_model_api_ml_models_models__model_id__delete",
        params=(
            ParamSpec(
                name="model_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_EMPTY,
        safety=Safety.DESTRUCTIVE,
    ),
    EndpointSpec(
        key="get /api/ml_models/models/{model_id}",
        module="ML models: initialization",
        method="GET",
        path="/api/ml_models/models/{model_id}",
        summary="Get Model By Id",
        operation_id="get_model_by_id_api_ml_models_models__model_id__get",
        params=(
            ParamSpec(
                name="model_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
    ),
    EndpointSpec(
        key="get /api/ml_models/models/{model_id}/download_onnx",
        module="ML models: initialization",
        method="GET",
        path="/api/ml_models/models/{model_id}/download_onnx",
        summary="Download Model Onnx",
        operation_id="download_model_onnx_api_ml_models_models__model_id__download_onnx_get",
        params=(
            ParamSpec(
                name="model_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_BINARY,
        safety=Safety.READ,
        note="модель отдаётся файлом, но спецификация описывает ответ как application/json",
    ),
    EndpointSpec(
        key="post /api/ml_models/models/{model_id}/train",
        module="ML models: training",
        method="POST",
        path="/api/ml_models/models/{model_id}/train",
        summary="Train Model",
        operation_id="train_model_api_ml_models_models__model_id__train_post",
        params=(
            ParamSpec(
                name="model_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "dataset_id: uuid",
            "batch_size: integer",
            "epochs: integer",
            "learning_rate: number",
            "trained_model_name: string",
            "augmentation_noise_percentage: number",
            "augmentation_num_samples: integer",
            "preprocessing_converter: string",
            "quantization: object",
        ),
        body_sample="""{
  "dataset_id": "<uuid>",
  "batch_size": 32,
  "epochs": 50,
  "learning_rate": 1e-07,
  "preprocessing_converter": "direct"
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.HEAVY,
    ),
    EndpointSpec(
        key="post /api/ml_models/{model_id}/check",
        module="ML models: testing",
        method="POST",
        path="/api/ml_models/{model_id}/check",
        summary="Check Model",
        operation_id="check_model_api_ml_models__model_id__check_post",
        params=(
            ParamSpec(
                name="model_id",
                location=PATH,
                type="string",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "dataset_id: uuid",
            "threshold: number",
        ),
        body_sample="""{
  "dataset_id": "<uuid>",
  "threshold": 0.5
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.HEAVY,
    ),
    EndpointSpec(
        key="post /api/ml_models/models/{model_id}/inference",
        module="ML models: inference",
        method="POST",
        path="/api/ml_models/models/{model_id}/inference",
        summary="Inference",
        operation_id="inference_api_ml_models_models__model_id__inference_post",
        params=(
            ParamSpec(
                name="model_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "dataset_id: uuid",
            "threshold: number",
        ),
        body_sample="""{
  "dataset_id": "<uuid>",
  "threshold": 0.5
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.HEAVY,
    ),
    EndpointSpec(
        key="post /api/ml_models/models/{model_id}/inference/single",
        module="ML models: inference",
        method="POST",
        path="/api/ml_models/models/{model_id}/inference/single",
        summary="Inference Single",
        operation_id="inference_single_api_ml_models_models__model_id__inference_single_post",
        params=(
            ParamSpec(
                name="model_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "U_A_signal: string",
            "U_B_signal: string",
            "U_C_signal: string",
            "I_A_signal: string",
            "I_B_signal: string",
            "I_C_signal: string",
            "U_mult: integer",
            "U_dev: integer",
            "I_mult: integer",
            "I_dev: integer",
            "byte_order: string",
            "signal_bytes: integer",
            "threshold: number",
        ),
        body_sample="""{
  "U_A_signal": "<U_A_signal>",
  "U_B_signal": "<U_B_signal>",
  "U_C_signal": "<U_C_signal>",
  "I_A_signal": "<I_A_signal>",
  "I_B_signal": "<I_B_signal>",
  "I_C_signal": "<I_C_signal>",
  "U_mult": 721,
  "U_dev": 1,
  "I_mult": 25000,
  "I_dev": 66,
  "byte_order": "Little",
  "signal_bytes": 3,
  "threshold": 0.5
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
    ),
    EndpointSpec(
        key="post /api/ml_models/models/{model_id}/find_params/population",
        module="ML models: AutoML",
        method="POST",
        path="/api/ml_models/models/{model_id}/find_params/population",
        summary="Create Find Params Population From Model",
        operation_id="create_find_params_population_from_model_api_ml_models_models__model_id__find_params_population_post",
        params=(
            ParamSpec(
                name="model_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "name: string",
            "description: string",
            "population_config: object",
        ),
        body_sample="""{
  "name": "<name>",
  "population_config": {
    "include_base_in_population": true,
    "rules": [
      {
        "path": "<path>",
        "mode": "scale",
        "models_amount": 1,
        "uniform": true,
        "type": "range",
        "range": [
          "<range_item>"
        ]
      }
    ]
  }
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.HEAVY,
        note="создаёт десятки мутантов (новые модели, веса базовой НЕ наследуются); при сбое — полный откат; вариант «в один вызов» (POST того же пути без model_id) в контракте отсутствует",
    ),
    EndpointSpec(
        key="post /api/ml_models/find_params/population/{population_id}/selection",
        module="ML models: AutoML",
        method="POST",
        path="/api/ml_models/find_params/population/{population_id}/selection",
        summary="Run Find Params Selection",
        operation_id="run_find_params_selection_api_ml_models_find_params_population__population_id__selection_post",
        params=(
            ParamSpec(
                name="population_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        body_kind=BODY_JSON,
        body_required=True,
        body_fields=(
            "train_dataset_id: uuid",
            "test_dataset_id: uuid",
            "train_config: object",
            "test_config: object",
            "selection_config: object",
            "name: string",
        ),
        body_sample="""{
  "train_dataset_id": "<uuid>"
}""",
        response_kind=RESPONSE_JSON,
        safety=Safety.HEAVY,
        note="ответ 202: запускается задача `find-params` (обучение всех активных мутантов); `test_dataset_id` по умолчанию = train; без `selection_config` метки отбора не проставляются",
    ),
    EndpointSpec(
        key="delete /api/ml_models/find_params/population/{population_id}",
        module="ML models: AutoML",
        method="DELETE",
        path="/api/ml_models/find_params/population/{population_id}",
        summary="Delete Find Params Population",
        operation_id="delete_find_params_population_api_ml_models_find_params_population__population_id__delete",
        params=(
            ParamSpec(
                name="population_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.DESTRUCTIVE,
        note="DELETE — каскад: модели, сигналы, файлы, S3-блобы и ZIP-ы популяции, без предпросмотра (P2)",
    ),
    EndpointSpec(
        key="get /api/ml_models/find_params/population/{population_id}",
        module="ML models: AutoML",
        method="GET",
        path="/api/ml_models/find_params/population/{population_id}",
        summary="Get Find Params Population",
        operation_id="get_find_params_population_api_ml_models_find_params_population__population_id__get",
        params=(
            ParamSpec(
                name="population_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="DELETE — каскад: модели, сигналы, файлы, S3-блобы и ZIP-ы популяции, без предпросмотра (P2)",
    ),
    EndpointSpec(
        key="get /api/ml_models/find_params/population",
        module="ML models: AutoML",
        method="GET",
        path="/api/ml_models/find_params/population",
        summary="List Find Params Populations",
        operation_id="list_find_params_populations_api_ml_models_find_params_population_get",
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="список популяций без пагинации (сначала новые)",
    ),
    EndpointSpec(
        key="get /api/ml_models/find_params/population/{population_id}/mutated_models",
        module="ML models: AutoML",
        method="GET",
        path="/api/ml_models/find_params/population/{population_id}/mutated_models",
        summary="List Find Params Mutated Models",
        operation_id="list_find_params_mutated_models_api_ml_models_find_params_population__population_id__mutated_models_get",
        params=(
            ParamSpec(
                name="population_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
            ParamSpec(
                name="is_show_best",
                location=QUERY,
                type="boolean",
                description="Показывать мутантов с меткой 'best'.",
                default="True",
            ),
            ParamSpec(
                name="is_show_normal",
                location=QUERY,
                type="boolean",
                description="Показывать мутантов с меткой 'normal'.",
                default="True",
            ),
            ParamSpec(
                name="is_show_discarded",
                location=QUERY,
                type="boolean",
                description="Показывать мутантов с меткой 'discarded'.",
                default="True",
            ),
            ParamSpec(
                name="is_show_failed",
                location=QUERY,
                type="boolean",
                description="Показывать мутантов с меткой 'failed'.",
                default="True",
            ),
            ParamSpec(
                name="descending",
                location=QUERY,
                type="boolean",
                description="Сортировать по рангу от худшего к лучшему вместо от лучшего к худшему.",
                default="False",
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="фильтры по метке (best/normal/discarded/failed) и сортировка по рангу последнего `check_results`; мутанты без ранга — в конце",
    ),
    EndpointSpec(
        key="get /api/ml_models/find_params/population/{population_id}/selections",
        module="ML models: AutoML",
        method="GET",
        path="/api/ml_models/find_params/population/{population_id}/selections",
        summary="List Find Params Selections",
        operation_id="list_find_params_selections_api_ml_models_find_params_population__population_id__selections_get",
        params=(
            ParamSpec(
                name="population_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="итерации отбора (сначала старые); `train_config`/`test_config` — свободные объекты без схемы",
    ),
    EndpointSpec(
        key="get /api/ml_models/find_params/population/{population_id}/grid.csv",
        module="ML models: AutoML",
        method="GET",
        path="/api/ml_models/find_params/population/{population_id}/grid.csv",
        summary="Download Find Params Grid Csv",
        operation_id="download_find_params_grid_csv_api_ml_models_find_params_population__population_id__grid_csv_get",
        params=(
            ParamSpec(
                name="population_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
            ParamSpec(
                name="selection_id",
                location=QUERY,
                type="string",
                format="uuid",
                description="Итерация отбора, метрики которой попадут в файл. По умолчанию — последняя запись check_results каждого мутанта.",
            ),
        ),
        response_kind=RESPONSE_CSV,
        safety=Safety.READ,
        note="ответ `text/csv` (tidy-таблица «мутант × оси»); `selection_id` по умолчанию — последняя запись `check_results`; пустая ячейка метрики означает «нет данных», не ноль",
    ),
    EndpointSpec(
        key="post /api/tasks/test",
        module="Task service",
        method="POST",
        path="/api/tasks/test",
        summary="Run Test Task",
        operation_id="run_test_task_api_tasks_test_post",
        params=(
            ParamSpec(
                name="duration",
                location=QUERY,
                type="integer",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
    ),
    EndpointSpec(
        key="post /api/tasks/{task_id}/pause",
        module="Task service",
        method="POST",
        path="/api/tasks/{task_id}/pause",
        summary="Pause Task",
        operation_id="pause_task_api_tasks__task_id__pause_post",
        params=(
            ParamSpec(
                name="task_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
    ),
    EndpointSpec(
        key="post /api/tasks/{task_id}/interrupt",
        module="Task service",
        method="POST",
        path="/api/tasks/{task_id}/interrupt",
        summary="Interrupt Task",
        operation_id="interrupt_task_api_tasks__task_id__interrupt_post",
        params=(
            ParamSpec(
                name="task_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
    ),
    EndpointSpec(
        key="post /api/tasks/{task_id}/resume",
        module="Task service",
        method="POST",
        path="/api/tasks/{task_id}/resume",
        summary="Resume Task",
        operation_id="resume_task_api_tasks__task_id__resume_post",
        params=(
            ParamSpec(
                name="task_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.WRITE,
    ),
    EndpointSpec(
        key="get /api/tasks/",
        module="Task service",
        method="GET",
        path="/api/tasks/",
        summary="List Tasks",
        operation_id="list_tasks_api_tasks__get",
        params=(
            ParamSpec(
                name="task_type",
                location=QUERY,
                type="string",
                enum=(
                    "celery-test",
                    "training",
                    "dataset-fill",
                    "model-testing",
                    "find-params",
                    "inference",
                ),
            ),
            ParamSpec(
                name="status",
                location=QUERY,
                type="string",
                enum=(
                    "new",
                    "running",
                    "completed",
                    "interrupted",
                    "failed",
                    "pausing",
                    "paused",
                    "not_found",
                ),
            ),
            ParamSpec(
                name="start_date",
                location=QUERY,
                type="string",
                format="date-time",
            ),
            ParamSpec(
                name="end_date",
                location=QUERY,
                type="string",
                format="date-time",
            ),
            ParamSpec(
                name="limit",
                location=QUERY,
                type="integer",
                default="100",
            ),
            ParamSpec(
                name="offset",
                location=QUERY,
                type="integer",
                default="0",
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="limit по умолчанию 100; фильтры task_type/status/start_date/end_date",
    ),
    EndpointSpec(
        key="get /api/tasks/{task_id}",
        module="Task service",
        method="GET",
        path="/api/tasks/{task_id}",
        summary="Get Task Details",
        operation_id="get_task_details_api_tasks__task_id__get",
        params=(
            ParamSpec(
                name="task_id",
                location=PATH,
                type="string",
                format="uuid",
                required=True,
            ),
        ),
        response_kind=RESPONSE_JSON,
        safety=Safety.READ,
        note="статус not_found для неизвестной задачи (BR-R7)",
    ),
)
# --- END GENERATED: ENDPOINTS ---

#: Быстрый доступ к реестру: ключ операции → описание.
ENDPOINTS_BY_KEY: dict[str, EndpointSpec] = {spec.key: spec for spec in ENDPOINTS}

#: Модули спецификации в порядке появления.
MODULES: tuple[str, ...] = tuple(dict.fromkeys(spec.module for spec in ENDPOINTS))


def endpoint_keys() -> tuple[str, ...]:
    """Ключи всех операций реестра (порядок спецификации)."""
    return tuple(spec.key for spec in ENDPOINTS)


def find(key: str) -> EndpointSpec | None:
    """Операция по ключу (`get /api/data/files`) или по `operationId`."""
    spec = ENDPOINTS_BY_KEY.get(key)
    if spec is not None:
        return spec
    return next((item for item in ENDPOINTS if item.operation_id == key), None)


def by_module(module: str) -> tuple[EndpointSpec, ...]:
    """Операции одного модуля спецификации."""
    return tuple(spec for spec in ENDPOINTS if spec.module == module)


def by_operation_id(operation_id: str) -> EndpointSpec | None:
    """Операция по `operationId` спецификации."""
    return next((spec for spec in ENDPOINTS if spec.operation_id == operation_id), None)


def module_names() -> tuple[str, ...]:
    """Модули (теги спецификации) для выбора в интерфейсе."""
    return MODULES


def safety_legend() -> list[dict[str, str]]:
    """Легенда классов безопасности для интерфейса и отчёта."""
    return [
        {
            "safety": str(item),
            "icon": SAFETY_ICONS[item],
            "label": SAFETY_LABELS[item],
        }
        for item in SAFETY_ORDER
    ]


def render_path(spec: EndpointSpec, values: dict[str, str] | None = None) -> str:
    """Подставляет значения path-параметров в шаблон пути.

    Незаполненные параметры остаются в виде `{имя}` — оператор видит, что именно
    не подставлено (см. `missing_path_params`), и запрос не уходит на сервер.
    """
    resolved = values or {}
    path = spec.path
    for param in spec.path_params():
        raw = str(resolved.get(param.name, "")).strip()
        if raw:
            path = path.replace(f"{{{param.name}}}", raw)
    return path


def missing_path_params(
    spec: EndpointSpec, values: dict[str, str] | None = None
) -> tuple[str, ...]:
    """Имена path-параметров, значения которых оператор не заполнил."""
    resolved = values or {}
    return tuple(
        param.name for param in spec.path_params() if not str(resolved.get(param.name, "")).strip()
    )


def parse_body_sample(spec: EndpointSpec) -> dict[str, Any]:
    """Разбирает пример тела запроса из реестра в словарь (пустой словарь — нет тела)."""
    if not spec.body_sample:
        return {}
    try:
        payload = json.loads(spec.body_sample)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def requires_test_prefix(spec: EndpointSpec) -> bool:
    """True, если операция создаёт/удаляет сущности и требует префикса `__TEST__`.

    Загрузка файла, создание датасета/модели/нагрузки и любые удаления на общем
    стенде выполняются только с префиксом `__TEST__` (§9 ТЗ); запуск обучения,
    проверки и инференса префикса не требует — он не создаёт новых сущностей.
    """
    if spec.safety in (Safety.WRITE, Safety.DESTRUCTIVE):
        return True
    return False


def match_path(method: str, path: str) -> EndpointSpec | None:
    """Операция реестра, которой соответствует фактический запрос.

    Нужно для отчёта и истории консоли: по журналу (`method`, `path`) видно,
    какая именно операция спецификации выполнялась, даже если в пути были `id`.
    """
    normalized = method.strip().upper()
    candidates = [spec for spec in ENDPOINTS if spec.method == normalized]
    exact = next((spec for spec in candidates if spec.path == path), None)
    if exact is not None:
        return exact
    return next((spec for spec in candidates if re.fullmatch(_path_pattern(spec.path), path)), None)


def _path_pattern(template: str) -> str:
    """Регулярное выражение для шаблона пути: `{param}` → любой сегмент."""
    parts = re.split(r"(\{[^}]+\})", template)
    return "".join(
        "[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
        for part in parts
    )
