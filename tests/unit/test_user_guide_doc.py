"""Стоп-тест руководства пользователя (`docs/12. Руководство пользователя…md`).

Руководство описывает функционал и правила пульта, поэтому в двух местах оно
обязано совпадать с кодом **посимвольно**, а не «по смыслу»:

    * §15.5 «Шаблоны известных дефектов» и §25.2 «Дефекты и обходные пути» —
      заголовки и приоритеты берутся из реестра `acceptance/notes.py`
      (`KNOWN_DEFECTS`), иначе оператор читает в руководстве устаревшие
      приоритеты (находка 21.09.2026: дефект Loads/`ph_n` был закрыт контрактом
      17.09.2026, а в руководстве оставался как действующий `P0`/`P1`);
    * шапка документа и приложение §26.7 — имена файлов контракта: действующий
      контракт в пульте называется `docs/SOM1.json`, поставка сохраняется как
      `docs/api_17_09_26.json`, архив — `docs/SOM1.2026-08-31.json` (таблица
      версий — `docs/README.md`), поэтому ссылки руководства на `SOM1.json`
      корректны и не требуют переписывания на имя поставки.

Дополнительно проверяются числа каталога проверок («48 / 69») — они выводятся
из `acceptance.checks.catalog`, а не набираются руками.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from acceptance import notes as notes_api
from acceptance.checks import catalog

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
DOC_PATH = DOCS_DIR / "12. Руководство пользователя пульта испытаний (функционал и правила).md"

TEMPLATES_BEGIN = "### 15.5."
TEMPLATES_END = "### 15.6."
DEFECTS_BEGIN = "### 25.2."
DEFECTS_END = "### 25.3."
APPENDIX_BEGIN = "### 26.7."
APPENDIX_END = "### 26.8."

#: Строка списка §15.5: «4. Заголовок — `P2`.» (приоритет — последний элемент).
TEMPLATE_LINE = re.compile(r"^\d+\.\s+(?P<title>.+)\s+—\s+`(?P<priority>P\d)`\.$")

#: Ожидаемые «устаревшие» формулировки, которых в документе быть не должно.
STALE_PHRASES = (
    "рабочий фильтр `ph_n` не действует",
    "Нет `phase_connection` в реестре нагрузок",
)

#: Имена файлов контракта, которые обязаны быть в шапке и в приложении §26.7.
CONTRACT_FILES = ("SOM1.json", "api_17_09_26.json", "SOM1.2026-08-31.json")

#: SHA-256 действующего контракта (docs/README.md §«Версии контракта API»).
CONTRACT_SHA_PREFIX = "50f8ed29"


@pytest.fixture(scope="module")
def document() -> str:
    """Текст руководства пользователя."""
    assert DOC_PATH.exists(), f"нет руководства: {DOC_PATH.as_posix()}"
    return DOC_PATH.read_text(encoding="utf-8")


def _fragment(text: str, begin: str, end: str) -> str:
    """Фрагмент документа между двумя заголовками третьего уровня."""
    start = text.index(begin)
    return text[start : text.index(end, start + len(begin))]


def _defects() -> list[tuple[str, str]]:
    """Пары «заголовок шаблона → приоритет» из реестра замечаний."""
    return [(str(item["title"]), str(item["priority"])) for item in notes_api.KNOWN_DEFECTS]


def test_templates_section_matches_registry(document: str) -> None:
    """§15.5 перечисляет все шаблоны `KNOWN_DEFECTS` — с теми же приоритетами."""
    fragment = _fragment(document, TEMPLATES_BEGIN, TEMPLATES_END)
    parsed = [
        (match.group("title").strip(), match.group("priority"))
        for line in fragment.splitlines()
        if (match := TEMPLATE_LINE.match(line.strip()))
    ]

    assert f"({len(notes_api.KNOWN_DEFECTS)})" in fragment, "в заголовке §15.5 — неверное число"
    assert parsed == _defects()


def test_defects_table_matches_registry(document: str) -> None:
    """Таблица §25.2 — тот же состав, порядок и приоритеты, что в реестре."""
    fragment = _fragment(document, DEFECTS_BEGIN, DEFECTS_END)
    rows: list[tuple[str, str]] = []
    for line in fragment.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in stripped.strip("|").split("|")]
        if len(cells) < 3 or cells[1] not in notes_api.PRIORITIES:
            continue
        rows.append((cells[0], cells[1]))

    assert rows == _defects()


def test_loads_defect_is_marked_as_closed(document: str) -> None:
    """Дефект Loads (`phase_connection`/`ph_n`) описан как закрытый контрактом."""
    for begin, end in ((TEMPLATES_BEGIN, TEMPLATES_END), (DEFECTS_BEGIN, DEFECTS_END)):
        fragment = _fragment(document, begin, end)
        assert "закрыто контрактом 17.09.2026" in fragment, f"нет пометки «закрыто» в {begin}"


def test_document_has_no_stale_defect_wording(document: str) -> None:
    """В документе нет устаревших формулировок о дефекте Loads."""
    for phrase in STALE_PHRASES:
        assert phrase not in document, f"устаревшая формулировка: {phrase!r}"


def test_header_explains_contract_files(document: str) -> None:
    """Шапка объясняет имена контракта: действующий, поставка и архив — отдельно."""
    header = document[: document.index("---")]
    for name in CONTRACT_FILES:
        assert name in header, f"в шапке не упомянут {name}"
    assert CONTRACT_SHA_PREFIX in header, "в шапке нет SHA-256 действующего контракта"


def test_appendix_lists_contract_files(document: str) -> None:
    """Приложение §26.7 перечисляет все три файла контракта."""
    fragment = _fragment(document, APPENDIX_BEGIN, APPENDIX_END)
    for name in CONTRACT_FILES:
        assert name in fragment, f"в §26.7 не упомянут {name}"


def test_catalog_numbers_match_code(document: str) -> None:
    """Числа каталога проверок в руководстве совпадают с `catalog.catalog_summary()`."""
    summary = catalog.catalog_summary()
    checks = summary["checks_implemented"]
    total = summary["checks_total"]
    groups = summary["groups_implemented"]
    groups_total = summary["groups_total"]

    assert f"{checks} проверок в {groups} группах" in document
    assert f"из {groups_total} групп / {total} проверок" in document
    assert f"{checks} из {total}" in document
    assert f"{checks} / {total}" in document


def test_stage_report_is_registered_in_docs_readme() -> None:
    """Отчёт этапа «Монитор обмена» существует и внесён в перечень документов испытаний."""
    docs_readme = (DOCS_DIR / "README.md").read_text(encoding="utf-8")
    stage_reports = sorted(DOCS_DIR.glob("13.*.md"))

    assert stage_reports, "нет отчёта этапа docs/13"
    assert stage_reports[0].name in docs_readme
    assert stage_reports[0].name in DOC_PATH.read_text(encoding="utf-8"), (
        "руководство не ссылается на отчёт этапа"
    )


def test_contract_composition_in_header(document: str) -> None:
    """Состав действующего контракта в шапке: 34 пути / 40 операций / 49 схем / 10 тегов."""
    header = document[: document.index("---")]

    assert "34 пути" in header
    assert "40 операций" in header
    assert "49 схем" in header
    assert "10 тегов" in header
