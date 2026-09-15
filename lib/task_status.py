"""Маппинг `TaskStatus` в визуальные индикаторы и доступные действия.

Реализует сквозную механику NFR-6 и опирается на автомат состояний FSM-1:
    * `running`  -> `pause` | `interrupt`
    * `paused`   -> `resume` | `interrupt`
    * терминальные состояния — без действий.
"""

from __future__ import annotations

from dataclasses import dataclass

from client.schemas import TaskStatus


@dataclass(frozen=True)
class StatusPresentation:
    """Визуальное представление состояния задачи."""

    label: str
    indicator: str
    is_terminal: bool
    actions: tuple[str, ...]


_STATUS_META: dict[TaskStatus, StatusPresentation] = {
    TaskStatus.NEW: StatusPresentation("Новая", "⚪", False, ()),
    TaskStatus.RUNNING: StatusPresentation("Выполняется", "🟢", False, ("pause", "interrupt")),
    TaskStatus.PAUSING: StatusPresentation("Приостанавливается", "🟡", False, ()),
    TaskStatus.PAUSED: StatusPresentation("Приостановлена", "🟡", False, ("resume", "interrupt")),
    TaskStatus.COMPLETED: StatusPresentation("Завершена", "✅", True, ()),
    TaskStatus.FAILED: StatusPresentation("Ошибка", "❌", True, ()),
    TaskStatus.INTERRUPTED: StatusPresentation("Прервана", "⛔", True, ()),
    TaskStatus.NOT_FOUND: StatusPresentation("Не найдена", "❓", True, ()),
}


def get_status_presentation(status: TaskStatus) -> StatusPresentation:
    """Возвращает представление статуса (label/indicator/actions)."""
    return _STATUS_META[status]


def is_terminal(status: TaskStatus) -> bool:
    """True, если задача находится в терминальном состоянии."""
    return _STATUS_META[status].is_terminal


def available_actions(status: TaskStatus) -> tuple[str, ...]:
    """Доступные для статуса действия (pause/resume/interrupt)."""
    return _STATUS_META[status].actions
