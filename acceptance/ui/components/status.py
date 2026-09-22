"""Статусы, маркеры и классы проверок: один словарь подписей для всех экранов.

Правило макета (`docs/16` §0): цвета и пиктограммы статусов одинаковы на всех
экранах, а источник значения называется рядом («сервер», «пульт», «оператор»).
Поэтому подписи берутся **из ядра**, а не переписываются в интерфейсе:

* статус проверки и его пиктограмма — `acceptance/checks/registry.py` (`STATUS_ICONS`);
* состояние пункта очереди — `acceptance/queue.py` (`QUEUE_ICONS`);
* маркеры «следующая/выполняется/последняя» — `acceptance/queue.py` (`MARKER_*`);
* происхождение результата — `acceptance/results.py` (`ORIGIN_*`).

Модуль чистый (без Streamlit): подписи проверяются unit-тестом, а экран только
подставляет их в разметку.
"""

from __future__ import annotations

from acceptance.checks.registry import CLASS_LABELS, STATUS_ICONS, CheckClass, CheckStatus
from acceptance.queue import MARKER_LAST, MARKER_NEXT, MARKER_RUNNING, QUEUE_ICONS, QUEUE_STATES
from acceptance.results import ORIGIN_MANUAL, ORIGIN_RUN, ORIGIN_SUSPENDED
from acceptance.session import TestSession

#: Маркеры очереди: как они называются оператору (макет `docs/16` §0).
MARKER_LABELS: dict[str, str] = {
    MARKER_RUNNING: "сейчас",
    MARKER_NEXT: "следующая",
    MARKER_LAST: "последняя",
}

#: Пиктограммы маркеров. Макет печатает «▶ сейчас» и «⚪ следующая/последняя»; чтобы
#: «следующая» и «последняя» различались глазом, у них разные пиктограммы.
MARKER_ICONS: dict[str, str] = {
    MARKER_RUNNING: "▶",
    MARKER_NEXT: "▷",
    MARKER_LAST: "🕘",
}

#: Происхождение результата (`IR-P-7`: рядом со значением видно, кто его дал).
ORIGIN_LABELS: dict[str, str] = {
    ORIGIN_RUN: "пульт",
    ORIGIN_MANUAL: "оператор",
    ORIGIN_SUSPENDED: "оператор",
}

#: Порядок статусов в фильтрах протокола: сначала «плохие».
STATUS_ORDER: tuple[str, ...] = (
    str(CheckStatus.FAILED),
    str(CheckStatus.BLOCKED),
    str(CheckStatus.INTERRUPTED),
    str(CheckStatus.SKIPPED),
    str(CheckStatus.NOT_RUN),
    str(CheckStatus.MANUAL_OK),
    str(CheckStatus.PASSED),
)


def status_icon(status: str) -> str:
    """Пиктограмма статуса проверки (`успех` → ✅)."""
    return STATUS_ICONS.get(str(status), "⚪")


def status_labels() -> tuple[str, ...]:
    """Все статусы проверки в порядке фильтров (для `st.selectbox`)."""
    return STATUS_ORDER


def queue_state_label(state: str) -> str:
    """Подпись состояния пункта очереди («ожидает», «выполняется», …)."""
    text = str(state)
    return text if text in QUEUE_STATES else str(QUEUE_STATES[0])


def queue_state_icon(state: str) -> str:
    """Пиктограмма состояния пункта очереди (`ожидает` → ⚪)."""
    return QUEUE_ICONS.get(str(state), "⚪")


def marker_label(marker: str) -> str:
    """Подпись маркера очереди («следующая», «выполняется», «последняя»); пусто — нет."""
    return MARKER_LABELS.get(str(marker), "")


def marker_icon(marker: str) -> str:
    """Пиктограмма маркера очереди; пусто — нет маркера."""
    return MARKER_ICONS.get(str(marker), "")


def marker_text(marker: str) -> str:
    """«▶ сейчас», «▷ следующая», «🕘 последняя» — или пустая строка."""
    label = marker_label(marker)
    return f"{marker_icon(marker)} {label}".strip() if label else ""


def origin_label(origin: str, *, suspended: bool = False) -> str:
    """Кто дал значение: «пульт» (прогон), «оператор» (отметка или снятие)."""
    if suspended:
        return "оператор"
    return ORIGIN_LABELS.get(str(origin), "")


def class_label(check_class: str | CheckClass) -> str:
    """Человекочитаемый класс проверки (`live` → «живая проверка (реальные данные)»)."""
    key = str(check_class)
    return CLASS_LABELS.get(key, key)


def is_confirmable(check_class: str | CheckClass) -> bool:
    """True, если проверке нужна карточка запуска (`live`/`heavy`, `FR-P-19`)."""
    return str(check_class) in (str(CheckClass.LIVE), str(CheckClass.HEAVY))


def is_manual(check_class: str | CheckClass) -> bool:
    """True, если проверка выполняется вручную (`FR-P-32`)."""
    return str(check_class) == str(CheckClass.MANUAL)


def result_text(row: dict[str, object]) -> str:
    """Строка результата для таблицы: «✅ успех · соответствует ожиданию»."""
    icon = str(row.get("icon") or status_icon(str(row.get("status") or "")))
    status = str(row.get("status") or "")
    verdict = str(row.get("verdict") or "")
    return f"{icon} {status}" + (f" · {verdict}" if verdict else "")


def build_warning(session: TestSession, current_build: str = "") -> str:
    """Предупреждение о расхождении сборок: испытываем не то, что записано в сессии.

    Молчать об этом нельзя (`AC-P-1`): прогон на другой сборке делает результаты
    неприменимыми к объекту испытаний, зафиксированному в реквизитах (`DR-P-1`).
    Пустая строка — расхождения нет или сравнивать не с чем.
    """
    stored = str(getattr(session, "server_build", "") or "").strip()
    actual = str(current_build or "").strip()
    if not stored or not actual or stored == actual:
        return ""
    return (
        f"Сборка стенда сейчас — {actual}, а в сессии зафиксирована {stored}: "
        "проверьте объект испытаний и снимите снимок стенда (`SCR-202`)"
    )
