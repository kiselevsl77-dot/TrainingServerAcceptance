"""Тесты экрана «Программа сессии» (`SCR-102`) и запретов планирования.

Программа — единственный источник состава прогона (`DR-P-14`): очередь строится как её
снимок, а результаты ссылаются на ревизию. Поэтому здесь проверяется то, что видит
руководитель испытаний: объединение наборов по логическому ИЛИ с происхождением пункта
(`FR-P-68`), покрытие разделов против целевого объёма (`IR-P-19`), дельта с предыдущей
ревизией и таблицы состава.

Отдельно проверяются запреты планирования: на экранах `SCR-101`/`SCR-102` нет ни одной
команды запуска (`IR-P-4`, `IR-P-18`) — запуск живёт только на экране «Прогон».
"""

from __future__ import annotations

import pathlib

from acceptance import results as results_api
from acceptance import sets as sets_api
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme
from acceptance.session import new_session
from acceptance.ui.screens import scr101_sets, scr102_programme

#: Команды запуска и исполнитель прогона: в планировании их быть не должно (`IR-P-18`).
RUN_COMMANDS = (
    "execute_next",
    "run_batch_tech",
    "auto_step",
    "execute_check",
    "from acceptance.runner",
)


def _library() -> sets_api.SetsLibrary:
    """Стартовая библиотека наборов из каталога (как после первого запуска пульта)."""
    return sets_api.library_from_catalog(author="Петров П.П.")


def _programme(library: sets_api.SetsLibrary, *set_ids: str) -> Programme:
    """Программа из указанных наборов (объединение по ИЛИ)."""
    return Programme.from_sets(library, list(set_ids))


# ---------------------------------------------------------------------------
# Наборы и объединение по ИЛИ
# ---------------------------------------------------------------------------
def test_set_rows_describe_library():
    """Строки библиотеки: идентификатор, раздел, объём, состав и статус набора."""
    rows = scr102_programme.set_rows(_library())
    smoke = next(row for row in rows if row["set_id"] == sets_api.SMOKE_SET_ID)

    assert smoke["scope"] == sets_api.SCOPE_SMOKE
    assert smoke["size"] > 0
    assert smoke["status"] == sets_api.SET_DRAFT
    assert any(row["section"] != "—" for row in rows)


def test_set_choice_label_is_readable():
    """Подпись набора в списке выбора называет номер, название, объём и вид набора."""
    row = {"set_id": "SET-SMOKE", "title": "Смоук-минимум", "size": 4, "scope": "смоук"}

    assert (
        scr102_programme.set_choice_label(row) == "SET-SMOKE · Смоук-минимум · 4 проверки · смоук"
    )
    assert scr102_programme.set_choice_label({"set_id": "SET-X"}) == "SET-X · 0 проверок"


def test_union_caption_shows_origin_and_intersections():
    """Подпись объединения: сколько пунктов, откуда пришли и сколько пересечений."""
    library = _library()
    ids = [item.set_id for item in library.sets][:2]
    programme = _programme(library, *ids)

    caption = scr102_programme.union_caption(programme)
    counts = scr102_programme.items_by_set(programme)

    assert caption.startswith("Включено по ИЛИ:")
    assert "пересечений:" in caption
    assert sum(counts.values()) >= programme.size, "происхождение учитывает все наборы-источники"
    assert set(counts) <= set(ids)


def test_selection_defaults_return_current_sets():
    """В списке наборов отмечено то, из чего собрана текущая программа."""
    library = _library()
    ids = [item.set_id for item in library.sets][:2]
    programme = _programme(library, *ids)

    assert scr102_programme.selection_defaults(programme) == ids
    assert scr102_programme.selection_defaults(Programme()) == []


# ---------------------------------------------------------------------------
# Покрытие разделов и дельта ревизий
# ---------------------------------------------------------------------------
def test_coverage_rows_close_full_and_mark_partial_sections():
    """Покрытие: полный раздел закрыт (✅), исключение пункта даёт «◐» (`FR-P-69`)."""
    library = _library()
    section = "TC-SYS"
    set_id = library.by_section(section)[0].set_id
    programme = _programme(library, set_id)

    def row_of(marks: dict[str, dict[str, str]]) -> dict[str, str]:
        return next(value for key, value in marks.items() if key.startswith(section))

    closed = row_of({row["section"]: row for row in scr102_programme.coverage_rows(programme)})

    assert closed["mark"] == "✅"
    assert closed["in_programme"].split("/")[0] == closed["in_programme"].split("/")[1]

    programme.exclude([programme.of_section(section)[-1].check_id])
    partial = row_of({row["section"]: row for row in scr102_programme.coverage_rows(programme)})

    assert partial["mark"] == "◐"
    assert partial["missing"] == "1"

    everything = scr102_programme.coverage_rows(programme)

    assert len(everything) == len(sets_api.section_options())
    assert any(row["in_programme"] == "0/0" for row in everything), (
        "раздел, к которому программа не предъявляет объём, виден как «0/0»"
    )


def test_coverage_mark_rules():
    """Пометка раздела: ✅ покрыт, ◐ частично, ⛔ не покрыт вовсе."""
    assert scr102_programme.coverage_mark({"uncovered": False}) == "✅"
    assert scr102_programme.coverage_mark({"uncovered": True, "in_programme": 2}) == "◐"
    assert scr102_programme.coverage_mark({"uncovered": True, "in_programme": 0}) == "⛔"


def test_delta_caption_describes_change():
    """Дельта с предыдущей ревизией: что добавилось и что ушло."""
    library = _library()
    ids = [item.set_id for item in library.sets][:2]
    programme = _programme(library, *ids)

    assert scr102_programme.delta_caption(programme) == "первая ревизия: сравнивать не с чем"

    programme.approve(by="Петров П.П.")
    programme.revise(author="Петров П.П.")
    programme.exclude([programme.check_ids[-1]])
    caption = scr102_programme.delta_caption(programme)

    assert "Дельта с ревизией 1" in caption
    assert caption.count("−1") == 1
    assert "missing" not in caption


def test_short_ids_limits_long_lists():
    """Длинный список идентификаторов сокращается, но число остаётся видимым."""
    assert scr102_programme.short_ids("+", []) == ""
    assert scr102_programme.short_ids("+", ["A", "B"]) == "+2 (A, B)"
    assert scr102_programme.short_ids("+", ["A", "B", "C", "D", "E"]).endswith("всего 5)")


# ---------------------------------------------------------------------------
# Таблицы состава и ревизий
# ---------------------------------------------------------------------------
def test_programme_table_rows_show_mandatory_and_class():
    """Состав показывает обязательные пункты, класс словами и результат сессии."""
    library = _library()
    programme = _programme(library, sets_api.SMOKE_SET_ID)
    first = programme.check_ids[0]
    session = new_session(base_url="http://test.local")
    results_api.record(
        session,
        {"check_id": first, "status": str(CheckStatus.PASSED), "verdict": "соответствует"},
    )

    rows = scr102_programme.programme_table_rows(programme, results_api.state(session))
    marked = [row for row in rows if row["mark"] == "★"]

    assert marked, "обязательные пункты помечены звёздочкой"
    assert all(row["check_class"] != "tech" for row in rows), "класс показывается словами"
    assert rows[0]["status"] == "✅ успех · соответствует"


def test_programme_table_rows_mark_unknown_checks():
    """Проверка, которой нет в каталоге, помечается: её нельзя исполнить."""
    library = _library()
    programme = _programme(library, sets_api.SMOKE_SET_ID)
    programme.add_check("TC-UNKNOWN-99")

    rows = scr102_programme.programme_table_rows(programme)
    unknown = next(row for row in rows if row["check_id"] == "TC-UNKNOWN-99")

    assert unknown["note"] == "нет в каталоге"


def test_revision_table_rows_list_current_and_archive():
    """Ревизии: текущая помечена, архивные видны с автором и составом."""
    library = _library()
    ids = [item.set_id for item in library.sets][:2]
    programme = _programme(library, *ids)
    programme.approve(by="Петров П.П.", comment="первая")
    programme.revise(author="Иванов И.И.", comment="правка после замечания")

    rows = scr102_programme.revision_table_rows(programme)

    assert rows[0]["current"] == "текущая"
    assert rows[0]["state"] == "черновик"
    assert rows[1]["state"] == "утверждена"
    assert rows[1]["comment"] == "первая"


def test_regress_source_collects_failures_of_session():
    """Шаблон «регресс» берёт отказы и блокировки сессии (`FR-P-70`)."""
    session = new_session(base_url="http://test.local")
    results_api.record(
        session,
        {"check_id": "TC-SYS-01", "status": str(CheckStatus.PASSED), "verdict": "успех"},
    )
    results_api.record(
        session,
        {"check_id": "TC-SYS-03", "status": str(CheckStatus.FAILED), "verdict": "405 вместо 404"},
    )
    results_api.record(
        session,
        {"check_id": "TC-FILE-10", "status": str(CheckStatus.BLOCKED), "verdict": "дефект API"},
    )

    assert sorted(scr102_programme.regress_source(session)) == ["TC-FILE-10", "TC-SYS-03"]
    assert scr102_programme.regress_source(new_session(base_url="http://test.local")) == []


# ---------------------------------------------------------------------------
# Запреты планирования (`IR-P-4`, `IR-P-18`)
# ---------------------------------------------------------------------------
def test_planning_screens_have_no_run_commands():
    """Экраны планирования не запускают проверки: запуск — только на `SCR-301` (`IR-P-4`)."""
    for module in (scr101_sets, scr102_programme):
        source = pathlib.Path(str(module.__file__)).read_text(encoding="utf-8")
        for command in RUN_COMMANDS:
            assert command not in source, f"{module.KEY}: в планировании найдена команда {command}"


def test_planning_screen_has_no_direct_result_writes():
    """Планирование не пишет результаты: единственный источник — прогон (`DR-P-5`)."""
    source = pathlib.Path(str(scr102_programme.__file__)).read_text(encoding="utf-8")

    assert "results_api.record(" not in source
    assert "results_api.suspend(" not in source
    assert "results_api.annotate(" not in source
