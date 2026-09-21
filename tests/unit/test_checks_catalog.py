"""Тесты каталога проверок чек-листа (FR-T4, этапы T3–T5).

Главная проверка — **совпадение каталога с программой испытаний**: описания проверок
групп `TC-SYS`, `TC-FILE`, `TC-REC`, `TC-LOAD`, `TC-TASK`, `TC-DS` должны
соответствовать таблицам `docs/02` (идентификатор, название, требования, класс).

Дополнительно проверяется связность каталога с остальными частями пульта:

    * эндпоинты проверок есть в реестре консоли (`acceptance.endpoints`), а негативные
      пробы (`probe_paths`) — наоборот, **отсутствуют**: их отсутствие и подтверждается;
    * автоматические сценарии объявлены в модулях-владельцах и видны общему реестру
      `acceptance.checks.runner`;
    * у проверок с ожидаемым дефектом API есть шаблон замечания (`notes.note_from_check`);
    * модули проверок совпадают со справочником замечаний (`notes.MODULES`);
    * структура каталога готова к этапам T6–T10 (10 групп, 69 проверок).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from acceptance import endpoints as ep
from acceptance import notes
from acceptance.checks import catalog, runner
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

#: Группы, наполненные в каталоге (этапы T3–T5).
IMPLEMENTED_GROUPS: tuple[str, ...] = (
    "TC-SYS",
    "TC-FILE",
    "TC-REC",
    "TC-LOAD",
    "TC-TASK",
    "TC-DS",
)

#: Состав классов по таблицам `docs/02` §4–§9. Замечание: сводка §14 для `TC-FILE`
#: указывает «tech 10, live 4», но в таблице §5 у `TC-FILE-14` (удаление
#: несуществующего файла) класс `tech` — каталог следует таблице, а не сводке.
CLASSES_BY_GROUP: dict[str, dict[str, int]] = {
    "TC-SYS": {"tech": 5, "manual": 1},
    "TC-FILE": {"tech": 11, "live": 3},
    "TC-REC": {"tech": 5, "manual": 1},
    "TC-LOAD": {"tech": 7},
    "TC-TASK": {"tech": 4, "live": 3, "manual": 1},
    "TC-DS": {"tech": 3, "live": 3, "heavy": 1},
}

#: Проверки без автоматического сценария (выполняются только оператором).
MANUAL_CHECKS: tuple[str, ...] = ("TC-SYS-06", "TC-REC-03", "TC-TASK-08")

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
@pytest.mark.parametrize("group_key", IMPLEMENTED_GROUPS)
def test_group_matches_checklist(checklist: str, group_key: str):
    """Группа совпадает с таблицей `docs/02` (id, название, требования, класс)."""
    rows = checklist_rows(checklist, group_key)
    specs = catalog.by_group(group_key)

    assert rows, f"в docs/02 нет строк группы {group_key}"
    assert len(rows) == len(specs)
    assert [row["id"] for row in rows] == [spec.check_id for spec in specs]
    for row, spec in zip(rows, specs, strict=True):
        assert row["title"] == spec.title, spec.check_id
        assert row["requirement"] == spec.requirement, spec.check_id
        assert CLASS_BY_LABEL[row["klass"]] == spec.check_class, spec.check_id


@pytest.mark.parametrize("group_key", IMPLEMENTED_GROUPS)
def test_group_classes_match_checklist(group_key: str):
    """Состав классов группы совпадает с таблицей программы испытаний."""
    classes: dict[str, int] = {}
    for spec in catalog.by_group(group_key):
        classes[str(spec.check_class)] = classes.get(str(spec.check_class), 0) + 1

    assert classes == CLASSES_BY_GROUP[group_key]


def test_catalog_is_ready_for_next_stages():
    """Каталог описывает 10 групп; на этапах T3–T5 наполнены шесть групп (48 проверок)."""
    summary = catalog.catalog_summary()

    assert summary["groups_total"] == 10
    assert summary["checks_total"] == 69
    assert summary["groups_implemented"] == 6
    assert summary["checks_implemented"] == 48
    assert [group.key for group in catalog.groups(implemented_only=True)] == list(
        IMPLEMENTED_GROUPS
    )
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
    for key in IMPLEMENTED_GROUPS:
        group = catalog.group(key)
        assert group is not None
        assert group.is_implemented and group.is_complete
    pending = catalog.group("TC-MOD")
    assert pending is not None
    assert not pending.is_implemented
    assert not pending.is_complete


def test_check_ids_are_unique_and_looked_up():
    """Идентификаторы проверок уникальны и доступны через поиск."""
    ids = catalog.check_ids()

    assert len(ids) == len(set(ids))
    assert ids[0] == "TC-SYS-01"
    for spec in catalog.CHECKS:
        assert catalog.find(spec.check_id) is spec
        assert catalog.find(spec.check_id.lower()) is spec
        assert catalog.group_of(spec.check_id) == spec.check_id.rsplit("-", 1)[0]
    assert catalog.stage_of("TC-TASK-01") == "T4"
    assert catalog.stage_of("TC-FILE-01") == "T3"
    assert catalog.stage_of("TC-DS-01") == "T5"
    assert catalog.group_of("TC-DS-01") == "TC-DS"
    assert len(catalog.by_group("TC-DS")) == 7
    # группы следующих этапов ещё не наполнены — проверок в них нет
    assert catalog.group_of("TC-MOD-01") == ""
    assert catalog.stage_of("TC-MOD-01") == ""
    assert catalog.group("TC-MOD") is not None
    assert catalog.by_group("TC-MOD") == ()


# ---------------------------------------------------------------------------
# Связность каталога с консолью, сценариями и замечаниями
# ---------------------------------------------------------------------------
def test_endpoints_exist_in_console_registry():
    """Эндпоинты проверок описаны в реестре консоли, а негативные пробы — нет."""
    for spec in catalog.CHECKS:
        for key in spec.endpoints:
            assert ep.find(key) is not None, f"{spec.check_id}: {key}"
        for path in spec.probe_paths:
            assert ep.find(path) is None, (
                f"{spec.check_id}: проба {path} не должна быть в реестре консоли — "
                "проверка подтверждает отсутствие маршрута"
            )
        assert spec.endpoints or spec.probe_paths or spec.automation is None, spec.check_id


def test_automations_are_declared():
    """Сценарии объявлены в модулях-владельцах и видны общему реестру `runner`."""
    automated = [spec for spec in catalog.CHECKS if spec.automation]
    manual = [spec for spec in catalog.CHECKS if not spec.automation]

    assert [spec.check_id for spec in manual] == list(MANUAL_CHECKS)
    for spec in automated:
        assert runner.scenario(spec) is not None, spec.check_id
    for spec in manual:
        assert runner.scenario(spec) is None, spec.check_id

    names = runner.automation_names()
    # 45 проверок автоматизированы; 46-й сценарий (`tasks.external_observation`) —
    # вспомогательный: он подтверждает ручную проверку TC-TASK-08 (BR-R5)
    assert len(names) == len(automated) + 1 == 46
    assert len(set(names)) == len(names)
    assert "tasks.external_observation" in names
    assert "files.round_trip" in names
    assert "loads.missing_path" not in names


def test_check_specs_are_filled_for_report():
    """Описания проверок пригодны для отчёта: шаги, ожидание, модуль и трассировка."""
    for spec in catalog.CHECKS:
        assert isinstance(spec, CheckSpec)
        assert spec.steps, spec.check_id
        assert spec.expected, spec.check_id
        assert spec.requirement, spec.check_id
        assert spec.module in notes.MODULES, spec.check_id
        assert spec.title, spec.check_id
        assert spec.class_label


def test_blocked_checks_have_known_defects():
    """У проверок с ожидаемым дефектом API есть шаблон замечания (FR-T7)."""
    blocked = [spec for spec in catalog.CHECKS if spec.blocked_by_api]

    assert [spec.check_id for spec in blocked] == [
        "TC-FILE-10",
        "TC-REC-05",
    ]
    for spec in blocked:
        note = notes.note_from_check(spec.check_id)
        assert note is not None, spec.check_id
        assert note.priority == "P0", spec.check_id
        assert note.source == notes.SOURCE_AUTO
        assert note.check_id == spec.check_id
