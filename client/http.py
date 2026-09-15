"""Обёртка httpx.Client с единообразной обработкой ответов и ошибок.

Дополнительно к JSON-запросам поддерживается скачивание файлов
(`get_binary`): сервер отдаёт `application/octet-stream` с именем файла
в заголовке `Content-Disposition` (в `SOM1.json` ответ специфицирован как
`application/json` со схемой `{}` — расхождение зафиксировано как замечание к API).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import httpx
from loguru import logger

from client.errors import (
    ApiError,
    NotFoundError,
    ServerUnavailableError,
    ValidationApiError,
)
from client.schemas import HTTPValidationError

_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*[^']*'[^']*'(?P<value>[^;]+)", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\s*=\s*"?(?P<value>[^";]+)"?', re.IGNORECASE)


@dataclass(frozen=True)
class BinaryResponse:
    """Двоичный ответ сервера (результат скачивания файла)."""

    content: bytes
    file_name: str | None = None
    media_type: str | None = None

    @property
    def size(self) -> int:
        """Размер полученного содержимого в байтах."""
        return len(self.content)


def filename_from_content_disposition(value: str | None) -> str | None:
    """Извлекает имя файла из `Content-Disposition` (RFC 6266, в т.ч. `filename*`)."""
    if not value:
        return None

    match = _FILENAME_STAR_RE.search(value)
    if match:
        return unquote(match.group("value").strip()) or None

    match = _FILENAME_RE.search(value)
    if match:
        return match.group("value").strip() or None

    return None


def error_detail(response: httpx.Response) -> str:
    """Извлекает осмысленный текст ошибки из тела ответа (NFR-2).

    FastAPI отдаёт ошибки как `{"detail": "..."}` (400/404) либо как
    `{"detail": [ ... ]}` (422); для не-JSON тел возвращается сырой текст.
    """
    text = response.text
    try:
        payload = response.json()
    except ValueError:
        return text

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            return "; ".join(str(item) for item in detail)

    return text


class ApiHttpClient:
    """Синхронный httpx-клиент к серверу обучения."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
        )

    # -- управление ресурсом ------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- низкоуровневый запрос ----------------------------------------------
    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Выполняет запрос и преобразует HTTP-ошибки в исключения клиента."""
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.error("Запрос {} {} не выполнен: {}", method, path, exc)
            raise ServerUnavailableError(str(exc)) from exc

        self._raise_for_status(response)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 404:
            raise NotFoundError(404, error_detail(response))
        if response.status_code == 422:
            raise ValidationApiError(422, ApiHttpClient._parse_validation_errors(response))
        if response.status_code >= 400:
            raise ApiError(response.status_code, error_detail(response))

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._decode(self._send(method, path, **kwargs))

    @staticmethod
    def _parse_validation_errors(response: httpx.Response) -> list[Any]:
        try:
            payload = HTTPValidationError.model_validate(response.json())
            return payload.detail
        except Exception:  # noqa: BLE001 — тело может быть не JSON
            return []

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.content

    # -- HTTP-методы ---------------------------------------------------------
    def get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    def get_binary(self, path: str, *, timeout: float | None = None) -> BinaryResponse:
        """GET для скачивания файла: содержимое + имя из `Content-Disposition`.

        Args:
            path: относительный путь эндпоинта (`/api/data/file/{id}/download`).
            timeout: таймаут запроса; для больших файлов задаётся отдельно
                (`TRAINING_SERVER_DOWNLOAD_TIMEOUT`), т.к. сервер не поддерживает
                `Range` и отдаёт файл целиком.
        """
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout

        response = self._send("GET", path, **kwargs)
        media_type = (response.headers.get("content-type") or "").split(";")[0].strip()

        return BinaryResponse(
            content=response.content,
            file_name=filename_from_content_disposition(
                response.headers.get("content-disposition")
            ),
            media_type=media_type or None,
        )

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._request("DELETE", path, **kwargs)
