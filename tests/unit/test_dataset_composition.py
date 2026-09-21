"""Тесты учёта состава датасетов (модуль «Datasets», этап T5).

Контракт 17.09.2026 состав датасета **не отдаёт** (`GET /api/datasets/{id}` — только
метаданные), поэтому проверяются все источники данных провайдера:

    * `local` — факт наполнения пультом (состав записан до вызова `fill`, поэтому
      верен даже при отказе задачи);
    * `server` — серверный состав (включается сам, когда операция появляется в
      реестре `acceptance.endpoints`, — задел на устранение замечания P1);
    * `heuristic` — предположение по реестру файлов (всегда помечается гипотезой);
    * `unknown` — данных нет: «состав недоступен (P1)».

Отдельно проверяются сверка серверного и локального состава (`compare`), косвенные
признаки дублей (`overlap_report`) и персистентность учёта (sidecar-файл, схема
сессии v5).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from acceptance import dataset_composition as dc
from acceptance.session import TestSession as SessionModel
from acceptance.session import new_session
from client.schemas import FileMetadataResponse

BASE_URL = "http://test.local"
AT = datetime(2026, 9, 17, 12, 0, 0)
RAW_1 = "11111111-1111-1111-1111-111111111111"
MARKUP_1 = "22222222-2222-2222-2222-222222222222"
RAW_2 = "33333333-3333-3333-3333-333333333333"
LETI_RAW = "44444444-4444-4444-4444-444444444444"
LETI_TWIN = "55555555-5555-5555-5555-555555555555"
MODEL = "66666666-6666-6666-6666-666666666666"


def file_record(
    file_id: str,
    name: str,
    *,
    size: int = 2048,
    file_type: str = "RAW",
    path: str = "",
) -> FileMetadataResponse:
    """Файл реестра `GET /api/data/files`."""
    return FileMetadataResponse(
        id=UUID(file_id),
        file_name=name,
        size=size,
        s3_path=path or f"s3://bucket/{name}",
        import_date=AT,
        file_type=file_type,
    )


def registry() -> list[FileMetadataResponse]:
    """Реестр файлов: две организации, повторная загрузка одного имени и прочий файл."""
    return [
        file_record(RAW_1, "skfu-record-1.raw.csv", size=4096),
        file_record(MARKUP_1, "skfu-record-1.markup.csv", size=512),
        file_record(RAW_2, "skfu-record-2.raw.csv", size=8192),
        file_record(LETI_RAW, "leti-record-1.raw.csv", size=4096),
        file_record(LETI_TWIN, "leti-record-1.raw.csv", size=4096),
        file_record(MODEL, "skfu-model-1.h5", file_type="H5"),
    ]


def session() -> SessionModel:
    """Сессия испытаний для учёта состава датасетов."""
    return new_session(base_url=BASE_URL)


class FakeDatasets:
    """Псевдо-клиент датасетов с методом состава (после устранения замечания P1)."""

    def __init__(self, payload: Any = None) -> None:
        self.payload = payload
        self.calls: list[Any] = []

    def get_dataset_summary(self, dataset_id: Any) -> Any:
        """Серверный состав датасета (форма ответа — из перспективных требований)."""
        self.calls.append(dataset_id)
        return self.payload


class FakeApis:
    """Агрегат сервисов с одним клиентом датасетов."""

    def __init__(self, payload: Any = None) -> None:
        self.datasets = FakeDatasets(payload)


# ---------------------------------------------------------------------------
# Локальный факт наполнения пультом (состав известен точно)
# ---------------------------------------------------------------------------
def test_record_fill_keeps_fact_and_sidecar(tmp_path: Path) -> None:
    """Факт наполнения: состав, происхождение, сессия и sidecar-файл."""
    current = session()

    composition = dc.record_fill(
        current,
        dataset_id="aaaa-1",
        raw_file_ids=[RAW_1, RAW_2],
        markup_file_ids=[MARKUP_1],
        name="__TEST__dataset",
        check_id="TC-DS-05",
        files=registry(),
        store_directory=tmp_path,
    )

    assert composition.source == dc.SOURCE_LOCAL
    assert composition.is_trusted and not composition.is_hypothesis
    assert composition.raw_ids == (RAW_1, RAW_2)
    assert composition.markup_ids == (MARKUP_1,)
    assert composition.total_bytes == 4096 + 8192 + 512
    assert composition.checks == ("TC-DS-05",)
    assert dc.composition_of(current, "aaaa-1", source=dc.SOURCE_LOCAL) == composition

    store = dc.load_store(dc.store_path(tmp_path))
    assert [item.dataset_id for item in store] == ["aaaa-1"]
    assert dc.store_path(tmp_path).exists()
    assert any(event["event"] == "dataset_composition" for event in current.history)


def test_record_composition_replaces_same_source_only(tmp_path: Path) -> None:
    """Повторная запись того же источника заменяет её, другой источник остаётся."""
    current = session()
    dc.record_fill(
        current,
        dataset_id="bbbb-1",
        raw_file_ids=[RAW_1],
        files=registry(),
        store_directory=tmp_path,
    )
    second = dc.record_fill(
        current,
        dataset_id="bbbb-1",
        raw_file_ids=[RAW_1, RAW_2],
        files=registry(),
        store_directory=tmp_path,
    )
    server = dc.DatasetComposition(
        dataset_id="bbbb-1", source=dc.SOURCE_SERVER, files=second.files, counts={"records": 2}
    )
    dc.record_composition(current, server, store_directory=tmp_path)

    assert dc.composition_of(current, "bbbb-1", source=dc.SOURCE_LOCAL) == second
    assert dc.composition_of(current, "bbbb-1", source=dc.SOURCE_SERVER) is not None
    assert len(dc.composition_store(current)) == 2


def test_store_round_trip_and_tolerance(tmp_path: Path) -> None:
    """Sidecar-файл читается обратно; отсутствие и порча файла — пустой список."""
    path = dc.store_path(tmp_path)
    assert dc.load_store(path) == []

    record = dc.DatasetComposition(
        dataset_id="cccc-1",
        source=dc.SOURCE_HEURISTIC,
        name="skfu-train",
        files=(dc.CompositionFile(file_id=RAW_1, role=dc.ROLE_RAW),),
        confidence="гипотеза",
    )
    dc.save_store([record], path)
    restored = dc.load_store(path)

    assert restored == [record]
    path.write_text("{не json", encoding="utf-8")
    assert dc.load_store(path) == []


# ---------------------------------------------------------------------------
# Провайдер: сервер → локальный факт → гипотеза
# ---------------------------------------------------------------------------
def test_read_composition_prefers_local_fact(tmp_path: Path) -> None:
    """Локальный факт возвращается, когда сервер состава не отдаёт (P1)."""
    current = session()
    dc.record_fill(
        current,
        dataset_id="dddd-1",
        raw_file_ids=[RAW_1],
        markup_file_ids=[MARKUP_1],
        name="skfu-train",
        files=registry(),
        store_directory=tmp_path,
    )

    composition = dc.read_composition(current, None, "dddd-1", name="skfu-train", files=registry())

    assert composition.source == dc.SOURCE_LOCAL
    assert composition.files_count == 2


def test_read_composition_uses_server_when_available(tmp_path: Path) -> None:
    """Серверный состав приоритетнее локального факта и обогащается из реестра."""
    current = session()
    dc.record_fill(
        current,
        dataset_id="eeee-1",
        raw_file_ids=[RAW_1],
        name="skfu-train",
        files=registry(),
        store_directory=tmp_path,
    )
    apis = FakeApis(
        {"raw_file_ids": [RAW_2], "markup_file_ids": [MARKUP_1], "records": 3, "chunks": 120}
    )

    composition = dc.read_composition(current, apis, "eeee-1", name="skfu-train", files=registry())

    assert apis.datasets.calls == ["eeee-1"]
    assert composition.source == dc.SOURCE_SERVER
    assert composition.raw_ids == (RAW_2,)
    assert composition.markup_ids == (MARKUP_1,)
    assert composition.counts == {"records": 3, "chunks": 120}
    assert composition.counts_label == "records=3, chunks=120"
    assert composition.files[0].file_name == "skfu-record-2.raw.csv"
    assert composition.confidence == "эталон"


def test_composition_from_server_parses_file_entries_and_nesting() -> None:
    """Серверный состав разбирается и в форме `files[]`, и внутри `dataset`."""
    payload = {
        "dataset": {
            "files": [
                {"id": RAW_1, "role": "raw"},
                {"file_id": MARKUP_1, "file_name": "skfu-record-1.markup.csv", "size": 512},
            ]
        }
    }

    composition = dc.composition_from_server("ffff-1", payload, files=registry())

    assert composition is not None
    assert composition.raw_ids == (RAW_1,)
    assert composition.markup_ids == (MARKUP_1,)
    assert composition.files[1].size == 512
    assert dc.composition_from_server("ffff-1", {"records": 4}) is None
    assert dc.composition_from_server("ffff-1", "не словарь") is None


def test_read_composition_heuristic_is_marked_as_hypothesis() -> None:
    """Гипотеза строится по имени датасета и помечается как предположение."""
    current = session()

    strict = dc.read_composition(current, None, "gggg-1", name="leti-record-1", files=registry())
    fallback = dc.read_composition(current, None, "gggg-2", name="skfu-train", files=registry())

    assert strict.source == dc.SOURCE_HEURISTIC and strict.is_hypothesis
    assert strict.raw_ids == (LETI_RAW, LETI_TWIN)
    assert "leti+record" in strict.confidence
    assert fallback.files_count == 4  # skfu-record-1 (RAW и markup), skfu-record-2, skfu-model-1
    assert "skfu" in fallback.confidence


def test_read_composition_modes_and_unknown() -> None:
    """Режимы чтения: принудительный сервер/факт и честное «состав недоступен»."""
    current = session()

    forced_server = dc.read_composition(current, None, "hhhh-1", mode=dc.MODE_SERVER)
    forced_local = dc.read_composition(current, None, "hhhh-1", mode=dc.MODE_LOCAL)
    no_guess = dc.read_composition(
        current, None, "hhhh-1", name="skfu-train", files=registry(), allow_heuristic=False
    )
    empty = dc.read_composition(current, None, "")

    assert forced_server.source == dc.SOURCE_UNKNOWN
    assert "принудительно" in forced_server.note
    assert forced_local.source == dc.SOURCE_UNKNOWN
    assert "вне пульта" in forced_local.note
    assert no_guess.source == dc.SOURCE_UNKNOWN
    assert empty.source == dc.SOURCE_UNKNOWN and empty.note == "датасет не выбран"
    assert dc.unknown_composition("hhhh-1").source_label == dc.SOURCE_LABELS[dc.SOURCE_UNKNOWN]


def test_server_capability_follows_endpoint_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Признак серверного состава включается сам при появлении операции в реестре."""
    assert dc.server_available() is False
    assert dc.server_summary_key() == ""

    monkeypatch.setattr(dc, "SERVER_SUMMARY_KEYS", ("get /api/datasets/{dataset_id}",))

    assert dc.server_available() is True
    assert dc.server_summary_key() == "get /api/datasets/{dataset_id}"


def test_role_for_recognises_markup_and_other_files() -> None:
    """Роль файла определяется по имени, а не по типу файла в реестре."""
    files = {str(item.file_name): item for item in registry()}

    assert dc.role_for(files["skfu-record-1.raw.csv"]) == dc.ROLE_RAW
    assert dc.role_for(files["skfu-record-1.markup.csv"]) == dc.ROLE_MARKUP
    assert dc.role_for(files["skfu-model-1.h5"]) == dc.ROLE_RAW


# ---------------------------------------------------------------------------
# Сверка составов и косвенные признаки дублей
# ---------------------------------------------------------------------------
def test_compare_reports_mismatch_and_match(tmp_path: Path) -> None:
    """Сверка серверного состава с локальным фактом: расхождения видны по группам."""
    current = session()
    local = dc.record_fill(
        current,
        dataset_id="iiii-1",
        raw_file_ids=[RAW_1],
        markup_file_ids=[MARKUP_1],
        files=registry(),
        store_directory=tmp_path,
    )
    other = dc.DatasetComposition(
        dataset_id="iiii-1",
        source=dc.SOURCE_SERVER,
        files=(dc.CompositionFile(file_id=RAW_2, role=dc.ROLE_RAW),),
    )

    mismatch = dc.compare(other, local)

    assert mismatch["match"] is False
    assert mismatch["raw"]["only_server"] == [RAW_2]
    assert mismatch["raw"]["only_local"] == [RAW_1]
    assert mismatch["markup"]["only_local"] == [MARKUP_1]
    assert "не совпал" in mismatch["verdict"]

    same = dc.compare(local, local)
    assert same["match"] is True and same["raw"]["only_server"] == []


def test_overlap_report_counts_crossings_and_repeats() -> None:
    """Отчёт о дублях: пересечение датасетов и повторная загрузка «имя + размер»."""
    first = dc.DatasetComposition(
        dataset_id="d1",
        name="skfu-train",
        source=dc.SOURCE_HEURISTIC,
        files=(
            dc.CompositionFile(file_id=RAW_1, role=dc.ROLE_RAW),
            dc.CompositionFile(file_id=LETI_RAW, role=dc.ROLE_RAW),
        ),
    )
    second = dc.DatasetComposition(
        dataset_id="d2",
        name="leti-train",
        source=dc.SOURCE_LOCAL,
        files=(dc.CompositionFile(file_id=LETI_RAW, role=dc.ROLE_RAW),),
    )

    report = dc.overlap_report([first, second], registry())

    assert report["totals"]["datasets"] == 2
    assert report["totals"]["cross_dataset"] == 1
    assert report["cross_dataset"][0]["datasets"] == ["leti-train", "skfu-train"]
    assert report["totals"]["repeat_uploads"] == 1
    assert report["repeat_uploads"][0]["file_name"] == "leti-record-1.raw.csv"
    assert report["repeat_uploads"][0]["file_ids"] == [LETI_RAW, LETI_TWIN]
    assert report["by_source"] == {dc.SOURCE_HEURISTIC: 1, dc.SOURCE_LOCAL: 1}
    assert any("вероятностный" in item for item in report["hypotheses"])


def test_overlap_report_without_data_says_no_signs() -> None:
    """Пустой учёт даёт честный ответ: признаков не найдено, вывод вероятностный."""
    report = dc.overlap_report([dc.unknown_composition("kkkk-1")])

    assert report["totals"]["cross_dataset"] == 0
    assert report["totals"]["repeat_uploads"] == 0
    assert "признаков" in report["hypotheses"][0]


def test_composition_rows_and_session_schema_v5(tmp_path: Path) -> None:
    """Строки состава для отчёта и совместимость схемы сессии (v5 и старые файлы)."""
    current = session()
    composition = dc.record_fill(
        current,
        dataset_id="jjjj-1",
        raw_file_ids=[RAW_1],
        markup_file_ids=[MARKUP_1],
        name="skfu-train",
        task_id="task-9",
        files=registry(),
        store_directory=tmp_path,
    )

    rows = dc.composition_rows([composition])

    assert rows[0]["name"] == "skfu-train"
    assert rows[0]["source"] == dc.SOURCE_LABELS[dc.SOURCE_LOCAL]
    assert rows[0]["files"] == 2 and rows[0]["raw"] == 1 and rows[0]["markup"] == 1
    assert rows[0]["task_id"] == "task-9"

    payload = current.to_dict()
    assert "dataset_composition" in payload
    assert SessionModel.from_dict(payload).dataset_composition == current.dataset_composition

    legacy = {key: value for key, value in payload.items() if key != "dataset_composition"}
    assert SessionModel.from_dict(legacy).dataset_composition == []
