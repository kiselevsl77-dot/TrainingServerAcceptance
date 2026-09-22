"""Навигация пульта: 15 целевых экранов в пяти группах (`IR-P-1`, макет `docs/16`).

Код экрана — `SCR-<раздел><порядок>` (§1.5 ТЗ, версия 1.2): раздел кодируется сотнями в
порядке навигации (1xx — планирование, 2xx — подготовка, 3xx — испытания, 4xx — результаты,
5xx — инструменты), поэтому коды идут по возрастанию и совпадают с порядком меню.

Модуль содержит только данные и проверки — без Streamlit. Поэтому:

    * состав, порядок групп и целостность реестра проверяет `tests/unit/test_nav.py`;
    * совпадение кодов с ТЗ (§6.1), макетом (`docs/16`) и прототипом (`mockup/app.js`) —
      `tests/unit/test_target_docs.py` (этап 4 расширит это до трассировки «экран ↔ требование»).

Соглашение об именах: **ключ** экрана (`scr101_sets`) — внутреннее имя маршрута
(`st.session_state["pult_screen"]`), **код** (`SCR-101`) — идентификатор для документации,
протокола и отчёта. Файл экрана называется по ключу: `acceptance/ui/screens/scr101_sets.py`,
подписи — в глоссарии (`acceptance/glossary.py`), чтобы экраны назывались одинаково в коде,
документации и отчёте.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: Подписи групп навигации (порядок процесса — `IR-P-1`, макет `docs/16` §0).
GROUP_PLANNING = "Планирование испытаний"
GROUP_PREPARATION = "Подготовка"
GROUP_TRIALS = "Испытания"
GROUP_RESULTS = "Результаты"
GROUP_TOOLS = "Инструменты"


@dataclass(frozen=True)
class Screen:
    """Экран пульта: код для документов, ключ маршрута, группа навигации и фаза процесса."""

    code: str
    key: str
    group: str
    phase: str


#: Экраны в порядке навигации. Порядок — часть бизнес-процесса испытаний (`docs/14` §4):
#: планирование → подготовка → прогон → разбор → финальный отчёт → инструменты.
SCREENS: tuple[Screen, ...] = (
    Screen("SCR-101", "scr101_sets", GROUP_PLANNING, "Ф0"),
    Screen("SCR-102", "scr102_programme", GROUP_PLANNING, "Ф0, Ф3"),
    Screen("SCR-201", "scr201_overview", GROUP_PREPARATION, "Ф0–Ф4"),
    Screen("SCR-202", "scr202_stand", GROUP_PREPARATION, "Ф0, Ф4"),
    Screen("SCR-203", "scr203_session", GROUP_PREPARATION, "Ф0, Ф4"),
    Screen("SCR-204", "scr204_data", GROUP_PREPARATION, "Ф0"),
    Screen("SCR-301", "scr301_run", GROUP_TRIALS, "Ф1–Ф2"),
    Screen("SCR-302", "scr302_check", GROUP_TRIALS, "Ф1–Ф2"),
    Screen("SCR-303", "scr303_tasks", GROUP_TRIALS, "Ф1–Ф2"),
    Screen("SCR-401", "scr401_protocol", GROUP_RESULTS, "Ф2"),
    Screen("SCR-402", "scr402_journal", GROUP_RESULTS, "Ф1–Ф4"),
    Screen("SCR-403", "scr403_notes", GROUP_RESULTS, "Ф2–Ф3"),
    Screen("SCR-404", "scr404_report", GROUP_RESULTS, "Ф4"),
    Screen("SCR-405", "scr405_compare", GROUP_RESULTS, "Ф3–Ф4"),
    Screen("SCR-501", "scr501_tools", GROUP_TOOLS, "вне процесса"),
)

#: Экран, на котором открывается пульт. Решение заказчика 22.09.2026: «на первое время»
#: первым идёт «Наборы проверок» (первый экран первой группы); когда центральной ролью
#: станет инженер-испытатель, стартовым станет `SCR-201` «Обзор испытаний» — это одна
#: строка здесь, порядок меню при этом не меняется.
DEFAULT_SCREEN = "scr101_sets"


def _build_groups() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Группы навигации из порядка экранов (единственный источник — `SCREENS`)."""
    groups: list[tuple[str, tuple[str, ...]]] = []
    for screen in SCREENS:
        if groups and groups[-1][0] == screen.group:
            groups[-1] = (screen.group, (*groups[-1][1], screen.key))
            continue
        groups.append((screen.group, (screen.key,)))
    return tuple(groups)


#: Группы навигации: подпись группы → экраны в порядке маршрута оператора.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = _build_groups()


def screens() -> tuple[str, ...]:
    """Ключи экранов в порядке навигации (для сверки с реестром экранов пульта)."""
    return tuple(screen.key for screen in SCREENS)


def codes() -> tuple[str, ...]:
    """Коды экранов (`SCR-…`) в порядке навигации."""
    return tuple(screen.code for screen in SCREENS)


def by_key(key: str) -> Screen:
    """Экран по ключу маршрута (`KeyError` — если экрана нет в реестре)."""
    for screen in SCREENS:
        if screen.key == key:
            return screen
    raise KeyError(key)


def by_code(code: str) -> Screen:
    """Экран по коду из документов (`SCR-301` → «Прогон»; `KeyError`, если кода нет)."""
    for screen in SCREENS:
        if screen.code == code:
            return screen
    raise KeyError(code)


def group_of(key: str) -> str:
    """Подпись группы, в которой стоит экран (`KeyError`, если экрана в навигации нет)."""
    for label, keys in GROUPS:
        if key in keys:
            return label
    raise KeyError(key)


def group_keys(label: str) -> tuple[str, ...]:
    """Экраны группы по её подписи (`KeyError`, если такой группы нет)."""
    for known, keys in GROUPS:
        if known == label:
            return keys
    raise KeyError(label)


def validate(screen_keys: Iterable[str]) -> None:
    """Проверяет, что навигация перечисляет все экраны пульта ровно один раз.

    Вызывается при старте пульта (`acceptance/app.py`): новый экран, забытый в навигации,
    сразу даёт понятную ошибку, а не «молча невидимую» страницу.

    Raises:
        ValueError: экран пропущен, назван дважды или отсутствует в разметке экранов.
    """
    known = set(screen_keys)
    listed = list(screens())
    duplicates = sorted({key for key in listed if listed.count(key) > 1})
    if duplicates:
        raise ValueError("экраны перечислены в навигации дважды: " + ", ".join(duplicates))
    missing = sorted(known - set(listed))
    if missing:
        raise ValueError("экраны отсутствуют в навигации: " + ", ".join(missing))
    unknown = sorted(set(listed) - known)
    if unknown:
        raise ValueError("навигация ссылается на неизвестные экраны: " + ", ".join(unknown))
