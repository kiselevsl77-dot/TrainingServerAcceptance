"""Тесты помощников подбора данных для ML-проверок (`acceptance/ml_samples`).

Проверяются два наблюдаемых факта живого стенда (`llm_tasks/48`, `llm_tasks/50`):
файл с не-ASCII именем скачать нельзя (дефект P0), а raw_data CSV разделён `;`,
где последние шесть колонок — Base64-сигналы для `inference/single`.
"""

from __future__ import annotations

import pytest

from acceptance import ml_samples
from client.schemas import DatasetResponse, FileMetadataResponse, ModelMetadata

RAW_HEADER = [
    "chunkID",
    "timestamp",
    "U_A_rms",
    "U_B_rms",
    "U_C_rms",
    "I_A_rms",
    "I_B_rms",
    "I_C_rms",
    "sample_rate",
    "signal_length",
    "channel_mask",
    "signal_bytes",
    "byte_order",
    "coding",
    "U_mult",
    "U_dev",
    "I_mult",
    "I_dev",
    "U_A_signal",
    "U_B_signal",
    "U_C_signal",
    "I_A_signal",
    "I_B_signal",
    "I_C_signal",
]

SIGNAL = "A" * 64


def file_row(
    file_id: str,
    name: str,
    *,
    file_type: str = "RAW",
    size: int = 1024,
) -> FileMetadataResponse:
    """Файл реестра `GET /api/data/files`."""
    return FileMetadataResponse.model_validate(
        {
            "id": file_id,
            "file_name": name,
            "size": size,
            "s3_path": f"s3://bucket/{name}",
            "import_date": "2026-09-15T10:00:00",
            "file_type": file_type,
        }
    )


def model_row(
    model_id: str,
    name: str,
    *,
    phase_type: str = "single",
    updated_at: str = "2026-09-14T19:08:10",
    metrics: dict | None = None,
) -> ModelMetadata:
    """Модель реестра `GET /api/ml_models/models`."""
    return ModelMetadata.model_validate(
        {
            "id": model_id,
            "file_id": "5932482e-0000-4000-8000-000000000002",
            "architecture": "stft_mlp",
            "version": "stft_mlp.9bbb964f",
            "name": name,
            "description": None,
            "phase_type": phase_type,
            "output_size": 2,
            "config": {"architecture": "stft_mlp"},
            "metrics": metrics,
            "pruning_mask": None,
            "created_at": "2026-09-14T19:08:10",
            "updated_at": updated_at,
            "signals": [],
            "signal_aliases": {"0": "miner", "1": "not_miner"},
        }
    )


def dataset_row(dataset_id: str, name: str) -> DatasetResponse:
    """Датасет реестра `GET /api/datasets/`."""
    return DatasetResponse.model_validate(
        {
            "id": dataset_id,
            "creation_date": "2026-09-15T10:00:00",
            "name": name,
            "description": None,
            "type": "direct_fill",
        }
    )


def raw_text(rows: int = 2, *, signals: bool = True) -> str:
    """Содержимое RAW-файла: заголовок и строки данных (структура живого стенда)."""
    lines = [ml_samples.RAW_SEPARATOR.join(RAW_HEADER)]
    for index in range(rows):
        values = {
            "chunkID": f"F8D79FE4-8E38-47CA-8F1C-3737941AB0{index:02d}",
            "timestamp": f"17718526412{index:02d}",
            "sample_rate": "    0",
            "signal_length": "2048",
            "channel_mask": "b00111111",
            "signal_bytes": " 3",
            "byte_order": "Little",
            "coding": "Base64",
            "U_mult": " 721",
            "U_dev": "   1",
            "I_mult": "1000",
            "I_dev": "   2",
            "U_A_signal": SIGNAL if signals else "",
            "U_B_signal": SIGNAL,
            "U_C_signal": SIGNAL,
            "I_A_signal": SIGNAL,
            "I_B_signal": SIGNAL,
            "I_C_signal": SIGNAL,
        }
        lines.append(
            ml_samples.RAW_SEPARATOR.join(str(values.get(name, "0.00")) for name in RAW_HEADER)
        )
    return "\n".join(lines)


def test_is_ascii_and_ascii_named_filter():
    files = [
        file_row("11111111-1111-4111-8111-111111111111", "Wi-Fi.raw.csv", size=900),
        file_row("22222222-2222-4222-8222-222222222222", "Принтер_РАБОТА.raw.csv", size=10),
    ]

    assert ml_samples.is_ascii("Wi-Fi.raw.csv") is True
    assert ml_samples.is_ascii("Принтер.raw.csv") is False
    assert [file.file_name for file in ml_samples.ascii_named(files)] == ["Wi-Fi.raw.csv"]


def test_pick_raw_file_prefers_operator_choice():
    """Выбранный оператором файл берётся даже с не-ASCII именем: отказ увидит протокол."""
    files = [
        file_row("11111111-1111-4111-8111-111111111111", "Wi-Fi.raw.csv", size=900),
        file_row("33333333-3333-4333-8333-333333333333", "Принтер.raw.csv", size=10),
    ]

    chosen = ml_samples.pick_raw_file(files, file_id="33333333-3333-4333-8333-333333333333")

    assert chosen is not None
    assert chosen.file_name == "Принтер.raw.csv"


def test_pick_raw_file_skips_non_ascii_and_markup():
    files = [
        file_row("11111111-1111-4111-8111-111111111111", "ТЭЦ.raw.csv", size=1),
        file_row("22222222-2222-4222-8222-222222222222", "ok.markup.csv", file_type="markup"),
        file_row("33333333-3333-4333-8333-333333333333", "big.raw.csv", size=5000),
        file_row("44444444-4444-4444-8444-444444444444", "small.raw.csv", size=900),
    ]

    chosen = ml_samples.pick_raw_file(files)

    assert chosen is not None
    assert chosen.file_name == "small.raw.csv"


def test_pick_raw_file_returns_none_without_ascii_raw():
    files = [file_row("11111111-1111-4111-8111-111111111111", "ТЭЦ.raw.csv", size=1)]

    assert ml_samples.pick_raw_file(files) is None


def test_pick_model_prefers_operator_choice_then_trained():
    models = [
        model_row("11111111-1111-4111-8111-111111111111", "old", updated_at="2026-09-10T10:00:00"),
        model_row(
            "22222222-2222-4222-8222-222222222222",
            "trained",
            updated_at="2026-09-14T10:00:00",
            metrics={"epochs": 3},
        ),
        model_row(
            "33333333-3333-4333-8333-333333333333", "fresh", updated_at="2026-09-20T10:00:00"
        ),
    ]

    chosen = ml_samples.pick_model(models, model_id="11111111-1111-4111-8111-111111111111")
    assert chosen is not None and chosen.name == "old"

    trained = ml_samples.pick_model(models, with_metrics=True)
    assert trained is not None and trained.name == "trained"

    newest = ml_samples.pick_model(models)
    assert newest is not None and newest.name == "fresh"


def test_pick_model_respects_phase_and_returns_none():
    models = [model_row("11111111-1111-4111-8111-111111111111", "three-phase", phase_type="three")]

    assert ml_samples.pick_model(models, phase_type="single") is None
    assert ml_samples.pick_model([]) is None


def test_pick_dataset_prefers_id_and_first_row():
    datasets = [
        dataset_row("5f0a5b7e-0000-4000-8000-000000000001", "skfu-train"),
        dataset_row("5f0a5b7e-0000-4000-8000-000000000002", "leti-train"),
    ]

    chosen = ml_samples.pick_dataset(datasets, dataset_id=str(datasets[1].id))
    assert chosen is not None and chosen.name == "leti-train"

    first = ml_samples.pick_dataset(datasets)
    assert first is not None and first.name == "skfu-train"
    assert ml_samples.pick_dataset([]) is None


def test_labels_for_model_and_dataset():
    model = model_row("11111111-1111-4111-8111-111111111111", "trained")
    dataset = dataset_row("5f0a5b7e-0000-4000-8000-000000000001", "skfu-train")

    assert ml_samples.model_label(model) == "trained · stft_mlp · stft_mlp.9bbb964f"
    assert ml_samples.dataset_label(dataset) == "skfu-train"
    assert ml_samples.dataset_label(dataset, files_count=104) == "skfu-train · файлов 104"


def test_parse_raw_sample_reads_signals_and_params():
    sample = ml_samples.parse_raw_sample(raw_text(rows=2), row=2, file_name="Wi-Fi.raw.csv")

    assert sample.file_name == "Wi-Fi.raw.csv"
    assert sample.row == 2
    assert sample.identity["timestamp"] == "1771852641201"
    assert len(sample.signals) == 6
    assert sample.signals["U_A_signal"] == SIGNAL
    assert sample.params == {
        "U_mult": 721,
        "U_dev": 1,
        "I_mult": 1000,
        "I_dev": 2,
        "byte_order": "Little",
        "signal_bytes": 3,
    }

    fields = sample.request_fields()
    assert fields["U_C_signal"] == SIGNAL
    assert fields["I_mult"] == 1000

    evidence = sample.as_evidence()
    assert evidence["signal_lengths"]["U_A_signal"] == len(SIGNAL)
    assert SIGNAL not in str(evidence)  # Base64 в доказательства не попадает


def test_raw_columns_and_row_helpers():
    """Канонический заголовок RAW и сборка строки данных: сигналы, шкалы, нули."""
    assert ml_samples.RAW_COLUMNS[0] == "chunkID"
    assert ml_samples.RAW_COLUMNS[-6:] == ml_samples.SAMPLE_SIGNAL_FIELDS
    assert len(ml_samples.RAW_COLUMNS) == 24

    row = ml_samples.raw_row({"chunkID": "abc", "U_mult": "721"})
    values = dict(zip(ml_samples.RAW_COLUMNS, row.split(ml_samples.RAW_SEPARATOR), strict=True))

    assert values["chunkID"] == "abc"
    assert values["U_mult"] == "721"
    assert values["I_A_signal"] == "0"
    assert row.count(ml_samples.RAW_SEPARATOR) == len(ml_samples.RAW_COLUMNS) - 1
    assert ml_samples.raw_row(columns=("a", "b")) == "0;0"


def test_parse_raw_sample_reports_problems():
    with pytest.raises(ValueError, match="нет строк данных"):
        ml_samples.parse_raw_sample("только заголовок")

    with pytest.raises(ValueError, match="нет строки данных №3"):
        ml_samples.parse_raw_sample(raw_text(rows=2), row=3)

    with pytest.raises(ValueError, match="не совпадает с заголовком"):
        ml_samples.parse_raw_sample("a;b\n1;2;3")

    with pytest.raises(ValueError, match="нет сигналов"):
        ml_samples.parse_raw_sample(raw_text(signals=False))
