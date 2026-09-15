"""Методы модуля «File Import» (FR-2, UC-02…UC-05).

Эндпоинты `SOM1.json`:
    POST   /api/data/file                    — загрузка файла (multipart/form-data)
    GET    /api/data/files                   — список файлов с фильтрами
    GET    /api/data/file/{file_id}/download — скачивание файла
    DELETE /api/{file_id}                    — удаление файла и его метаданных

Замечания к API, выявленные при работе с живым сервисом (см. документ этапа 1):
    * `GET /api/data/files` не поддерживает `limit`/`offset` (в спецификации их нет,
      сервер их игнорирует) — пагинация выполняется на стороне UI;
    * ответ `download` в спецификации описан как `application/json` со схемой `{}`,
      фактически отдаётся `application/octet-stream` с `Content-Disposition`
      и без `Content-Length`; заголовок `Range` не поддерживается;
    * загрузки субдатасета архивом (zip) в API нет — архив распаковывается
      на стороне UI и загружается пофайлово.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from client.http import ApiHttpClient, BinaryResponse
from client.schemas import FileMetadataListResponse, FileMetadataResponse

#: MIME-тип по умолчанию, если расширение файла неизвестно.
DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: MIME-типы, отправляемые в multipart-запрос загрузки.
#:
#: Сервер проверяет тип части запроса: при `application/octet-stream` для CSV
#: `POST /api/data/file` отвечает `400 Only CSV files are allowed`, поэтому
#: для `.csv` отправляется `text/csv` (замечание к API: проверяется MIME-тип,
#: а не содержимое файла). Таблица задана явно вместо `mimetypes.guess_type`,
#: потому что на Windows реестр отдаёт для `.csv` `application/vnd.ms-excel`.
CONTENT_TYPES: dict[str, str] = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".zip": "application/zip",
    ".h5": DEFAULT_CONTENT_TYPE,
    ".onnx": DEFAULT_CONTENT_TYPE,
}


def guess_content_type(file_name: str) -> str:
    """Определяет MIME-тип файла по расширению (не зависит от платформы)."""
    return CONTENT_TYPES.get(Path(file_name).suffix.lower(), DEFAULT_CONTENT_TYPE)


class FilesApi:
    """Доступ к эндпоинтам модуля файлов данных."""

    def __init__(self, client: ApiHttpClient, *, download_timeout: float | None = None) -> None:
        self._client = client
        self._download_timeout = download_timeout

    # -- загрузка ------------------------------------------------------------
    def upload(
        self,
        *,
        file_name: str,
        content: bytes,
        file_type: str,
        description: str | None = None,
    ) -> FileMetadataResponse:
        """POST /api/data/file — загрузка файла (UC-02).

        Args:
            file_name: имя файла (в хранилище сохраняется только базовое имя).
            content: содержимое файла.
            file_type: `RAW`/`LOADS`/`ONNX` (`FileType`).
            description: необязательное описание.

        Returns:
            Метаданные сохранённого файла (ответ 201).
        """
        data: dict[str, str] = {"file_type": file_type}
        if description:
            data["description"] = description

        payload: dict[str, Any] = self._client.post(
            "/api/data/file",
            files={"file": (Path(file_name).name, content, guess_content_type(file_name))},
            data=data,
        )
        return FileMetadataResponse.model_validate(payload)

    # -- список --------------------------------------------------------------
    def list_files(
        self,
        *,
        file_id: UUID | str | None = None,
        file_name: str | None = None,
        import_date: date | str | None = None,
        file_type: str | None = None,
    ) -> FileMetadataListResponse:
        """GET /api/data/files — список файлов с фильтрами (UC-03).

        `file_name` — подстрочный поиск по имени, `import_date` — дата в формате
        `YYYY-MM-DD`, `file_type` — `RAW`/`LOADS`/`ONNX` (фактически и другие значения).
        """
        params: dict[str, Any] = {
            "id": str(file_id) if file_id else None,
            "file_name": file_name or None,
            "import_date": (
                import_date.isoformat() if isinstance(import_date, date) else import_date or None
            ),
            "file_type": file_type or None,
        }

        payload = self._client.get("/api/data/files", params=_drop_empty(params))
        if payload is None:
            return FileMetadataListResponse(files=[], count=0)
        return FileMetadataListResponse.model_validate(payload)

    # -- скачивание/удаление -------------------------------------------------
    def download(self, file_id: UUID | str) -> BinaryResponse:
        """GET /api/data/file/{file_id}/download — скачивание файла (UC-04)."""
        return self._client.get_binary(
            f"/api/data/file/{file_id}/download",
            timeout=self._download_timeout,
        )

    def delete(self, file_id: UUID | str) -> None:
        """DELETE /api/{file_id} — удаление файла из S3 и его метаданных (UC-05)."""
        self._client.delete(f"/api/{file_id}")


def _drop_empty(params: dict[str, Any]) -> dict[str, Any]:
    """Убирает незаполненные фильтры, чтобы не отправлять пустые query-параметры."""
    return {key: value for key, value in params.items() if value not in (None, "")}
