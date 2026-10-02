"""Тесты клиента файлов данных (FR-2, UC-02…UC-05) — потоковая загрузка `path=` (Э2.1).

Сценарии проверок грузят файлы байтами (`content=`) и должны продолжать работать
без изменений; пульт грузит боевые RAW **путём** (`path=`) — тогда `httpx` стримит
файл с диска, и 2 ГБ не занимают память пульта (`llm_tasks/64` §2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from client.files import FilesApi

FILE_ID = "5932482e-0000-4000-8000-000000000002"

SAVED: dict[str, Any] = {
    "id": FILE_ID,
    "file_name": "raw-01.csv",
    "size": 42,
    "s3_path": "RAW/2026/10/02/raw-01.csv",
    "import_date": "2026-10-02T10:00:00",
    "file_type": "RAW",
}


def _api(client_factory: Any, seen: dict[str, Any]) -> FilesApi:
    """Клиент с подменённым транспортом: запрос запоминается, ответ — метаданные файла."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.read()
        return httpx.Response(201, json=SAVED)

    return FilesApi(client_factory(handler))


def test_upload_from_bytes_keeps_previous_behaviour(client_factory: Any) -> None:
    """Байты (как в сценариях проверок) уходят частью multipart с типом CSV."""
    seen: dict[str, Any] = {}
    api = _api(client_factory, seen)

    saved = api.upload(file_name="raw-01.csv", content=b"chunkID;timestamp\n", file_type="RAW")

    assert str(saved.id) == FILE_ID and saved.file_name == "raw-01.csv"
    assert seen["path"] == "/api/data/file"
    assert "multipart/form-data" in str(seen["content_type"])
    assert b'name="file_type"' in seen["body"] and b"RAW" in seen["body"]
    assert b'filename="raw-01.csv"' in seen["body"]
    assert b"text/csv" in seen["body"]
    assert b"chunkID;timestamp\n" in seen["body"]


def test_upload_from_path_streams_file(client_factory: Any, tmp_path: Path) -> None:
    """Путь на диске: содержимое уходит потоком, имя берётся из файла."""
    seen: dict[str, Any] = {}
    api = _api(client_factory, seen)
    source = tmp_path / "raw 02.csv"
    source.write_bytes(b"chunkID;U_A_signal\n2;BBBB\n")

    saved = api.upload(path=source, file_type="RAW", description="боевой датасет")

    assert str(saved.id) == FILE_ID
    assert b'filename="raw 02.csv"' in seen["body"]
    assert b"chunkID;U_A_signal\n2;BBBB\n" in seen["body"]
    assert "боевой датасет".encode() in seen["body"]


def test_upload_from_path_allows_name_override(client_factory: Any, tmp_path: Path) -> None:
    """Имя для стенда можно задать отдельно (оператор узнаёт файл по своей метке)."""
    seen: dict[str, Any] = {}
    api = _api(client_factory, seen)
    source = tmp_path / "tmp-ab12.csv"
    source.write_bytes(b"a;b\n")

    api.upload(file_name="skfu-train-raw-01.csv", path=source, file_type="RAW")

    assert b'filename="skfu-train-raw-01.csv"' in seen["body"]


def test_upload_requires_exactly_one_source(client_factory: Any, tmp_path: Path) -> None:
    """Два источника или ни одного — ошибка в коде пульта, а не запрос на стенд."""
    seen: dict[str, Any] = {}
    api = _api(client_factory, seen)
    source = tmp_path / "a.csv"
    source.write_bytes(b"a")

    with pytest.raises(ValueError, match="ровно один источник"):
        api.upload(content=b"a", path=source, file_type="RAW")
    with pytest.raises(ValueError, match="ровно один источник"):
        api.upload(file_type="RAW")
    with pytest.raises(ValueError, match="file_name"):
        api.upload(content=b"a", file_type="RAW")

    assert seen == {}, "некорректный вызов не должен доходить до стенда"
