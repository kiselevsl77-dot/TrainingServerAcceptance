"""Стоп-тест актуальности контракта API (`docs/SOM1.json`).

Контракт — источник реестра эндпоинтов консоли, поэтому его версия фиксируется **явно**:
размер, контрольная сумма и состав записаны здесь, разбор версий — в `docs/README.md`,
план перехода — в `docs/09. Контракт API 17.09.2026 — изменения и план работ.md`.
Так откат контракта (например, копированием старой версии из постановки командой
`tools/sync_docs`) виден сразу, а не после расхождения реестра, чек-листа и отчёта.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
CONTRACT_PATH = DOCS_DIR / "SOM1.json"
ARCHIVE_PATH = DOCS_DIR / "SOM1.2026-08-31.json"

#: Актуальная версия контракта (получена 17.09.2026 отдельным файлом `docs/api_17_09_26.json`).
CONTRACT_SHA256 = "50f8ed29f9fcf425ab0c0075751a64a474ffc5758f63d06a83f51f7a30fc6b11"
CONTRACT_SIZE = 107765

#: Состав актуального контракта (было 27 путей / 32 операции / 34 схемы / 6 тегов).
PATHS_TOTAL, OPERATIONS_TOTAL, SCHEMAS_TOTAL, TAGS_TOTAL = 34, 40, 49, 10

#: Архивная копия предыдущей версии: по ней построены этапы T0–T3.
ARCHIVE_SHA256 = "05dd6e2fcb5178b539bdecaef3c4a44d6d31089acc1a3672188f7e703fbdcd06"
ARCHIVE_OPERATIONS = 32

HTTP_METHODS = ("get", "post", "put", "delete", "patch")

#: Пути блока AutoML (8 операций, тег `ML models: AutoML`).
AUTOML_PATHS = (
    "/api/ml_models/models/{model_id}/find_params/population",
    "/api/ml_models/find_params/population/{population_id}/selection",
    "/api/ml_models/find_params/population/{population_id}",
    "/api/ml_models/find_params/population",
    "/api/ml_models/find_params/population/{population_id}/mutated_models",
    "/api/ml_models/find_params/population/{population_id}/selections",
    "/api/ml_models/find_params/population/{population_id}/grid.csv",
)

GRID_PATH = "/api/ml_models/find_params/population/{population_id}/grid.csv"

NEW_SCHEMAS = (
    "PopulationConfig",
    "MutationRangeRule",
    "MutationValuesRule",
    "SelectionConfig",
    "FindParamsPopulation",
    "FindParamsMutatedModel",
)


def load(path: Path) -> dict:
    """Читает спецификацию."""
    return json.loads(path.read_text(encoding="utf-8"))


def operations(spec: dict) -> list[tuple[str, str]]:
    """Все операции спецификации: (метод, путь)."""
    return [
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method in item
        if method in HTTP_METHODS
    ]


@pytest.fixture(scope="module")
def contract() -> dict:
    """Актуальный контракт пульта."""
    assert CONTRACT_PATH.is_file(), f"нет контракта: {CONTRACT_PATH.as_posix()}"
    return load(CONTRACT_PATH)


def test_contract_is_actual_version():
    """`docs/SOM1.json` — зафиксированная версия от 17.09.2026 (размер и SHA-256)."""
    data = CONTRACT_PATH.read_bytes()

    assert len(data) == CONTRACT_SIZE
    assert hashlib.sha256(data).hexdigest() == CONTRACT_SHA256
    assert load(CONTRACT_PATH)["info"] == {"title": "Energomera", "version": "0.1.0"}


def test_contract_composition_matches_recorded(contract: dict):
    """Состав контракта: 34 пути, 40 операций, 49 схем, 10 тегов."""
    schemas = (contract.get("components") or {}).get("schemas") or {}
    tags = {
        tag
        for item in contract["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict)
        for tag in (operation.get("tags") or [])
    }

    assert len(contract["paths"]) == PATHS_TOTAL
    assert len(operations(contract)) == OPERATIONS_TOTAL
    assert len(schemas) == SCHEMAS_TOTAL
    assert len(tags) == TAGS_TOTAL


def test_automl_block_is_declared(contract: dict):
    """В контракте есть весь блок AutoML, CSV-выгрузка сетки и её схемы."""
    for path in AUTOML_PATHS:
        assert path in contract["paths"], path

    grid = contract["paths"][GRID_PATH]["get"]
    assert "text/csv" in grid["responses"]["200"]["content"]
    for name in NEW_SCHEMAS:
        assert name in contract["components"]["schemas"], name


def test_loads_contract_dropped_phase(contract: dict):
    """Контракт снял требование фазы: `phase_connection` убран, `ph_n` отсутствует."""
    schemas = contract["components"]["schemas"]
    params = contract["paths"]["/api/loads/list"]["get"]["parameters"]

    assert "phase_connection" not in schemas["LoadDevice"]["properties"]
    assert "phase_connection" not in schemas["UpdateLoadRequest"]["properties"]
    assert schemas["LoadDevice"]["required"] == ["load_id", "category"]
    assert {param["name"] for param in params} == {
        "category",
        "load_id",
        "description_search",
        "limit",
        "offset",
    }
    assert "find-params" in schemas["TaskType"]["enum"]


def test_archive_contract_is_previous_version():
    """Архивная копия — предыдущий контракт (32 операции), по нему построены этапы T0–T3."""
    assert ARCHIVE_PATH.is_file(), f"нет архивной копии: {ARCHIVE_PATH.as_posix()}"
    archived = load(ARCHIVE_PATH)

    assert hashlib.sha256(ARCHIVE_PATH.read_bytes()).hexdigest() == ARCHIVE_SHA256
    assert len(operations(archived)) == ARCHIVE_OPERATIONS
