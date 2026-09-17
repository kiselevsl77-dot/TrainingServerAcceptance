"""Маппинг `TaskStatus` в визуальные индикаторы и доступные действия.

Реализует сквозную механику NFR-6 и опирается на автомат состояний FSM-1:
    * `running`  -> `pause` | `interrupt`
    * `paused`   -> `resume` | `interrupt`
    * терминальные состояния — без действий.

Для таблиц есть компактные пиктограммы (`glyph`) и цвет (`color`): список задач
должен читаться «с одного взгляда» — исполняется задача или нет, чем закончилась.
Раскраска применяется через `pandas.Styler` в `st.dataframe` (эмодзи-индикаторы
`indicator` остаются для сообщений, артефактов и отчёта).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from client.schemas import TaskStatus


@dataclass(frozen=True)
class StatusPresentation:
    """Визуальное представление состояния задачи.

    Attributes:
        label: подпись для интерфейса и отчёта («Выполняется»).
        indicator: эмодзи-индикатор для сообщений и артефактов («🟢»).
        glyph: компактная пиктограмма для таблиц («▶», «✔», «✖»).
        color: цвет пиктограммы в таблице (CSS-имя: green/yellow/red/grey).
        is_terminal: True, если задача больше не изменится.
        actions: команды FSM-1, допустимые из этого состояния.
    """

    label: str
    indicator: str
    glyph: str
    color: str
    is_terminal: bool
    actions: tuple[str, ...]


_STATUS_META: dict[TaskStatus, StatusPresentation] = {
    TaskStatus.NEW: StatusPresentation("Новая", "⚪", "○", "grey", False, ()),
    TaskStatus.RUNNING: StatusPresentation(
        "Выполняется", "🟢", "▶", "green", False, ("pause", "interrupt")
    ),
    TaskStatus.PAUSING: StatusPresentation("Приостанавливается", "🟡", "⏳", "yellow", False, ()),
    TaskStatus.PAUSED: StatusPresentation(
        "Приостановлена", "🟡", "⏸", "yellow", False, ("resume", "interrupt")
    ),
    TaskStatus.COMPLETED: StatusPresentation("Завершена", "✅", "✔", "green", True, ()),
    TaskStatus.FAILED: StatusPresentation("Ошибка", "❌", "✖", "red", True, ()),
    TaskStatus.INTERRUPTED: StatusPresentation("Прервана", "⛔", "⏹", "orange", True, ()),
    TaskStatus.NOT_FOUND: StatusPresentation("Не найдена", "❓", "?", "grey", True, ()),
}

#: Представление неизвестного или ещё не запрошенного статуса.
UNKNOWN_STATUS = StatusPresentation("Неизвестен", "⚪", "?", "grey", False, ())


def get_status_presentation(status: TaskStatus) -> StatusPresentation:
    """Возвращает представление статуса (label/indicator/glyph/color/actions)."""
    return _STATUS_META[status]


def presentation_for(value: Any) -> StatusPresentation:
    """Представление по значению статуса; неизвестное значение — `UNKNOWN_STATUS`."""
    if isinstance(value, TaskStatus):
        return _STATUS_META[value]
    try:
        return _STATUS_META[TaskStatus(str(value or ""))]
    except ValueError:
        return UNKNOWN_STATUS


def is_terminal(status: TaskStatus) -> bool:
    """True, если задача находится в терминальном состоянии."""
    return _STATUS_META[status].is_terminal


def available_actions(status: TaskStatus) -> tuple[str, ...]:
    """Доступные для статуса действия (pause/resume/interrupt)."""
    return _STATUS_META[status].actions
