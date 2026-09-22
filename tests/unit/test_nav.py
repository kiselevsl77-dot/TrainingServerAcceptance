"""Тест порядка навигации пульта (`acceptance/ui/nav.py`).

Замечание заказчика (21.09.2026, п. 2–3): экран «⏱️ `$Задачи`» стоял вторым в группе
«Испытания», хотя это монитор инфраструктуры (обслуживает асинхронные проверки), а не
шаг приёмки; маршрут оператора должен читаться сверху вниз: подготовка → прогон →
данные стенда → инфраструктура → инструменты → результаты
(`docs/14. Концепт пульта — целевой бизнес-процесс испытаний.md`, §4).

Тест — «замок» на этот порядок: до него порядок групп нигде не проверялся, поэтому
следующая правка `GROUPS` молча возвращала бы прежний «хаос» в меню. Дополнительно
проверяется целостность: навигация обязана перечислять все экраны ровно один раз —
это же условие пульт проверяет при старте (`nav.validate` в `acceptance/app.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acceptance.ui import nav

APP_PATH = Path(__file__).resolve().parents[2] / "acceptance" / "app.py"

#: Ожидаемый порядок групп — как в §4 концепта (`docs/14`).
EXPECTED_GROUPS: tuple[str, ...] = ("Подготовка", "Испытания", "Инструменты", "Результаты")


def test_groups_follow_business_process_order() -> None:
    """Группы идут в порядке маршрута оператора: подготовка → … → результаты."""
    assert tuple(label for label, _ in nav.GROUPS) == EXPECTED_GROUPS


def test_preparation_group_opens_the_console() -> None:
    """Прогон начинается с «Подготовки»: стенд и сессия — до остальных разделов."""
    assert nav.group_keys("Подготовка") == ("stand", "session")
    assert nav.DEFAULT_SCREEN == "stand"


def test_trials_start_with_run_screens() -> None:
    """В «Испытаниях» прогон и лента обмена — первыми, данные стенда — после них."""
    assert nav.group_keys("Испытания")[:2] == ("checks", "monitor")
    assert nav.group_keys("Испытания")[2:] == ("records", "datasets", "tasks")


def test_tasks_screen_is_last_in_trials() -> None:
    """`$Задачи` — последний экран группы «Испытания» (замечание п. 2)."""
    assert nav.group_keys("Испытания")[-1] == "tasks"
    assert nav.group_of("tasks") == "Испытания"


def test_console_is_tool_not_trial_step() -> None:
    """Консоль запросов — инструмент диагностики (FR-T3), а не шаг приёмки."""
    assert nav.group_keys("Инструменты") == ("console",)
    assert nav.group_of("console") == "Инструменты"


def test_results_group_is_unchanged() -> None:
    """«Результаты» замыкают маршрут: замечания, журнал обмена, отчёт."""
    assert nav.group_keys("Результаты") == ("notes", "logs", "report")


def test_group_key_of_unknown_screen_raises() -> None:
    """Экран, которого нет в навигации, — понятная ошибка, а не пустая строка."""
    with pytest.raises(KeyError):
        nav.group_of("__unknown__")
    with pytest.raises(KeyError):
        nav.group_keys("__Без такой группы__")


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
    """Кнопка навигации без экрана — тоже ошибка: иначе панель падает при нажатии."""
    without_report = tuple(key for key in nav.screens() if key != "report")

    with pytest.raises(ValueError, match="неизвестные экраны: report"):
        nav.validate(without_report)


def test_app_uses_navigation_module() -> None:
    """`app.py` берёт порядок и стартовый экран из `nav`, а не задаёт их у себя."""
    source = APP_PATH.read_text(encoding="utf-8")

    assert "nav.GROUPS" in source
    assert "nav.validate(SCREENS)" in source
    assert "nav.DEFAULT_SCREEN" in source
    assert "GROUPS: list" not in source, "порядок групп не должен задаваться в app.py повторно"
