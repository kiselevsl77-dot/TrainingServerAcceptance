"""Тесты снимка стенда (FR-T1) и подписи сборки сервера."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from acceptance.api import Apis, build_client, take_stand_snapshot
from acceptance.config import PultConfig
from acceptance.http_log import Journal
from acceptance.ui.common.labels import build_label
from client.settings import TrainingServerSettings

FILES = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "file_name": "Antminer_S19.raw.csv",
        "size": 1000,
        "s3_path": "RAW/2026/09/09/Antminer_S19.raw.csv",
        "import_date": "2026-09-09T13:53:56",
        "file_type": "RAW",
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "file_name": "Antminer_S19.raw.csv",
        "size": 500,
        "s3_path": "RAW/2026/09/09/Antminer_S19.raw.csv",
        "import_date": "2026-09-09T14:42:07",
        "file_type": "RAW",
    },
    {
        "id": "33333333-3333-4333-8333-333333333333",
        "file_name": "Antminer_S19.markup.csv",
        "size": 100,
        "s3_path": "LOADS/2026/09/09/Antminer_S19.markup.csv",
        "import_date": "2026-09-09T14:42:07",
        "file_type": "LOADS",
    },
]


def _handler(request: httpx.Request) -> httpx.Response:
    """Подменённый сервер: минимальные ответы по всем модулям снимка."""
    path = request.url.path
    if path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if path == "/version":
        return httpx.Response(
            200,
            json={"branch": "dev", "revision": "58ac72f1234567", "build_date": "2026-09-14"},
        )
    if path == "/api/data/files":
        return httpx.Response(200, json={"files": FILES, "count": len(FILES)})
    if path == "/api/loads/list":
        return httpx.Response(
            200,
            json={
                "loads": [{"load_id": "Antminer S19", "category": "майнер"}],
                "result_size": 62,
                "limit": 1,
                "offset": 0,
            },
        )
    if path == "/api/ml_models/models":
        return httpx.Response(200, json={"models": [], "count": 0})
    if path == "/api/datasets/":
        return httpx.Response(200, json={"datasets": [], "count": 0})
    if path == "/api/tasks/":
        return httpx.Response(200, json={"tasks": [], "count": 11})
    return httpx.Response(404, json={"detail": "not found"})


def _apis(settings: TrainingServerSettings, journal: Journal, handler: Callable) -> Apis:
    """Агрегат сервисов поверх подменённого транспорта."""
    client = build_client(
        settings,
        journal,
        config=PultConfig(body_limit=500, log_bodies=True, journal_max=50),
        inner=httpx.MockTransport(handler),
    )
    return Apis.build(client)


def test_snapshot_collects_counts_and_file_stats(settings, journal):
    snapshot = take_stand_snapshot(_apis(settings, journal, _handler))

    assert snapshot["counts"] == {"files": 3, "loads": 62, "models": 0, "datasets": 0, "tasks": 11}
    assert snapshot["files"]["total_bytes"] == 1600
    assert snapshot["files"]["unique_names"] == 2
    assert snapshot["files"]["duplicate_names"] == 1
    assert snapshot["files"]["by_type"] == {"RAW": 2, "LOADS": 1}
    assert snapshot["errors"] == {}
    assert snapshot["version"]["branch"] == "dev"


def test_snapshot_is_recorded_in_journal(settings, journal):
    take_stand_snapshot(_apis(settings, journal, _handler))

    summary = journal.summary()
    assert summary["total"] == 6
    assert 0 < summary["total"] - summary["errors"]
    assert all(record.label is None for record in journal.records)


def test_snapshot_records_errors_without_failing(settings, journal):
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    snapshot = take_stand_snapshot(_apis(settings, journal, failing))

    assert set(snapshot["errors"]) == {"version", "files", "loads", "models", "datasets", "tasks"}
    assert snapshot["counts"] == {}
    assert journal.summary()["errors"] == 6


def test_build_label_formats_revision_and_branch():
    assert build_label({"branch": "dev", "revision": "58ac72f1234567890"}) == "dev@58ac72f12345"
    assert build_label({"revision": "abcdef1234567890"}) == "abcdef123456"
    assert build_label(None) == "не определена"
    assert build_label({}) == "не определена"
