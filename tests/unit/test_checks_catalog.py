"""Тесты каталога проверок чек-листа (FR-T4, этап T4).

Главная проверка — **совпадение каталога с программой испытаний**: описания
проверок группы `TC-TASK` должны соответствовать таблице `docs/02`
(идентификатор, название, требования, класс), а состав групп — сводке §14.

Дополнительно проверяется, что каталог «сшит» с остальными частями пульта:
эндпоинты проверок есть в реестре консоли, автоматические сценарии объявлены в
`acceptance.checks.tasks`, а структура готова к добавлению групп T3–T10.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from acceptance import endpoints as ep
from acceptance.checks import catalog
from acceptance.checks import tasks as check_tasks
from acceptance.checks.registry import CheckClass, CheckSpec

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
CHECKLIST_PATH = DOCS_DIR / "02. Чек-лист испытаний.md"

#: Разделы программы испытаний: заголовок группы → ключ группы (docs/02 §4–§13).
GROUP_HEADINGS: dict[str, str] = {
    "## 4. TC-SYS": "TC-SYS",
    "## 5. TC-FILE": "TC-FILE",
    "## 6. TC-REC": "TC-REC",
    "## 7. TC-LOAD": "TC-LOAD",
    "## 8. TC-TASK": "TC-TASK",
    "## 9. TC-DS": "TC-DS",
    "## 10. TC-MOD": "TC-MOD",
    "## 11. TC-TR": "TC-TR",
    "## 12. TC-INF": "TC-INF",
    "## 13. TC-CLEAN": "TC-CLEAN",
}

SUMMARY_BEGIN = "## 14."
SUMMARY_END = "## 15."

CHECK_ROW = re.compile(
    r"^\|\s*(?P<id>TC-[A-Z]+-\d{2})\s*\|(?P<title>[^|]*)\|(?P<requirement>[^|]*)\|"
    r"(?P<klass>[^|]*)\|"
)

#: Классы в терминах программы испытаний.
CLASS_BY_LABEL = {
    "tech": CheckClass.TECH,
    "live": CheckClass.LIVE,
    "heavy": CheckClass.HEAVY,
    "manual": CheckClass.MANUAL,
}


@pytest.fixture(scope="module")
def checklist() -> str:
    """Текст программы испытаний (`docs/02`)."""
    assert CHECKLIST_PATH.exists(), f"нет чек-листа: {CHECKLIST_PATH.as_posix()}"
    return CHECKLIST_PATH.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Фрагмент программы испытаний: от заголовка группы до следующего раздела."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def checklist_rows(checklist: str, group_key: str) -> list[dict[str, str]]:
    """Строки таблицы проверок группы из `docs/02` (id, название, требования, класс)."""
    heading = next(head for head, key in GROUP_HEADINGS.items() if key == group_key)
    rows: list[dict[str, str]] = []
    for line in _section(checklist, heading).splitlines():
        match = CHECK_ROW.match(line.strip())
        if match:
            rows.append({key: value.strip() for key, value in match.groupdict().items()})
    return rows


# ---------------------------------------------------------------------------
# Совпадение каталога с программой испытаний
# ---------------------------------------------------------------------------
def test_task_group_matches_checklist(checklist: str):
    """Группа `TC-TASK` совпадает с таблицей `docs/02` §8 (id, название, класс)."""
    rows = checklist_rows(checklist, "TC-TASK")
    specs = catalog.by_group("TC-TASK")

    assert len(rows) == 8
    assert [row["id"] for row in rows] == [spec.check_id for spec in specs]
    for row, spec in zip(rows, specs, strict=True):
        assert row["title"] == spec.title, spec.check_id
        assert row["requirement"] == spec.requirement, spec.check_id
        assert CLASS_BY_LABEL[row["klass"]] == spec.check_class, spec.check_id


def test_group_counts_match_summary(checklist: str):
    """Состав групп совпадает со сводкой `docs/02` §14 (все 69 проверок)."""
    start = checklist.index(SUMMARY_BEGIN)
    summary = checklist[start : checklist.index(SUMMARY_END, start)]
    documented = {
        match.group(1): int(match.group(2))
        for match in re.finditer(r"\|\s*(TC-[A-Z]+)\s*\|\s*(\d+)\s*\|", summary)
    }

    assert documented == {group.key: group.plan.checks_total for group in catalog.GROUPS}
    assert catalog.PLANNED_CHECKS_TOTAL == 69


def test_groups_are_ready_for_next_stages():
    """Каталог описывает все 10 групп; на этапе T4 наполнена только `TC-TASK`."""
    summary = catalog.catalog_summary()

    assert summary["groups_total"] == 10
    assert summary["checks_total"] == 69
    assert summary["checks_implemented"] == 8
    assert [group.key for group in catalog.groups(implemented_only=True)] == ["TC-TASK"]
    assert [group.stage for group in catalog.GROUPS] == [
        "T3",
        "T3",
        "T3",
        "T3",
        "T4",
        "T5",
        "T6",
        "T7",
        "T8",
        "T9",
    ]
    task_group = catalog.group("TC-TASK")
    assert task_group is not None
    assert task_group.is_implemented
    assert task_group.is_complete
    pending = catalog.group("TC-DS")
    assert pending is not None
    assert not pending.is_implemented
    assert not pending.is_complete


def test_task_group_classes_match_program():
    """Классы проверок группы `TC-TASK`: tech 4, live 3, manual 1 (docs/02 §14)."""
    classes: dict[str, int] = {}
    for spec in catalog.by_group("TC-TASK"):
        classes[str(spec.check_class)] = classes.get(str(spec.check_class), 0) + 1

    assert classes == {"tech": 4, "live": 3, "manual": 1}
    assert catalog.catalog_summary()["classes"] == classes


# ---------------------------------------------------------------------------
# Связность каталога с движком, консолью и сценариями
# ---------------------------------------------------------------------------
def test_check_ids_are_unique_and_looked_up():
    """Идентификаторы проверок уникальны и доступны через поиск."""
    ids = catalog.check_ids()

    assert len(ids) == len(set(ids))
    assert ids[0] == "TC-TASK-01"
    for spec in catalog.CHECKS:
        assert catalog.find(spec.check_id) is spec
        assert catalog.find(spec.check_id.lower()) is spec
        assert catalog.group_of(spec.check_id) == "TC-TASK"
        assert catalog.stage_of(spec.check_id) == "T4"
    assert catalog.find("TC-SYS-01") is None
    assert catalog.group_of("TC-SYS-01") == ""
    assert catalog.stage_of("TC-SYS-01") == ""
    assert catalog.group("TC-DS") is not None
    assert catalog.by_group("TC-SYS") == ()


def test_endpoints_exist_in_console_registry():
    """Каждый эндпоинт проверки описан в реестре консоли (единый источник)."""
    for spec in catalog.by_group("TC-TASK"):
        assert spec.endpoints, spec.check_id
        for key in spec.endpoints:
            assert ep.find(key) is not None, f"{spec.check_id}: {key}"


def test_automations_are_declared():
    """Ключи автоматических сценариев объявлены в `acceptance.checks.tasks`."""
    automated = [spec for spec in catalog.by_group("TC-TASK") if spec.automation]
    manual = [spec for spec in catalog.by_group("TC-TASK") if not spec.automation]

    assert len(automated) == 7
    assert [spec.check_id for spec in manual] == ["TC-TASK-08"]
    for spec in automated:
        assert check_tasks.scenario(spec) is not None, spec.check_id
    assert check_tasks.scenario(catalog.find("TC-TASK-08")) is None
    assert "tasks.external_observation" in check_tasks.AUTOMATIONS


def test_check_specs_are_filled_for_report():
    """Описания проверок пригодны для отчёта: шаги, ожидание и трассировка заполнены."""
    for spec in catalog.CHECKS:
        assert isinstance(spec, CheckSpec)
        assert spec.steps
        assert spec.expected
        assert spec.requirement
        assert spec.module == "Task service"
        assert spec.class_label
