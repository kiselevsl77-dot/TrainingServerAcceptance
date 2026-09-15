"""Интеграционные проверки живого стенда (только чтение, этапы T0–T1).

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
from acceptance.records import build_records
from client.settings import load_settings
from lib.markup_stats import parse_markup_stats

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


# ---------------------------------------------------------------------------
# Этап T1: объединение живого реестра в записи (только чтение)
# ---------------------------------------------------------------------------
def test_live_registry_is_merged_into_records():
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        files = Apis.build(client).files.list_files().files
    finally:
        client.close()

    overview = build_records(files)

    assert overview.stats.records > 0, "в реестре нет RAW-файлов"
    assert overview.manifest_is_complete(), "манифест потерял файлы реестра"
    assert overview.stats.confirmed > 0, "нет ни одной пары, подтверждённой по времени"
    assert overview.stats.duplicate_bases >= 1, "ожидались дубли имён (повторные загрузки)"


def test_live_markup_download_gives_records_and_chunks_or_api_defect():
    """Разбор разметки доступных markup-файлов; дефект сервера фиксируется, а не падает."""
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        files = Apis.build(client).files.list_files().files
        overview = build_records(files)
        ascii_record = next(
            (
                record
                for record in overview.records
                if record.markup is not None
                and str(record.markup.file_name).isascii()
                and record.markup.size < 5 * 1024 * 1024
            ),
            None,
        )
        if ascii_record is None:
            pytest.skip("нет доступного markup-файла с ASCII-именем")

        payload = Apis.build(client).files.download(ascii_record.markup.id)
    finally:
        client.close()

    stats = parse_markup_stats(payload.content)

    assert stats.records > 0, "markup-файл не содержит строк данных"
    assert stats.chunks > 0, "в markup-файле нет колонки chunkID"
    assert any(record.path.endswith("/download") for record in journal.records)
