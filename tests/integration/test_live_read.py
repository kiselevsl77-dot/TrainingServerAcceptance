"""Интеграционные проверки живого стенда (только чтение, этап T0).

Запуск:

    $env:TRAINING_SERVER_BASE_URL = "https://energomera.ai-center.online"
    python -m pytest -m integration

Тесты **не выполняют** ресурсоёмких и изменяющих операций: только `/health`,
`/version` и чтение реестров (снимок стенда). Проверяется, что все запросы
попадают в журнал пульта.
"""

from __future__ import annotations

import pytest

from acceptance.api import Apis, build_client, take_stand_snapshot
from acceptance.http_log import Journal
from client.settings import load_settings

pytestmark = pytest.mark.integration


def _settings_or_skip():
    """Настройки подключения или пропуск теста (если base URL не задан)."""
    settings = load_settings()
    if not settings.is_configured:
        pytest.skip("TRAINING_SERVER_BASE_URL не задан")
    return settings


def test_live_health_version_and_journal():
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        apis = Apis.build(client)
        assert apis.system.health() is True
        version = apis.system.version()
    finally:
        client.close()

    assert isinstance(version, dict) and version
    summary = journal.summary()
    assert summary["total"] >= 2
    assert summary["errors"] == 0


def test_live_stand_snapshot_matches_registry():
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        snapshot = take_stand_snapshot(Apis.build(client))
    finally:
        client.close()

    assert snapshot["counts"].get("files", 0) > 0
    assert snapshot["files"]["unique_names"] >= 1
    assert "files" not in snapshot["errors"]
    assert journal.summary()["total"] >= 5


def test_live_files_registry_is_readable():
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        files = Apis.build(client).files.list_files().files
    finally:
        client.close()

    assert files, "реестр файлов пуст — проверьте доступность стенда"
    assert all(file.s3_path for file in files)
    assert journal.records[0].path == "/api/data/files"
