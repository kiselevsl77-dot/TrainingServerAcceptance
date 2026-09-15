"""Методы модуля «ML models» — минимальный набор для Этапа 2 (FR-3).

Полный экран моделей (FR-5, UC-15…UC-20) реализуется на Этапе 4; здесь нужен
только список моделей, чтобы получить «набор нагрузок модели»: у живых моделей
поле `signals` (привязка `signal_num` → `device_id[]`) пусто, а классы описаны
в `signal_aliases` (`{"0": "Майнер", "1": "Не майнер"}`).

Эндпоинт: `GET /api/ml_models/models` с фильтрами
`model_id`, `architecture`, `version`, `name`, `phase_type`, `output_size`, `device_id`.
"""

from __future__ import annotations

from typing import Any

from client.http import ApiHttpClient
from client.schemas import ModelListResponse, ModelMetadata


class ModelsApi:
    """Доступ к эндпоинтам каталога моделей (пока — только чтение списка)."""

    def __init__(self, client: ApiHttpClient) -> None:
        self._client = client

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


def _drop_empty(params: dict[str, Any]) -> dict[str, Any]:
    """Убирает незаполненные фильтры, чтобы не отправлять пустые query-параметры."""
    return {key: value for key, value in params.items() if value not in (None, "")}
