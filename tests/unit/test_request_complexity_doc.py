"""Тесты документа «Оценка сложности формирования запросов» (FR-T3, план этапов T5–T8).

Документ `docs/05. Сложность формирования запросов в консоли.md` описывает **все**
операции API с оценкой сложности ручного формирования запроса. Тест не даёт документу
разойтись с реестром `acceptance/endpoints.py` (а значит — со спецификацией `SOM1.json`):
набор операций в таблице, вид тела и класс безопасности должны совпадать, а уровень
сложности — назначаться явно из шкалы S1–S4.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from acceptance import endpoints as ep

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
DOC_PATH = DOCS_DIR / "05. Сложность формирования запросов в консоли.md"
CHECKLIST_PATH = DOCS_DIR / "02. Чек-лист испытаний.md"
DOCS_README_PATH = DOCS_DIR / "README.md"

#: Границы разделов документа: §3 — шкала сложности, §4 — сводная таблица, §5 — разбор.
SCALE_BEGIN = "## 3."
SCALE_END = "## 4."
TABLE_BEGIN = "## 4."
TABLE_END = "## 5."

LEVELS = ("S1", "S2", "S3", "S4")
LEVELS_EXPECTED = {"S1": 15, "S2": 16, "S3": 6, "S4": 3}
LEVELS_HARDEST = {
    "post /api/datasets/fill/{dataset_id}",
    "post /api/ml_models/models/upload",
    "post /api/ml_models/models/{model_id}/inference/single",
}
LEVELS_HARD = {
    "post /api/loads",
    "put /api/loads/{load_id}",
    "post /api/ml_models/models",
    "post /api/ml_models/models/{model_id}/train",
    # AutoML (контракт 17.09.2026): вложенные правила сетки и итерация отбора
    "post /api/ml_models/models/{model_id}/find_params/population",
    "post /api/ml_models/find_params/population/{population_id}/selection",
}

NO_BODY_CELLS = {"—", "-", "none"}
COLUMNS = 8
NUMBER, MODULE, OPERATION, SAFETY, BODY, LEVEL, PREPARATION, REASON = range(COLUMNS)
MIN_CELL_LENGTH = 15
OPERATIONS_IN_TABLE = 40

CHECK_ID_PATTERN = re.compile(r"TC-[A-Z]+-\d{2}")
OPERATION_PATTERN = re.compile(r"^(GET|POST|PUT|DELETE|PATCH)\s+(\S+)$")


@pytest.fixture(scope="module")
def document() -> str:
    """Текст документа оценки сложности."""
    assert DOC_PATH.exists(), f"нет документа оценки: {DOC_PATH.as_posix()}"
    return DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rows(document: str) -> list[list[str]]:
    """Строки сводной таблицы §4 (по восемь колонок, нумерация операций с 1)."""
    parsed = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in _fragment(document, TABLE_BEGIN, TABLE_END).splitlines()
        if line.strip().startswith("|")
    ]
    return [cells for cells in parsed if len(cells) == COLUMNS and cells[NUMBER].isdigit()]


def _fragment(text: str, begin: str, end: str) -> str:
    """Фрагмент документа между двумя заголовками второго уровня."""
    start = text.index(begin)
    return text[start : text.index(end, start + len(begin))]


def _operation_key(cell: str) -> str:
    """Ключ операции реестра (`get /health`) из ячейки «Метод и путь»."""
    match = OPERATION_PATTERN.match(cell.replace("`", "").strip())
    assert match, f"не разобрать операцию: {cell!r}"
    return f"{match.group(1).lower()} {match.group(2)}"


def _safety(cell: str) -> str:
    """Класс безопасности по подписи из ячейки «Класс»."""
    found = [str(value) for value, label in ep.SAFETY_LABELS.items() if label in cell]
    assert len(found) == 1, f"класс безопасности не определён однозначно: {cell!r}"
    return found[0]


def _body(cell: str) -> str:
    """Вид тела по ячейке «Тело» (`—` означает отсутствие тела)."""
    cleaned = cell.replace("`", "").strip()
    if cleaned in NO_BODY_CELLS:
        return ep.BODY_NONE
    assert cleaned in (ep.BODY_JSON, ep.BODY_MULTIPART), f"неизвестный вид тела: {cell!r}"
    return cleaned


def _level(cell: str) -> str:
    """Уровень сложности (`S1`…`S4`) из ячейки «Сложность»."""
    match = re.search(r"S[1-4]", cell)
    assert match, f"уровень сложности не указан: {cell!r}"
    return match.group(0)


def test_table_covers_every_operation(rows: list[list[str]]):
    """В таблице §4 есть все операции реестра (40) и нет лишних."""
    keys = [_operation_key(row[OPERATION]) for row in rows]

    assert len(rows) == OPERATIONS_IN_TABLE
    assert [int(row[NUMBER]) for row in rows] == list(range(1, OPERATIONS_IN_TABLE + 1))
    assert len(set(keys)) == len(keys)
    assert set(keys) == set(ep.endpoint_keys())


def test_table_matches_registry_modules(rows: list[list[str]]):
    """Модуль каждой строки — тег спецификации той же операции."""
    for row in rows:
        spec = ep.find(_operation_key(row[OPERATION]))

        assert spec is not None, f"операции нет в реестре: {row[OPERATION]!r}"
        assert row[MODULE] == spec.module, spec.key


def test_body_and_safety_match_registry(rows: list[list[str]]):
    """Вид тела и класс безопасности в таблице совпадают с реестром операций."""
    for row in rows:
        spec = ep.find(_operation_key(row[OPERATION]))

        assert spec is not None, f"операции нет в реестре: {row[OPERATION]!r}"
        assert _body(row[BODY]) == spec.body_kind, spec.key
        assert _safety(row[SAFETY]) == str(spec.safety), spec.key


def test_levels_are_assigned_from_scale(rows: list[list[str]]):
    """Уровни сложности назначены всем операциям и покрывают шкалу целиком."""
    levels = [_level(row[LEVEL]) for row in rows]

    assert set(levels) == set(LEVELS)
    assert Counter(levels) == LEVELS_EXPECTED


def test_hard_operations_are_marked(rows: list[list[str]]):
    """Операции, невыполнимые вручную, отмечены S4, а многосоставные — S3."""
    levels = {_operation_key(row[OPERATION]): _level(row[LEVEL]) for row in rows}

    assert {key for key, level in levels.items() if level == "S4"} == LEVELS_HARDEST
    assert {key for key, level in levels.items() if level == "S3"} == LEVELS_HARD


def test_heavy_operations_are_not_trivial(rows: list[list[str]]):
    """Ресурсоёмкие операции не помечены тривиальными, а S1 не содержит multipart."""
    levels = {_operation_key(row[OPERATION]): _level(row[LEVEL]) for row in rows}

    for spec in ep.ENDPOINTS:
        if spec.is_heavy:
            assert levels[spec.key] != "S1", spec.key
        if levels[spec.key] == "S1":
            assert spec.body_kind != ep.BODY_MULTIPART, spec.key
            assert len(spec.body_fields) <= 4, spec.key


def test_preparation_and_reason_are_filled(rows: list[list[str]]):
    """У каждой операции указано, что подготовить, и почему уровень такой."""
    for row in rows:
        assert len(row[PREPARATION]) >= MIN_CELL_LENGTH, row[OPERATION]
        assert len(row[REASON]) >= MIN_CELL_LENGTH, row[OPERATION]
        assert row[PREPARATION] != "—" and row[REASON] != "—", row[OPERATION]


def test_scale_declares_all_levels(document: str):
    """Шкала §3 описывает все четыре уровня сложности."""
    scale = _fragment(document, SCALE_BEGIN, SCALE_END)

    for level in LEVELS:
        assert level in scale, level
    assert str(OPERATIONS_IN_TABLE) in scale


def test_hard_cases_are_explained(document: str):
    """Для сложных операций есть разбор в §5."""
    for heading in ("### 5.1", "### 5.2", "### 5.3", "### 5.4", "### 5.5", "### 5.6"):
        assert heading in document, heading
    for key in LEVELS_HARDEST:
        assert key.split(" ", 1)[1] in document, key


def test_checklist_identifiers_exist(document: str):
    """Метки проверок, упомянутые в документе, есть в чек-листе испытаний."""
    checklist = CHECKLIST_PATH.read_text(encoding="utf-8")
    mentioned = set(CHECK_ID_PATTERN.findall(document))

    assert mentioned
    assert sorted(check_id for check_id in mentioned if check_id not in checklist) == []


def test_document_is_registered_in_docs_readme():
    """Документ включён в перечень документов испытаний (`docs/README.md`)."""
    readme = DOCS_README_PATH.read_text(encoding="utf-8")

    assert DOC_PATH.name in readme
