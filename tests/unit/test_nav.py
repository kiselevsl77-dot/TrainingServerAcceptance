"""Тесты реестра экранов и навигации пульта (`acceptance/ui/nav.py`).

Реестр — не оформление, а карта приёмки: 15 целевых экранов (`SCR-101`…`SCR-501`) в пяти
группах, порядок групп совпадает с порядком бизнес-процесса (`IR-P-1`, `docs/14` §4):

    * **Планирование испытаний** — наборы и программа: без утверждённой программы прогон
      не имеет смысла;
    * **Подготовка** — обзор, стенд, сессия, данные стенда: снимок «Начало» и реквизиты;
    * **Испытания** — прогон, карточка проверки и последним монитор `$Задач` (инфраструктура,
      а не шаг приёмки);
    * **Результаты** — протокол, журнал обмена, замечания, отчёт, сравнение сессий;
    * **Инструменты** — консоль и настройки: вне маршрута приёмки.

Тест — «замок» на порядок, коды и целостность: до него порядок меню нигде не проверялся,
поэтому следующая правка реестра молча ломала бы карту приёмки. Дополнительно проверяется
связка с макетом: коды `nav.SCREENS` совпадают с разделами `docs/16` (этап 4 расширит это
до трассировки «экран ↔ требование»).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from acceptance import glossary
from acceptance.ui import nav

ROOT = Path(__file__).resolve().parents[2]
APP_PATH = ROOT / "acceptance" / "app.py"
MOCKUP_DOC = ROOT / "docs" / "16. Макет интерфейса ПУЛЬТА (целевые экраны).md"

SCREEN_HEADING = re.compile(r"^## (SCR-\d{3})\.", re.MULTILINE)

#: Ожидаемый порядок групп — как в §4 концепта (`docs/14`) и §0 макета (`docs/16`).
EXPECTED_GROUPS: tuple[str, ...] = (
    "Планирование испытаний",
    "Подготовка",
    "Испытания",
    "Результаты",
    "Инструменты",
)


def test_groups_follow_business_process_order() -> None:
    """Группы идут в порядке маршрута оператора: планирование → … → инструменты."""
    assert tuple(label for label, _ in nav.GROUPS) == EXPECTED_GROUPS


def test_planning_opens_the_screens() -> None:
    """Путь начинается с планирования: наборы и программа — первые экраны меню."""
    assert nav.group_keys("Планирование испытаний") == ("scr101_sets", "scr102_programme")
    assert nav.DEFAULT_SCREEN == "scr101_sets"
    assert nav.group_of(nav.DEFAULT_SCREEN) == "Планирование испытаний"


def test_preparation_group_is_complete() -> None:
    """«Подготовка»: обзор, стенд, сессия, данные стенда — весь вход в испытания."""
    assert nav.group_keys("Подготовка") == (
        "scr201_overview",
        "scr202_stand",
        "scr203_session",
        "scr204_data",
    )


def test_trials_start_with_run_and_end_with_tasks() -> None:
    """«Испытания»: прогон и карточка проверки — первыми, монитор `$Задач` — последним."""
    assert nav.group_keys("Испытания")[:2] == ("scr301_run", "scr302_check")
    assert nav.group_keys("Испытания")[-1] == "scr303_tasks"
    assert nav.group_of("scr303_tasks") == "Испытания"


def test_results_group_is_complete() -> None:
    """«Результаты»: протокол, журнал, замечания, отчёт, сравнение сессий."""
    assert nav.group_keys("Результаты") == (
        "scr401_protocol",
        "scr402_journal",
        "scr403_notes",
        "scr404_report",
        "scr405_compare",
    )


def test_tools_are_outside_the_acceptance_route() -> None:
    """«Инструменты» — один экран вне маршрута приёмки (консоль, настройки, справка)."""
    assert nav.group_keys("Инструменты") == ("scr501_tools",)
    assert nav.group_of("scr501_tools") == "Инструменты"


def test_there_are_fifteen_screens() -> None:
    """Целевой пульт — 15 экранов: 15 кодов, 15 ключей, пять групп (ТЗ §6.1)."""
    assert len(nav.SCREENS) == 15
    assert len(nav.screens()) == len(nav.codes()) == 15
    assert len(nav.GROUPS) == len(EXPECTED_GROUPS)


def test_codes_follow_navigation_order() -> None:
    """Коды идут по возрастанию: раздел — сотни, порядок в разделе — единицы (§1.5 ТЗ)."""
    codes = nav.codes()
    digits = [code.removeprefix("SCR-") for code in codes]

    assert codes == tuple(sorted(codes)), "коды экранов не в порядке навигации"
    assert all(len(value) == 3 for value in digits), "код экрана — три цифры"
    assert [value[0] for value in digits] == sorted(value[0] for value in digits), (
        "разделы экранов идут не по порядку навигации"
    )


def test_code_and_key_are_unique_and_linked() -> None:
    """Код и ключ однозначно указывают на экран (`by_code`/`by_key` — обратные функции)."""
    for screen in nav.SCREENS:
        assert nav.by_code(screen.code) == nav.by_key(screen.key) == screen
    assert len(set(nav.codes())) == 15
    assert len(set(nav.screens())) == 15


def test_labels_come_from_glossary_for_every_screen() -> None:
    """У каждого экрана есть подпись и пиктограмма в глоссарии — иначе меню не отрисуется."""
    assert set(glossary.SCREEN_LABELS) == set(nav.screens())
    assert set(glossary.SCREEN_ICONS) == set(nav.screens())
    for key in nav.screens():
        assert glossary.nav_label(key).startswith(glossary.SCREEN_ICONS[key])
        assert glossary.SCREEN_LABELS[key] in glossary.nav_label(key)


def test_codes_match_mockup_document() -> None:
    """Коды реестра совпадают с разделами макета (`docs/16`) и идут в том же порядке."""
    text = MOCKUP_DOC.read_text(encoding="utf-8")

    assert tuple(SCREEN_HEADING.findall(text)) == nav.codes()


def test_group_key_of_unknown_screen_raises() -> None:
    """Экран или группа, которых нет в навигации, — понятная ошибка, а не пустая строка."""
    with pytest.raises(KeyError):
        nav.group_of("__unknown__")
    with pytest.raises(KeyError):
        nav.group_keys("__Без такой группы__")
    with pytest.raises(KeyError):
        nav.by_key("__unknown__")
    with pytest.raises(KeyError):
        nav.by_code("SCR-999")


def test_screens_are_listed_once() -> None:
    """Каждый экран навигации назван ровно один раз (проверка целостности)."""
    listed = nav.screens()

    assert len(listed) == len(set(listed))
    nav.validate(listed)


def test_validate_reports_screen_without_navigation() -> None:
    """Экран пульта, забытый в навигации, — ошибка с его именем (страница «невидима»)."""
    with pytest.raises(ValueError, match="экраны отсутствуют в навигации: __extra__"):
        nav.validate((*nav.screens(), "__extra__"))


def test_validate_reports_navigation_screen_without_page() -> None:
    """Пункт навигации без экрана — тоже ошибка: иначе панель упадёт при нажатии."""
    without_tools = tuple(key for key in nav.screens() if key != "scr501_tools")

    with pytest.raises(ValueError, match="неизвестные экраны: scr501_tools"):
        nav.validate(without_tools)


def test_app_uses_navigation_module() -> None:
    """`app.py` берёт порядок, стартовый экран и подписи из реестра, а не задаёт их у себя."""
    source = APP_PATH.read_text(encoding="utf-8")

    assert "nav.GROUPS" in source
    assert "nav.validate(SCREENS)" in source
    assert "nav.DEFAULT_SCREEN" in source
    assert "glossary.nav_label(" in source
    assert "GROUPS: list" not in source, "порядок групп не должен задаваться в app.py повторно"
