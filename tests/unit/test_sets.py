"""Тесты библиотеки наборов проверок (`acceptance/sets.py`).

Проверяются свойства, на которых держится этап «Планирование испытаний»
(`FR-P-65…FR-P-67`, `DR-P-13`): состав и порядок набора, ревизии и утверждение,
библиотека в целом и её файловое хранение.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acceptance import sets as sets_api
from acceptance.checks import catalog


def revision_of(item: sets_api.CheckSet, number: int) -> sets_api.SetRevision:
    """Архивная ревизия набора с проверкой, что она есть (помощник для типизации)."""
    found = item.find_revision(number)
    assert found is not None, f"ревизия {number} набора {item.set_id} не найдена"
    return found


@pytest.fixture()
def library() -> sets_api.SetsLibrary:
    """Библиотека, собранная из каталога (7 наборов: 6 разделов + смоук-минимум)."""
    return sets_api.library_from_catalog(author="Иванов И.И.")


def test_library_from_catalog_builds_set_per_section(library: sets_api.SetsLibrary):
    """На каждый наполненный раздел каталога — набор с полным составом раздела."""
    summary = library.summary()

    assert summary["sets"] == len(catalog.groups(implemented_only=True)) + 1
    assert summary["drafts"] == summary["sets"]

    for group in catalog.groups(implemented_only=True):
        found = library.by_section(group.key)
        assert found, f"нет набора для раздела {group.key}"
        assert found[0].check_ids == [spec.check_id for spec in group.checks]


def test_smoke_set_marks_mandatory_checks(library: sets_api.SetsLibrary):
    """«Смоук-минимум»: по одной `tech`-проверке раздела, все пункты обязательные."""
    smoke = library.find(sets_api.SMOKE_SET_ID)

    assert smoke is not None
    assert smoke.scope == sets_api.SCOPE_SMOKE
    assert smoke.mandatory_ids == smoke.check_ids
    assert smoke.check_ids == sets_api.smoke_check_ids()
    for check_id in smoke.check_ids:
        spec = catalog.find(check_id)
        assert spec is not None
        assert str(spec.check_class) == "tech"


def test_add_is_idempotent_and_reindexes(library: sets_api.SetsLibrary):
    """Добавление проверок: без дублей, порядок пересчитывается (1…N)."""
    item = library.create(title="Проверки системы", section="TC-SYS")

    added = item.add(["TC-SYS-02", "TC-SYS-01", "TC-SYS-02", ""])
    again = item.add(["TC-SYS-01"])

    assert added == ["TC-SYS-02", "TC-SYS-01"]
    assert again == []
    assert item.check_ids == ["TC-SYS-02", "TC-SYS-01"]
    assert [entry.order for entry in item.items] == [1, 2]


def test_remove_reindexes_and_reports_removed(library: sets_api.SetsLibrary):
    """Удаление проверок возвращает фактически убранные и перенумеровывает состав."""
    item = library.create(
        title="Проверки системы",
        section="TC-SYS",
        check_ids=["TC-SYS-01", "TC-SYS-02", "TC-SYS-03"],
    )

    removed = item.remove(["TC-SYS-02", "TC-SYS-99"])

    assert removed == ["TC-SYS-02"]
    assert item.check_ids == ["TC-SYS-01", "TC-SYS-03"]
    assert [entry.order for entry in item.items] == [1, 2]


def test_move_and_reorder_change_the_order(library: sets_api.SetsLibrary):
    """Порядок пунктов набора правится кнопками ↑/↓ и полным переупорядочиванием."""
    item = library.create(
        title="Проверки системы",
        section="TC-SYS",
        check_ids=["TC-SYS-01", "TC-SYS-02", "TC-SYS-03"],
    )

    assert item.move("TC-SYS-03", -1) is True
    assert item.check_ids == ["TC-SYS-01", "TC-SYS-03", "TC-SYS-02"]
    assert item.move("TC-SYS-01", -5) is False  # выше первого места не поднимается
    assert item.move("TC-SYS-99", 1) is False  # чужой проверки в наборе нет

    item.reorder(["TC-SYS-02", "TC-SYS-99"])
    assert item.check_ids == ["TC-SYS-02", "TC-SYS-01", "TC-SYS-03"]


def test_approved_set_is_locked(library: sets_api.SetsLibrary):
    """Утверждённый набор не редактируется: правила `FR-P-66`, `FR-P-67`."""
    item = library.find("SET-SYS")
    assert item is not None
    item.approve(author="Петров П.П.")

    assert item.is_approved and item.status == sets_api.SET_APPROVED
    assert item.author == "Петров П.П."
    assert item.icon == "✅"

    with pytest.raises(ValueError):
        item.add(["TC-SYS-99"])
    with pytest.raises(ValueError):
        item.remove(["TC-SYS-01"])
    with pytest.raises(ValueError):
        item.move("TC-SYS-01", 1)
    with pytest.raises(ValueError):
        item.rename("Новое имя")


def test_revise_archives_immutable_revision(library: sets_api.SetsLibrary):
    """Изменение утверждённого набора идёт через новую ревизию, архив неизменяем."""
    item = library.find("SET-SYS")
    assert item is not None
    item.approve(author="Петров П.П.", comment="исходная программа")
    item.revise(author="Петров П.П.", comment="добавлена проверка")

    assert item.status == sets_api.SET_DRAFT
    assert item.revision == 2
    archived = item.find_revision(1)
    assert archived is not None
    assert archived.size == 6
    assert archived.author == "Петров П.П."
    assert archived.comment == "исходная программа"

    item.add(["TC-SYS-99"])

    assert item.size == 7
    assert archived.size == 6  # архивная ревизия не переписывается
    assert item.find_revision(1) is archived


def test_restore_revision_keeps_history_and_opens_new_revision(library: sets_api.SetsLibrary):
    """Восстановление ревизии — это новая ревизия, прежние остаются читаемыми."""
    item = library.find("SET-FILE")
    assert item is not None
    full = list(item.check_ids)
    item.archive(author="Иванов И.И.", comment="ревизия 1")
    item.remove(["TC-FILE-01"])

    # правка зафиксированной ревизии открывает следующую (правило FR-P-66)
    assert item.revision == 2
    item.revise(author="Иванов И.И.", comment="сокращённый состав")
    item.remove(["TC-FILE-02"])
    shortened = list(item.check_ids)
    assert item.revision == 3

    restored = item.restore_revision(1, author="Иванов И.И.")

    assert restored.check_ids == full
    assert item.check_ids == full
    assert item.revision == 4
    assert item.status == sets_api.SET_DRAFT
    assert revision_of(item, 1).check_ids == full
    assert revision_of(item, 2).check_ids == [
        check_id for check_id in full if check_id != "TC-FILE-01"
    ]
    assert revision_of(item, 3).check_ids == shortened


def test_revision_rows_mark_current_revision(library: sets_api.SetsLibrary):
    """История ревизий отдаётся «сверху вниз» и помечает текущую (панель «Ревизии»)."""
    item = library.find("SET-REC")
    assert item is not None
    item.approve(author="Петров П.П.", comment="первая редакция")
    item.revise(author="Петров П.П.", comment="вторая редакция")

    rows = item.revision_rows()

    assert [row["revision"] for row in rows] == [2, 1]
    assert [row["current"] for row in rows] == [True, False]
    assert rows[1]["comment"] == "первая редакция"


def test_summary_counts_classes_and_unknown_checks(library: sets_api.SetsLibrary):
    """KPI набора: классы проверок, число обязательных и «неизвестных» пунктов."""
    item = library.find("SET-SYS")
    assert item is not None
    item.add(["TC-SYS-99"])  # проверки с таким идентификатором в каталоге нет

    summary = item.summary()

    assert summary["set_id"] == "SET-SYS"
    assert summary["section_title"] == "System"
    assert summary["size"] == 7
    assert summary["unknown"] == 1
    assert sum(summary["classes"].values()) == 6  # проверки каталога; добавленная — в unknown
    assert set(summary["classes"]) <= {"tech", "live", "heavy", "manual"}
    assert summary["scope_label"].startswith("полный")


def test_set_section_and_scope_are_editable_in_draft(library: sets_api.SetsLibrary):
    """Раздел и целевой объём набора меняются, пока набор — черновик (FR-P-65)."""
    item = library.create(title="Прочее", section="TC-SYS")

    item.set_section("TC-LOAD", scope=sets_api.SCOPE_SMOKE)

    assert item.section == "TC-LOAD"
    assert item.section_title == "Loads"
    assert item.scope == sets_api.SCOPE_SMOKE

    item.set_section("TC-LOAD", scope="неизвестный объём")
    assert item.scope == sets_api.SCOPE_SMOKE  # неверный объём не применяется


def test_replace_all_replaces_composition(library: sets_api.SetsLibrary):
    """Импорт набора заменяет состав целиком (FR-P-70)."""
    item = library.create(title="Импорт", section="TC-DS", check_ids=["TC-DS-01", "TC-DS-02"])

    item.replace_all(["TC-DS-03", "", "TC-DS-03"])

    assert item.check_ids == ["TC-DS-03"]
    assert [entry.order for entry in item.items] == [1]


def test_library_remove_requires_force_for_approved_set(library: sets_api.SetsLibrary):
    """Черновик удаляется свободно, утверждённый набор — только с `force`."""
    approved = library.find("SET-TASK")
    assert approved is not None
    approved.approve(author="Петров П.П.")

    with pytest.raises(ValueError):
        library.remove("SET-TASK")

    assert library.remove("SET-TASK", force=True) is True
    assert library.find("SET-TASK") is None
    assert library.remove("SET-TASK") is False


def test_library_copy_creates_independent_draft(library: sets_api.SetsLibrary):
    """Копия набора — независимый черновик с новым идентификатором (FR-P-65)."""
    source = library.find("SET-SYS")
    assert source is not None
    source.approve(author="Петров П.П.")

    clone = library.copy("SET-SYS", title="Система — смоук")

    assert clone.set_id == "SET-SYS-2"
    assert clone.title == "Система — смоук"
    assert clone.status == sets_api.SET_DRAFT
    assert clone.check_ids == source.check_ids
    assert clone.revisions == []
    assert library.summary()["sets"] == 8

    clone.remove(["TC-SYS-01"])
    assert source.size == 6 and clone.size == 5


def test_suggested_set_id_avoids_collisions():
    """Идентификатор набора выводится из раздела и не повторяется."""
    assert sets_api.suggested_set_id("TC-SYS") == "SET-SYS"
    assert sets_api.suggested_set_id("TC-SYS", taken=["SET-SYS"]) == "SET-SYS-2"
    assert sets_api.suggested_set_id("TC-SYS", taken=["set-sys", "SET-SYS-2"]) == "SET-SYS-3"
    assert sets_api.suggested_set_id("") == "SET-NEW"


def test_library_section_rows_show_volume(library: sets_api.SetsLibrary):
    """Раздел библиотеки: наборы, проверки и объёмы каталога (`SCR-101`, `FR-P-69`)."""
    rows = {row["section"]: row for row in library.section_rows()}

    assert set(rows) == set(sets_api.section_options())
    assert rows["TC-SYS"]["title"] == "System"
    assert rows["TC-SYS"]["sets"] == 1
    assert rows["TC-SYS"]["checks"] == 6
    assert rows["TC-SYS"]["planned"] == 6
    assert rows["TC-MOD"]["sets"] == 0  # раздел ещё не наполнен проверками


def test_library_round_trip_through_file(library: sets_api.SetsLibrary, tmp_path: Path):
    """Библиотека переживает запись и чтение файла `check_sets.json`."""
    item = library.find("SET-SYS")
    assert item is not None
    item.approve(author="Петров П.П.", comment="утверждено")
    item.revise(author="Петров П.П.", comment="добавлена проверка")
    item.add(["TC-SYS-99"])

    path = sets_api.save_sets(library, tmp_path)
    restored = sets_api.load_sets(tmp_path)
    found = restored.find("SET-SYS")

    assert path.name == sets_api.SETS_FILENAME
    assert found is not None
    assert found.revision == 2
    assert found.check_ids == item.check_ids
    assert revision_of(found, 1).size == 6
    assert restored.summary() == library.summary()


def test_corrupted_or_missing_file_gives_empty_library(tmp_path: Path):
    """Отсутствие или повреждение файла библиотеки — пустая библиотека, не падение."""
    assert sets_api.load_sets(tmp_path).sets == []

    sets_api.sets_path(tmp_path).write_text("{ это не JSON", encoding="utf-8")

    assert sets_api.load_sets(tmp_path).sets == []
    assert sets_api.load_sets(tmp_path).schema_version == sets_api.SETS_SCHEMA_VERSION


def test_library_from_dict_tolerates_unknown_and_missing_fields():
    """Чтение набора терпимо к отсутствующим и лишним полям (правило DR-P-12)."""
    payload = {
        "schema_version": 1,
        "sets": [
            {
                "set_id": "SET-SYS",
                "title": "Система",
                "status": "неизвестный статус",
                "revision": "3",
                "items": [{"check_id": "TC-SYS-01"}, {"check_id": "TC-SYS-02", "order": 2}],
                "revisions": [{"revision": 1, "items": [{"check_id": "TC-SYS-01"}]}],
                "лишнее поле": 1,
            }
        ],
    }

    library = sets_api.SetsLibrary.from_dict(payload)
    item = library.find("SET-SYS")

    assert item is not None
    assert item.status == sets_api.SET_DRAFT
    assert item.revision == 3
    assert item.check_ids == ["TC-SYS-01", "TC-SYS-02"]
    assert item.find_revision(1).size == 1
    assert item.scope == sets_api.SCOPE_STANDARD
    assert item.created_at  # поле времени заполнено значением по умолчанию
