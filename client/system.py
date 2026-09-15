"""Методы модуля «Система» (FR-1, UC-01): /health и /version."""

from __future__ import annotations

from typing import Any

from client.http import ApiHttpClient


class SystemApi:
    """Доступ к системным эндпоинтам сервера обучения."""

    def __init__(self, client: ApiHttpClient) -> None:
        self._client = client

    def health(self) -> bool:
        """GET /health — проверка работоспособности сервиса.

        Возвращает True, если сервер ответил 200 OK.
        """
        self._client.get("/health")
        return True

    def version(self) -> dict[str, Any]:
        """GET /version — информация о текущей сборке.

        В спецификации схема ответа пустая (`{}`), поэтому возвращаем
        разобранный JSON как dict (или пустой dict при пустом теле).
        """
        data = self._client.get("/version")
        return data if isinstance(data, dict) else {}
