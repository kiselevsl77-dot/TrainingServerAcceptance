"""Методы модуля «Loads» (FR-3, UC-06…UC-08).

Эндпоинты `SOM1.json`:
    GET  /api/loads/list      — реестр нагрузок (фильтры и пагинация)
    POST /api/loads           — создание нагрузки
    PUT  /api/loads/{load_id} — правка нагрузки

Замечания к API, выявленные на живом сервисе (см. документ этапа 2):
    * ответ списка описан в спецификации пустой схемой, фактически приходит
      `{loads, result_size, limit, offset}` и **без `phase_connection`**;
    * фильтр `ph_n` присутствует в спецификации, но ничего не фильтрует
      (данных о фазе в реестре нет);
    * `load_id` и `category` фильтруются **точно** (регистрозависимо),
      `description_search` — по подстроке;
    * эндпоинта удаления нагрузки нет, поэтому «ошибочную» нагрузку через API
      удалить нельзя (замечание к API).

Методы `create_load`/`update_load` реализованы как задел UC-07/UC-08: в обычном
сценарии реестр наполняется данными разметки, и UI работает с ним «только для чтения»
(замечание №2 к макету).
"""

from __future__ import annotations

from typing import Any

from client.http import ApiHttpClient
from client.schemas import LoadDevice, LoadsListResponse, UpdateLoadRequest


class LoadsApi:
    """Доступ к эндпоинтам реестра нагрузок."""

    def __init__(self, client: ApiHttpClient) -> None:
        self._client = client

    # -- чтение --------------------------------------------------------------
    def list_loads(
        self,
        *,
        load_id: str | None = None,
        category: str | None = None,
        description_search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> LoadsListResponse:
        """GET /api/loads/list — реестр нагрузок с фильтрами и пагинацией (UC-06).

        Args:
            load_id: точное совпадение `load_id` (регистрозависимо).
            category: точное совпадение `category` (регистрозависимо).
            description_search: поиск по подстроке в `description`.
            limit: размер страницы (серверная пагинация).
            offset: смещение от начала списка.

        Note:
            `ph_n` не передаётся: на живом сервисе параметр не влияет на результат
            (фильтр заявлен в спецификации, но данных о фазе в реестре нет).
        """
        params: dict[str, Any] = {
            "load_id": load_id or None,
            "category": category or None,
            "description_search": description_search or None,
            "limit": limit,
            "offset": offset,
        }

        payload = self._client.get("/api/loads/list", params=_drop_empty(params))
        if payload is None:
            return LoadsListResponse()

        return LoadsListResponse.model_validate(payload)

    # -- запись (задел UC-07/UC-08) ------------------------------------------
    def create_load(
        self,
        *,
        load_id: str,
        phase_connection: str,
        category: str,
        description: str | None = None,
    ) -> None:
        """POST /api/loads — создание нагрузки (UC-07)."""
        request = LoadDevice(
            load_id=load_id,
            phase_connection=phase_connection,
            category=category,
            description=description,
        )
        self._client.post("/api/loads", json=request.model_dump(exclude_none=True))

    def update_load(
        self,
        load_id: str,
        *,
        phase_connection: str | None = None,
        category: str | None = None,
        description: str | None = None,
    ) -> None:
        """PUT /api/loads/{load_id} — правка нагрузки (UC-08)."""
        request = UpdateLoadRequest(
            phase_connection=phase_connection,
            category=category,
            description=description,
        )
        self._client.put(
            f"/api/loads/{load_id}",
            json=request.model_dump(exclude_none=True),
        )


def _drop_empty(params: dict[str, Any]) -> dict[str, Any]:
    """Убирает незаполненные фильтры, чтобы не отправлять пустые query-параметры."""
    return {key: value for key, value in params.items() if value not in (None, "")}
