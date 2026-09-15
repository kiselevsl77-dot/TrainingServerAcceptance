"""Исключения клиента API.

Единый способ представления ошибок сетевого слоя и HTTP-ошибок сервера:
сетевые сбои/таймауты отделены от валидационных (422) и «не найдено» (404),
что позволяет UI показывать осмысленные сообщения оператору (NFR-2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from client.schemas import ValidationError


class ClientError(Exception):
    """Базовое исключение клиента API."""


class ServerUnavailableError(ClientError):
    """Сервер недоступен: сеть, таймаут, ошибка соединения.

    Соответствует сценарию UC-01 «сервер недоступен».
    """


class ApiError(ClientError):
    """HTTP-ошибка сервера (4xx/5xx) вне отдельно обработанных случаев."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail or f"HTTP {status_code}")


class NotFoundError(ApiError):
    """Ресурс не найден (404)."""


class ValidationApiError(ApiError):
    """Ошибка валидации 422 с разобранным `HTTPValidationError.detail[]`."""

    def __init__(self, status_code: int, errors: list[ValidationError]) -> None:
        self.errors = errors
        super().__init__(status_code, self._format(errors))

    @staticmethod
    def _format(errors: list[ValidationError]) -> str:
        if not errors:
            return "Ошибка валидации запроса"
        return "; ".join(f"{e.msg} (поле: {'.'.join(map(str, e.loc))})" for e in errors)
