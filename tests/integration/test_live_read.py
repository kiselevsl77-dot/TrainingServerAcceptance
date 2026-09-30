"""Интеграционные проверки живого стенда (только чтение, этапы T0–T2).

Запуск:

    $env:TRAINING_SERVER_BASE_URL = "https://energomera.ai-center.online"
    python -m pytest -m integration

Тесты **не выполняют** ресурсоёмких и изменяющих операций: только `/health`,
`/version`, чтение реестров (снимок стенда), объединение записей и читающие
вызовы консоли запросов. Проверяется, что все запросы попадают в журнал пульта.
"""

from __future__ import annotations

import pytest

from acceptance import ml_samples
from acceptance.api import Apis, build_client, take_stand_snapshot
from acceptance.exchange import ERROR_NOT_FOUND, build_console_client, execute_request
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


# ---------------------------------------------------------------------------
# Этап T2: консоль запросов на живом стенде (только чтение)
# ---------------------------------------------------------------------------
def test_live_console_reading_request_returns_status_and_journal_ref():
    """Читающий вызов консоли: статус, заголовки, тело и привязка к записи журнала."""
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_console_client(settings, journal)
    try:
        result = execute_request(
            client, method="GET", path="/health", label="TC-CONSOLE", journal=journal
        )
    finally:
        client.close()

    assert result.ok, f"живой стенд ответил {result.status_label}: {result.error}"
    assert result.status == 200
    assert result.journal_seq == journal.records[-1].seq
    assert isinstance(result.body_json, dict)
    assert str(result.body_json.get("status", "")).lower() == "ok"
    assert result.duration_ms >= 0
    assert result.headers.get("content-type", "").startswith("application/json")


def test_live_console_query_parameters_reach_the_server():
    """Параметры консоли доходят до сервера (живой реестр файлов по имени)."""
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_console_client(settings, journal)
    try:
        result = execute_request(
            client,
            method="GET",
            path="/api/data/files",
            query={"file_name": "Antminer"},
            label="TC-FILE-01",
            journal=journal,
        )
    finally:
        client.close()

    assert result.ok, f"живой стенд ответил {result.status_label}: {result.error}"
    assert isinstance(result.body_json, dict)
    files = result.body_json.get("files") or []
    assert files, "фильтр по имени не вернул файлов"
    assert all("Antminer" in str(item.get("file_name", "")) for item in files)
    assert journal.records[-1].label == "TC-FILE-01"


def test_live_console_404_is_a_result_not_an_exception():
    """Известный дефект стенда (404 на скачивание) не роняет консоль, а фиксируется."""
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_console_client(settings, journal)
    try:
        result = execute_request(
            client,
            method="GET",
            path="/api/data/file/00000000-0000-4000-8000-000000000000/download",
            label="TC-CONSOLE",
            binary=True,
            journal=journal,
            keep_content=True,
        )
    finally:
        client.close()

    assert result.is_error
    assert result.error_kind == ERROR_NOT_FOUND
    assert result.status == 404
    assert result.error
    assert result.journal_seq is not None


# ---------------------------------------------------------------------------
# Этапы T6–T8: каталог моделей, ONNX и сэмпл RAW для инференса (только чтение)
# ---------------------------------------------------------------------------
def test_live_ml_architectures_and_model_card_are_readable():
    """Каталог архитектур и карточка модели живого стенда заполнены (TC-MOD-01/06)."""
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        apis = Apis.build(client)
        architectures = apis.models.list_architectures()
        models = apis.models.list_models()
        picked = ml_samples.pick_model(models, with_metrics=True)
        card = apis.models.get_model(picked.id) if picked is not None else None
    finally:
        client.close()

    assert architectures, "каталог архитектур пуст"
    assert all(item.default_config for item in architectures)
    assert models, "реестр моделей пуст — ML-проверки не с чем выполнять"
    if card is None:
        pytest.skip("на стенде нет обученной модели (metrics пусты)")
    assert str(card.id) == str(picked.id)
    assert card.file_id, "у модели нет файла `.h5` — проверки загрузки/удаления не выполнить"
    assert card.version and card.updated_at
    assert [record.path for record in journal.records if record.path.endswith("/models")]


def test_live_onnx_export_returns_file():
    """Экспорт ONNX отдаёт непустой файл с именем от сервера (TC-TR-06)."""
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        apis = Apis.build(client)
        picked = ml_samples.pick_model(apis.models.list_models(), with_metrics=True)
        if picked is None:
            pytest.skip("на стенде нет обученной модели для экспорта ONNX")
        payload = apis.models.download_onnx(picked.id)
    finally:
        client.close()

    assert payload.size > 0, "сервер вернул пустой ONNX"
    assert payload.file_name or payload.media_type
    assert any(record.path.endswith("/download_onnx") for record in journal.records)


def test_live_raw_sample_is_ready_for_inference_single():
    """Строка RAW-файла разбирается в сэмпл `inference/single` (TC-INF-02, только чтение)."""
    settings = _settings_or_skip()
    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        apis = Apis.build(client)
        files = apis.files.list_files().files
        picked = ml_samples.pick_raw_file(files)
        if picked is None:
            pytest.skip("в реестре нет RAW-файла с ASCII-именем: скачать нечего")
        payload = apis.files.download(picked.id)
    finally:
        client.close()

    sample = ml_samples.parse_raw_sample(
        payload.content.decode("utf-8", errors="replace"),
        file_name=picked.file_name,
    )

    assert len(sample.signals) == 6
    assert all(len(value) > 0 for value in sample.signals.values())
    assert sample.params.get("signal_bytes") in (1, 2, 3, 4)
    assert sample.identity["chunkID"], "в строке RAW нет `chunkID`"
