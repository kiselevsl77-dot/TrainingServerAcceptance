"""Тесты логирующего транспорта и журнала обмена (FR-T6).

Проверяется главная гарантия прозрачности испытаний: **любой** запрос пульта
(успешный, ошибочный по статусу, неудавшийся по соединению) фиксируется и в
памяти (`Journal`), и в структурном JSONL — с меткой проверки и метриками.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from acceptance.http_log import Journal, LoggingTransport, label_context
from acceptance.logging_setup import setup_logging
from client.errors import NotFoundError, ServerUnavailableError
from client.http import ApiHttpClient

BODY_LIMIT = 20


def _handler(request: httpx.Request) -> httpx.Response:
    """Подменённый сервер: успех, 404, ошибка соединения и бинарная отдача."""
    path = request.url.path
    if path == "/api/data/files":
        return httpx.Response(200, json={"files": [], "count": 0, "note": "x" * 50})
    if path == "/api/boom":
        raise httpx.ConnectError("нет соединения")
    if path.endswith("/download"):
        return httpx.Response(
            200,
            content=b"\x00\x01" * 10,
            headers={"content-type": "application/octet-stream"},
        )
    return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def wired(tmp_path: Path):
    """Клиент с логирующим транспортом, журнал и путь JSONL-журнала."""
    setup_logging(
        level="DEBUG",
        session_id="http",
        app_log=tmp_path / "app.log",
        session_log=tmp_path / "session.jsonl",
        console=False,
        enqueue=False,
    )
    journal = Journal(max_records=50)
    transport = LoggingTransport(
        journal,
        inner=httpx.MockTransport(_handler),
        body_limit=BODY_LIMIT,
        log_bodies=True,
    )
    client = ApiHttpClient(base_url="http://test.local", timeout=5.0, transport=transport)
    return journal, client, tmp_path / "session.jsonl"


def _events(path: Path) -> list[str]:
    """Типы событий из структурного журнала."""
    return [
        json.loads(line)["record"]["extra"]["event"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_successful_exchange_is_journaled(wired):
    journal, client, jsonl = wired

    client.get("/api/data/files", params={"file_name": "a"})

    record = journal.records[0]
    assert record.status == 200
    assert record.path == "/api/data/files"
    assert record.query == "file_name=a"
    assert record.response_bytes is not None and record.response_bytes > 0
    assert record.content_type == "application/json"
    assert record.duration_ms >= 0
    assert not record.is_error
    assert {"http_request", "http_response"}.issubset(set(_events(jsonl)))


def test_label_context_marks_exchange_with_check_id(wired):
    journal, client, jsonl = wired

    with label_context("TC-FILE-01"):
        client.get("/api/data/files")

    assert journal.records[0].label == "TC-FILE-01"
    assert len(journal.for_label("TC-FILE-01")) == 1

    payload = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[-1])["record"]["extra"]
    assert payload["payload"]["label"] == "TC-FILE-01"


def test_headers_are_journaled_for_both_directions(wired):
    """Заголовки запроса и ответа попадают в запись журнала и в его выгрузку.

    Без них «полная» подробность монитора обмена невозможна: комиссия должна видеть
    заголовки так, как их отдал сервер (важно для дефектов вида «нет Content-Length»).
    """
    journal, client, _jsonl = wired

    client.get("/api/data/files", headers={"X-Pult-Probe": "1"})

    record = journal.records[0]
    assert record.request_header_map.get("x-pult-probe") == "1"
    assert "accept" in record.request_header_map
    assert record.response_header_map.get("content-type") == "application/json"
    payload = record.as_dict()
    assert payload["request_headers"]["x-pult-probe"] == "1"
    assert "content-type" in payload["response_headers"]


def test_connection_error_is_journaled_and_logged(wired):
    journal, client, jsonl = wired

    with pytest.raises(ServerUnavailableError):
        client.get("/api/boom")

    record = journal.records[-1]
    assert record.status is None
    assert record.error is not None and "ConnectError" in record.error
    assert record.is_error
    assert "http_error" in _events(jsonl)


def test_error_status_is_marked_as_error(wired):
    journal, client, _ = wired

    with pytest.raises(NotFoundError):
        client.get("/api/missing")

    assert journal.errors[-1].status == 404
    assert journal.summary()["by_status"]["4xx"] == 1


def test_binary_body_is_not_stored_but_size_is(wired):
    journal, client, _ = wired

    response = client.get_binary("/api/data/file/1/download")

    record = journal.records[-1]
    assert response.content == b"\x00\x01" * 10
    assert record.response_body is None
    assert record.response_bytes == 20
    assert record.content_type == "application/octet-stream"


def test_long_body_is_truncated_by_limit(wired):
    journal, client, _ = wired

    client.get("/api/data/files")

    record = journal.records[-1]
    assert record.response_body is not None
    assert len(record.response_body) == BODY_LIMIT
    assert record.body_truncated


def test_journal_summary_and_clear(wired):
    journal, client, _ = wired
    client.get("/api/data/files")

    summary = journal.summary()
    assert summary["total"] == 1
    assert summary["by_method"] == {"GET": 1}
    assert summary["response_bytes"] > 0

    journal.clear()
    assert journal.records == ()
    assert journal.summary()["total"] == 0
    assert summary["total"] == 1
    assert summary["by_method"] == {"GET": 1}
    assert summary["response_bytes"] > 0

    journal.clear()
    assert journal.records == ()
    assert journal.summary()["total"] == 0
