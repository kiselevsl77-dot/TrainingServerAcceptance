"""Тесты экрана «Наборы проверок» (`SCR-101`) и запретов планирования.

Набор — документ планирования испытаний (`FR-P-65…FR-P-67`): слева каталог проверок, справа
состав с порядком, ревизиями и утверждением. Здесь держатся правила экрана:

    * статусы строк каталога **только читаются** из `results.py` (`DR-P-5`) — планирование
      результатов не ставит и не меняет;
    * каталог проверок не редактируется (источник состава — `docs/02`);
    * ни одной команды запуска (`IR-P-18`): запуск один, на «Прогоне».

Логика вынесена в чистые функции, поэтому проверяется без Streamlit и без стенда.
"""

from __future__ import annotations

import json
import pathlib

from acceptance import results as results_api
from acceptance import sets as sets_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.session import TestSession as SessionModel
from acceptance.session import new_session
from acceptance.ui.screens import scr101_sets

#: Команды запуска и исполнитель прогона: в планировании их быть не должно (`IR-P-18`).
RUN_COMMANDS = (
    "execute_next",
    "run_batch_tech",
    "auto_step",
    "execute_call",
    "execute_check",
    "from acceptance.runner",
)


def _library() -> sets_api.SetsLibrary:
    """Стартовая библиотека наборов из каталога (как после первого запуска пульта)."""
    return sets_api.library_from_catalog(author="Петров П.П.")


def _set(library: sets_api.SetsLibrary, set_id: str = sets_api.SMOKE_SET_ID) -> sets_api.CheckSet:
    """Набор библиотеки по идентификатору."""
    found = library.find(set_id)
    assert found is not None
    return found


# ---------------------------------------------------------------------------
# Каталог: фильтры, членство и статусы сессии
# ---------------------------------------------------------------------------
def test_catalog_rows_show_membership_and_session_status():
    """Строка каталога несёт членство в наборе и статус текущей сессии (`DR-P-5`)."""
    library = _library()
    check_set = _set(library)
    session = new_session(base_url="http://test.local")
    first = check_set.check_ids[0]
    results_api.record(
        session,
        {"check_id": first, "status": str(CheckStatus.PASSED), "verdict": "ок"},
    )

    rows = scr101_sets.catalog_rows(check_set, results_api.state(session))

    assert len(rows) == len(catalog.CHECKS), "каталог показывается целиком"
    marked = next(row for row in rows if row["check_id"] == first)
    assert marked["member"] is True and marked["mark"] == scr101_sets.MARK_IN
    assert marked["status"].endswith("успех"), "статус сессии виден у строки каталога"
    outside = next(row for row in rows if not row["member"])
    assert outside["mark"] == scr101_sets.MARK_OUT
    assert outside["status"] == "⚪ не выполнена"


def test_catalog_rows_mark_blocked_members():
    """Пункт набора, заблокированный дефектом API, помечается до прогона (`FR-P-19`)."""
    blocked = next(spec for spec in catalog.CHECKS if spec.blocked_by_api)
    check_set = sets_api.CheckSet(set_id="SET-BLOCKED", title="набор с дефектом")
    check_set.add([blocked.check_id])
    empty_set = sets_api.CheckSet(set_id="SET-EMPTY", title="пустой набор")

    inside = next(
        item for item in scr101_sets.catalog_rows(check_set) if item["check_id"] == blocked.check_id
    )
    outside = next(
        item for item in scr101_sets.catalog_rows(empty_set) if item["check_id"] == blocked.check_id
    )

    assert inside["blocked"] is True
    assert inside["note"] == scr101_sets.BLOCKED_NOTE
    assert outside["blocked"] is True, "дефект виден и вне набора"
    assert outside["note"] == "", "примечание относится только к пунктам набора"


def test_catalog_filters_narrow_selection():
    """Фильтры каталога: раздел, класс, членство в наборе и поиск (`IR-P-17`)."""
    library = _library()
    check_set = _set(library)
    rows = scr101_sets.catalog_rows(check_set)
    section = catalog.group_of(check_set.check_ids[0])

    by_section = scr101_sets.filter_catalog(rows, section=section)
    assert by_section and all(row["section"] == section for row in by_section)

    members = scr101_sets.filter_catalog(rows, membership=scr101_sets.MEMBERSHIP_IN)
    assert [row["check_id"] for row in members] == check_set.check_ids

    strangers = scr101_sets.filter_catalog(rows, membership=scr101_sets.MEMBERSHIP_OUT)
    assert strangers and all(row["member"] is False for row in strangers)
    assert len(strangers) == len(rows) - check_set.size

    found = scr101_sets.filter_catalog(rows, search=check_set.check_ids[0].lower())
    assert [row["check_id"] for row in found] == [check_set.check_ids[0]]
    assert scr101_sets.filter_catalog(rows, search="нет-такой-проверки") == []
    assert len(scr101_sets.filter_catalog(rows, section=scr101_sets.ANY)) == len(rows)


def test_catalog_options_and_filtered_ids():
    """Варианты фильтров и массовый выбор по фильтру (`IR-P-17`)."""
    library = _library()
    rows = scr101_sets.catalog_rows(_set(library))

    assert scr101_sets.section_options(rows)[0] == scr101_sets.ANY
    assert scr101_sets.class_options(rows)[0] == scr101_sets.ANY
    assert len(scr101_sets.section_options(rows)) > 1
    assert scr101_sets.filtered_ids(rows[:3]) == [row["check_id"] for row in rows[:3]]
    assert scr101_sets.MEMBERSHIP_CHOICES[0] == scr101_sets.MEMBERSHIP_ALL


# ---------------------------------------------------------------------------
# Состав набора, KPI и предупреждения
# ---------------------------------------------------------------------------
def test_member_rows_follow_set_order():
    """Строки состава идут в порядке набора, обязательные помечены (`DR-P-13`)."""
    library = _library()
    check_set = _set(library)
    check_set.set_mandatory(check_set.check_ids[0])

    rows = scr101_sets.member_rows(check_set)

    assert [row["check_id"] for row in rows] == check_set.check_ids
    assert rows[0]["mark"] == scr101_sets.MARK_MANDATORY
    assert rows[0]["order"] == 1
    assert rows[0]["status"] == "⚪ не выполнена"
    assert rows[0]["check_class"], "класс проверки показывается словами"


def test_member_rows_mark_unknown_check():
    """Пункт, которого нет в каталоге, помечается: его нельзя исполнить."""
    library = _library()
    check_set = _set(library)
    unknown = sets_api.CheckSet(set_id="SET-X", title="набор")
    unknown.add([check_set.check_ids[0], "TC-UNKNOWN-99"])

    rows = {row["check_id"]: row for row in scr101_sets.member_rows(unknown)}

    assert rows["TC-UNKNOWN-99"]["note"] == "нет в каталоге: исполнена быть не может"
    assert rows[check_set.check_ids[0]]["note"] == ""


def test_set_kpi_counts_classes_and_mandatory():
    """KPI набора: объём, состав по классам и число обязательных пунктов (`FR-P-65`)."""
    library = _library()
    check_set = _set(library)
    kpi = dict(scr101_sets.set_kpi(check_set))
    counted = scr101_sets.class_counts(check_set.check_ids)

    assert kpi["Пунктов"] == check_set.size
    assert kpi["Обязательный смоук"] == len(check_set.mandatory_ids)
    assert sum(counted.values()) == check_set.size, "каждый пункт попадает в свой класс"
    assert kpi["tech"] == counted["tech"]


def test_run_cards_note_names_confirmable_checks():
    """Пункты live/heavy названы: им нужна карточка запуска (`FR-P-19`)."""
    live = next(spec for spec in catalog.CHECKS if spec.is_confirmation_required)

    note = scr101_sets.run_cards_note([live.check_id])

    assert live.check_id in note
    assert "FR-P-19" in note
    assert "не требуются" in scr101_sets.run_cards_note([])


def test_mandatory_and_membership_notes():
    """Обязательный смоук и пересечение с другими наборами объясняются текстом (`FR-P-68`)."""
    library = _library()
    check_set = next(item for item in library.sets if not item.mandatory_ids)

    assert "не отмечен" in scr101_sets.mandatory_note(check_set)

    for check_id in check_set.check_ids[:2]:
        check_set.set_mandatory(check_id)

    assert "идёт первыми пунктами" in scr101_sets.mandatory_note(check_set)
    assert "идёт первыми пунктами" in scr101_sets.mandatory_note(_set(library)), (
        "смоук-набор целиком обязательный"
    )
    assert "FR-P-68" in scr101_sets.membership_warning(library, check_set)
    assert scr101_sets.membership_warning(sets_api.SetsLibrary(sets=[check_set]), check_set) == ""


def test_session_note_reports_missing_session():
    """Без сессии экран честно говорит, что статусы не показываются (`IR-P-8`)."""
    library = _library()
    note = scr101_sets.session_note(None, library)

    assert "нет сессии" in note
    assert "наборов" in note
    session: SessionModel = new_session(base_url="http://test.local")
    assert "статусы текущей сессии показаны" in scr101_sets.session_note(session, library)


def test_set_label_and_import_round_trip():
    """Подпись набора и импорт его выгрузки: набор восстанавливается из `json` (`FR-P-70`)."""
    library = _library()
    check_set = _set(library)

    label = scr101_sets.set_label(check_set)

    assert check_set.set_id in label and check_set.title in label
    assert "проверок" in label

    imported = scr101_sets.set_from_json(json.dumps(check_set.to_dict(), ensure_ascii=False))

    assert imported.set_id == check_set.set_id
    assert imported.check_ids == check_set.check_ids
    assert imported.section == check_set.section
    assert check_set.set_id in scr101_sets.import_note(imported)


def test_set_from_json_rejects_garbage():
    """Импорт не глотает мусор: ошибка объясняется оператору (`FR-P-70`)."""
    for text in ("не json", "{}", "[1, 2, 3]"):
        try:
            scr101_sets.set_from_json(text)
        except ValueError as exc:
            assert "импорт" in str(exc)
        else:
            raise AssertionError(f"на входе {text!r} ошибки не было")


# ---------------------------------------------------------------------------
# Запреты планирования (`IR-P-4`, `IR-P-18`, `DR-P-5`)
# ---------------------------------------------------------------------------
def test_sets_screen_has_no_run_commands():
    """Экран наборов не запускает проверки: запуск один, на «Прогоне» (`IR-P-18`)."""
    source = pathlib.Path(str(scr101_sets.__file__)).read_text(encoding="utf-8")

    for command in RUN_COMMANDS:
        assert command not in source, f"в планировании найдена команда запуска {command}"
    assert "queue.begin(" not in source
    assert "results_api.record(" not in source, "планирование не пишет результаты (`DR-P-5`)"
