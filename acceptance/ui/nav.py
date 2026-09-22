"""Навигация пульта: порядок экранов по бизнес-процессу испытаний (docs/14).

Порядок групп — не оформление, а часть маршрута оператора
(`docs/14. Концепт пульта — целевой бизнес-процесс испытаний.md`, §4):

    * **Подготовка** — стенд и сессия: без них прогон не имеет смысла (снимок «Начало»,
      реквизиты испытаний);
    * **Испытания** — сначала прогон (`checks`) и лента обмена (`monitor`), затем данные
      стенда (`records`, `datasets`) и последним — монитор инфраструктуры `$Задачи`
      (`tasks`): он обслуживает асинхронные проверки, но шагом приёмки не является;
    * **Инструменты** — `console`: свободные пробы и диагностика (FR-T3), а не шаг
      бизнес-процесса приёмки; впереди других разделов она сбивает с маршрута;
    * **Результаты** — замечания, журнал обмена, отчёт испытаний: то, с чем работают
      после прогона.

Модуль содержит только данные и проверки — без Streamlit, поэтому порядок навигации
можно проверять unit-тестом: `acceptance/app.py` импортирует `GROUPS` для боковой панели,
`tests/unit/test_nav.py` фиксирует порядок и целостность.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Экран, на котором открывается пульт (стартовая группа «Подготовка»).
DEFAULT_SCREEN = "stand"

#: Группы навигации: подпись группы → экраны в порядке маршрута оператора.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Подготовка", ("stand", "session")),
    ("Испытания", ("checks", "monitor", "records", "datasets", "tasks")),
    ("Инструменты", ("console",)),
    ("Результаты", ("notes", "logs", "report")),
)


def screens() -> tuple[str, ...]:
    """Ключи экранов в порядке навигации (для сверки с `SCREENS` экранов пульта)."""
    return tuple(key for _, keys in GROUPS for key in keys)


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
    """Проверяет, что навигация перечисляет все экраны ровно один раз.

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
