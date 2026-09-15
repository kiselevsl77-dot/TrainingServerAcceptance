"""Методы модуля «Datasets» (FR-T5 пульта, UC-09…UC-14).

Эндпоинты `SOM1.json`:
    POST   /api/datasets/                    — создание датасета (`name`, `description`, `type`);
    GET    /api/datasets/                    — список (фильтры `name`, `creation_date`,
                                               `description`, `type`);
    GET    /api/datasets/{dataset_id}        — метаданные датасета;
    PUT    /api/datasets/{dataset_id}        — правка (`name`, `description`);
    DELETE /api/datasets/{dataset_id}        — удаление;
    POST   /api/datasets/fill/{dataset_id}   — наполнение (async, 202).

Замечания к API, важные для испытаний:
    * `DatasetType` в спецификации — `const: "direct_fill"` (единственное значение);
    * `GET /api/datasets/{id}` возвращает только метаданные: связи «датасет ↔ файлы»
      и структурных характеристик (записи/чанки) в API нет — замечание к API;
    * `fill` не возвращает `task_id`, поэтому связанная задача ищется в
      `GET /api/tasks/` (монитор задач, этап T4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from client.http import ApiHttpClient
from client.schemas import (
    DatasetCreate,
    DatasetFillRequest,
    DatasetListResponse,
    DatasetResponse,
    DatasetType,
    DatasetUpdate,
)


def _drop_empty(params: dict[str, Any]) -> dict[str, Any]:
    """Убирает незаполненные фильтры, чтобы не отправлять пустые query-параметры."""
    return {key: value for key, value in params.items() if value not in (None, "")}


def _iso(value: datetime | str | None) -> str | None:
    """Приводит дату/время к строке ISO (для query-параметра `creation_date`)."""
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else value


class DatasetsApi:
    """Доступ к эндпоинтам датасетов."""

    def __init__(self, client: ApiHttpClient) -> None:
        self._client = client

    # -- чтение --------------------------------------------------------------
    def list_datasets(
        self,
        *,
        name: str | None = None,
        creation_date: datetime | str | None = None,
        description: str | None = None,
        dataset_type: DatasetType | None = None,
    ) -> DatasetListResponse:
        """GET /api/datasets/ — список датасетов с фильтрами (UC-12)."""
        params: dict[str, Any] = {
            "name": name or None,
            "creation_date": _iso(creation_date),
            "description": description or None,
            "type": dataset_type,
        }
        payload = self._client.get("/api/datasets/", params=_drop_empty(params))
        if payload is None:
            return DatasetListResponse(datasets=[], count=0)
        return DatasetListResponse.model_validate(payload)

    def get_dataset(self, dataset_id: UUID | str) -> DatasetResponse:
        """GET /api/datasets/{dataset_id} — метаданные датасета (UC-13)."""
        payload = self._client.get(f"/api/datasets/{dataset_id}")
        return DatasetResponse.model_validate(payload)

    # -- запись --------------------------------------------------------------
    def create_dataset(
        self,
        *,
        name: str,
        description: str | None = None,
        dataset_type: DatasetType = "direct_fill",
    ) -> DatasetResponse:
        """POST /api/datasets/ — создание датасета (UC-09)."""
        body = DatasetCreate(name=name, description=description or None, type=dataset_type)
        payload = self._client.post("/api/datasets/", json=body.model_dump(exclude_none=True))
        return DatasetResponse.model_validate(payload)

    def update_dataset(
        self,
        dataset_id: UUID | str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> DatasetResponse:
        """PUT /api/datasets/{dataset_id} — правка наименования/описания (UC-13)."""
        body = DatasetUpdate(name=name or None, description=description or None)
        payload = self._client.put(
            f"/api/datasets/{dataset_id}",
            json=body.model_dump(exclude_none=True),
        )
        return DatasetResponse.model_validate(payload)

    def delete_dataset(self, dataset_id: UUID | str) -> Any:
        """DELETE /api/datasets/{dataset_id} — удаление датасета (UC-13)."""
        return self._client.delete(f"/api/datasets/{dataset_id}")

    # -- наполнение ----------------------------------------------------------
    def fill_dataset(
        self,
        dataset_id: UUID | str,
        *,
        raw_file_ids: list[UUID | str],
        markup_file_ids: list[UUID | str] | None = None,
    ) -> Any:
        """POST /api/datasets/fill/{dataset_id} — наполнение датасета (UC-14, async).

        Note:
            Ответ (202) не содержит `task_id`; мониторинг задачи `dataset-fill`
            выполняется через `GET /api/tasks/` (замечание к API).
        """
        body = DatasetFillRequest(
            raw_file_ids=[UUID(str(item)) for item in raw_file_ids],
            markup_file_ids=[UUID(str(item)) for item in (markup_file_ids or [])],
        )
        return self._client.post(
            f"/api/datasets/fill/{dataset_id}",
            json=body.model_dump(mode="json"),
        )
