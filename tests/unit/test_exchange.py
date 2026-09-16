"""Тесты движка консоли запросов (FR-T3): выполнение, чтение ответа, журнал.

Сеть подменяется `httpx.MockTransport`, журнал — настоящий (`LoggingTransport`),
поэтому проверяется и сам обмен, и его след в журнале (метка проверки, номер записи).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from acceptance.config import PultConfig
from acceptance.exchange import (
    ERROR_API,
    ERROR_NETWORK,
    ERROR_NOT_FOUND,
    ERROR_REQUEST,
    ERROR_VALIDATION,
    MultipartPayload,
    build_console_client,
    charset_from_content_type,
    decode_body,
    execute_request,
    is_text_content,
)
from acceptance.http_log import Journal
from client.settings import TrainingServerSettings

BODY_LIMIT = 120


def _console(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    body_limit: int = BODY_LIMIT,
) -> tuple[httpx.Client, Journal]:
    """Клиент консоли с подменённым транспортом и настоящим журналом."""
    journal = Journal(max_records=50)
    settings = TrainingServerSettings(base_url="http://test.local", timeout=5.0)
    client = build_console_client(
        settings,
        journal,
        config=PultConfig(body_limit=body_limit),
        inner=httpx.MockTransport(handler),
    )
    return client, journal


def test_get_json_captures_status_headers_body_and_link_to_journal():
    """GET с JSON-ответом: статус, заголовки, тело, номер записи журнала, метка."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok"},
            headers={"content-type": "application/json", "etag": 'W/"1"'},
        )

    client, journal = _console(handler)
    result = execute_request(
        client,
        method="get",
        path="/health",
        label="TC-SYS-01",
        journal=journal,
        body_limit=BODY_LIMIT,
    )

    assert result.ok
    assert result.status == 200
    assert result.body_json == {"status": "ok"}
    assert result.headers["etag"] == 'W/"1"'
    assert result.content_type == "application/json"
    assert result.duration_ms >= 0
    assert result.size_bytes > 0
    assert result.journal_seq == journal.records[-1].seq
    assert journal.records[-1].label == "TC-SYS-01"
    assert result.error == ""
    assert result.status_label == "200"
    assert result.important_headers()["etag"] == 'W/"1"'
    client.close()


def test_query_parameters_are_passed_and_empty_values_dropped():
    """Query-параметры уходят в запрос, пустые — не отправляются."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.query.decode()
        return httpx.Response(200, json={"files": [], "count": 0})

    client, journal = _console(handler)
    result = execute_request(
        client,
        method="GET",
        path="/api/data/files",
        query={"file_name": "Antminer", "file_type": "", "id": None},
        journal=journal,
    )

    assert captured["query"] == "file_name=Antminer"
    assert result.query == {"file_name": "Antminer"}
    assert result.url == "/api/data/files?file_name=Antminer"
    client.close()


def test_json_body_is_sent_and_stored_for_reproduction():
    """JSON-тело отправляется и сохраняется строкой (для замечаний и curl)."""
    received: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "11111111-1111-4111-8111-111111111111"})

    client, journal = _console(handler)
    payload = {"name": "__TEST__dataset", "type": "direct_fill"}
    result = execute_request(
        client, method="POST", path="/api/datasets/", json_body=payload, journal=journal
    )

    assert received == payload
    assert json.loads(result.request_body) == payload
    assert result.status_label == "201"
    assert "__TEST__dataset" in result.as_curl("https://energomera.ai-center.online")
    client.close()


def test_multipart_upload_sends_file_and_form_fields():
    """Multipart-загрузка: файл с MIME-типом CSV и текстовые поля формы."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content.decode("utf-8", errors="replace")
        return httpx.Response(201, json={"id": "11111111-1111-4111-8111-111111111111"})

    client, journal = _console(handler)
    multipart = MultipartPayload(
        file_name="__TEST__probe.raw.csv",
        content=b"time,u\n0,1\n",
        file_type="RAW",
        description="проверка загрузки",
    )
    result = execute_request(
        client, method="POST", path="/api/data/file", multipart=multipart, journal=journal
    )

    assert "multipart/form-data" in captured["content_type"]
    assert 'filename="__TEST__probe.raw.csv"' in captured["body"]
    assert "text/csv" in captured["body"]
    assert 'name="file_type"' in captured["body"]
    assert result.request_body.startswith("<multipart: __TEST__probe.raw.csv")
    assert "проверка загрузки" in result.request_body
    client.close()


def test_server_errors_are_classified_without_raising():
    """404/422/500 становятся полями результата, исключения не пролетают наружу."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/404"):
            return httpx.Response(404, json={"detail": "Not found"})
        if request.url.path.endswith("/422"):
            return httpx.Response(
                422, json={"detail": [{"loc": ["body", "name"], "msg": "Field required"}]}
            )
        return httpx.Response(500, text="internal error")

    client, journal = _console(handler)

    missing = execute_request(client, method="GET", path="/api/404", journal=journal)
    invalid = execute_request(client, method="POST", path="/api/422", json_body={}, journal=journal)
    failed = execute_request(client, method="GET", path="/api/500", journal=journal)

    assert missing.status == 404
    assert missing.error_kind == ERROR_NOT_FOUND
    assert missing.error == "Not found"
    assert invalid.error_kind == ERROR_VALIDATION
    assert "Field required" in invalid.error
    assert failed.error_kind == ERROR_API
    assert "internal error" in failed.error
    assert missing.is_error
    assert not failed.ok
    assert len(journal.errors) == 3
    client.close()


def test_network_error_is_returned_as_result():
    """Обрыв соединения: результат с классом «сервер недоступен», без исключения."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет соединения", request=request)

    client, journal = _console(handler)
    result = execute_request(client, method="GET", path="/health", journal=journal)

    assert result.status is None
    assert result.status_label == "нет ответа"
    assert result.error_kind == ERROR_NETWORK
    assert "ConnectError" in result.error
    assert result.error_label == "сервер недоступен (сеть, таймаут)"
    assert journal.records[-1].error
    client.close()


def test_invalid_request_is_reported_as_request_error():
    """Некорректный URL не роняет консоль (класс «запрос не отправлен»)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - не вызывается
        return httpx.Response(200, json={})

    client, journal = _console(handler)
    result = execute_request(
        client, method="GET", path="/api/data/file/\x07bad/download", journal=journal
    )

    assert result.error_kind == ERROR_REQUEST
    assert result.status is None
    assert result.error_label == "запрос не отправлен (некорректные параметры)"
    client.close()


def test_binary_download_reports_file_name_and_size():
    """Скачивание файла: результат — файл, имя из `Content-Disposition`, тело не хранится."""
    content = b"\x00\x01\x02binary"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content,
            headers={
                "content-type": "application/octet-stream",
                "content-disposition": "attachment; filename*=UTF-8''Antminer_S19.markup.csv",
            },
        )

    client, journal = _console(handler)
    result = execute_request(
        client,
        method="GET",
        path="/api/data/file/11111111-1111-4111-8111-111111111111/download",
        binary=True,
        journal=journal,
        keep_content=True,
    )

    assert result.binary
    assert result.file_name == "Antminer_S19.markup.csv"
    assert result.size_bytes == len(content)
    assert result.content == content
    assert result.body_text == ""
    assert "файл" in result.body_preview()
    # содержимое файла не попадает в сериализуемые представления (журнал/отчёт)
    assert "content" not in result.as_dict()
    client.close()


def test_text_body_is_truncated_by_limit():
    """Длинное текстовое тело показывается с усечением по `PULT_BODY_LIMIT`."""
    payload = "x" * 500

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=payload, headers={"content-type": "text/csv"})

    client, journal = _console(handler)
    result = execute_request(
        client, method="GET", path="/api/data/files", journal=journal, body_limit=BODY_LIMIT
    )

    assert result.body_truncated
    assert len(result.body_text) == BODY_LIMIT
    assert "показано 10" in result.body_preview(limit=10)
    client.close()


def test_empty_response_has_no_body():
    """`204 No Content` — успешный результат без тела (удаление сущности)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client, journal = _console(handler)
    result = execute_request(client, method="DELETE", path="/api/11111111", journal=journal)

    assert result.ok
    assert result.status == 204
    assert result.body_text == ""
    assert result.size_bytes == 0
    client.close()


def test_charset_from_content_type_and_text_detection():
    """Разбор `charset` и определение текстовых типов (остальное — файл)."""
    assert charset_from_content_type("text/csv; charset=UTF-8") == "utf-8"
    assert charset_from_content_type('application/json; charset="cp1251"') == "cp1251"
    assert charset_from_content_type("text/csv") == ""
    assert charset_from_content_type(None) == ""

    assert is_text_content("application/json")
    assert is_text_content("text/csv")
    assert not is_text_content("application/octet-stream")
    assert not is_text_content("")
    assert not is_text_content(None)


def test_decode_body_prefers_declared_charset_and_falls_back():
    """Кодировка из заголовка имеет приоритет, иначе подбирается рабочая."""
    cyrillic = "3Dпринтер"
    utf8 = cyrillic.encode("utf-8")
    cp1251 = cyrillic.encode("cp1251")

    text, encoding = decode_body(utf8, "text/csv; charset=utf-8")
    assert (text, encoding) == (cyrillic, "utf-8")

    # сервер не указал кодировку и отдал не-UTF-8 байты (дефект latin-1/кодировок)
    text, encoding = decode_body(cp1251, "text/csv")
    assert text == cyrillic
    assert encoding == "cp1251"

    # любые байты читаются без исключения (последний резерв — latin-1)
    text, encoding = decode_body(b"\x98\xfe", "text/plain")
    assert text
    assert encoding == "latin-1"


def test_binary_body_is_not_kept_in_result_by_default():
    """Содержимое файла сохраняется только по явному запросу оператора."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data", headers={"content-type": "application/pdf"})

    client, journal = _console(handler)
    result = execute_request(
        client, method="GET", path="/api/data/file/x/download", journal=journal
    )

    assert result.binary
    assert result.content is None
    assert result.file_name is None
    client.close()


def test_multipart_payload_helpers():
    """MultipartPayload: MIME-тип по расширению, части файла и поля формы."""
    multipart = MultipartPayload(
        file_name="__TEST__model.h5",
        content=b"h5",
        file_type="H5",
        description="модель для проверки",
    )

    assert multipart.content_type == "application/octet-stream"
    assert multipart.as_files() == {"file": ("__TEST__model.h5", b"h5", "application/octet-stream")}
    assert multipart.form_data() == {"file_type": "H5", "description": "модель для проверки"}

    bare = MultipartPayload(file_name="x.raw.csv", content=b"a")
    assert bare.content_type == "text/csv"
    assert bare.form_data() == {}


def test_result_helpers_for_history_and_filters():
    """Вспомогательные представления результата (история консоли, фильтр, отчёт)."""
    client, journal = _console(
        lambda request: httpx.Response(200, json={"count": 1}, headers={"server": "nginx"})
    )
    result = execute_request(
        client,
        method="GET",
        path="/api/data/files",
        query={"file_name": "Antminer"},
        label="TC-FILE-01",
        journal=journal,
    )

    assert result.one_line.startswith("GET /api/data/files?file_name=Antminer → 200")
    assert result.matches("antminer")
    assert result.matches("TC-FILE-01")
    assert not result.matches("Mavic")
    assert json.dumps(result.as_dict(), ensure_ascii=False)
    assert result.as_dict()["journal_seq"] == result.journal_seq
    client.close()
