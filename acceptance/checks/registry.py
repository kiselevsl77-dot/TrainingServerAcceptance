"""Модели чек-листа испытаний: описание проверки и её результат (FR-T4).

Проверка (`CheckSpec`) — это сценарий из программы испытаний: идентификатор
`TC-<модуль>-<NN>`, трассировка на требования (`UC`/`FR`/`BR`/`FSM`), класс
(`tech` — безопасная, `live` — боевая на реальных данных, `heavy` —
ресурсоёмкая, `manual` — только вручную), шаги, ожидаемый результат и признак
зависимости от известного дефекта API.

Результат (`CheckResult`) — факт выполнения: статус, вердикт, заключение
оператора, доказательства, параметры запуска, время и диапазон номеров журнала
обмена с сервером (по ним проверка воспроизводима по JSONL сессии).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CheckClass(StrEnum):
    """Класс проверки (влияет на требования к запуску)."""

    TECH = "tech"
    LIVE = "live"
    HEAVY = "heavy"
    MANUAL = "manual"


class CheckStatus(StrEnum):
    """Статус выполнения проверки."""

    NOT_RUN = "не выполнена"
    PASSED = "успех"
    FAILED = "отказ"
    BLOCKED = "блокировано API"
    SKIPPED = "пропущена"
    MANUAL_OK = "выполнена вручную"
    INTERRUPTED = "прервана"


CLASS_LABELS: dict[str, str] = {
    CheckClass.TECH: "техническая (безопасная)",
    CheckClass.LIVE: "боевая (реальные данные)",
    CheckClass.HEAVY: "ресурсоёмкая",
    CheckClass.MANUAL: "ручная",
}

STATUS_ICONS: dict[str, str] = {
    CheckStatus.NOT_RUN: "⚪",
    CheckStatus.PASSED: "✅",
    CheckStatus.FAILED: "❌",
    CheckStatus.BLOCKED: "🚫",
    CheckStatus.SKIPPED: "⏭️",
    CheckStatus.MANUAL_OK: "🖐️",
    CheckStatus.INTERRUPTED: "⛔",
}

#: Статусы, считающиеся «выполненными» (входят в итог испытаний).
DONE_STATUSES = (
    CheckStatus.PASSED,
    CheckStatus.FAILED,
    CheckStatus.BLOCKED,
    CheckStatus.MANUAL_OK,
    CheckStatus.INTERRUPTED,
)

CONFIRMABLE_CLASSES = (CheckClass.HEAVY, CheckClass.LIVE)


@dataclass(frozen=True)
class CheckSpec:
    """Описание проверки из программы испытаний."""

    check_id: str
    title: str
    module: str
    requirement: str
    check_class: CheckClass = CheckClass.TECH
    steps: tuple[str, ...] = ()
    expected: str = ""
    endpoints: tuple[str, ...] = ()
    blocked_by_api: str | None = None
    automation: str | None = None

    @property
    def is_confirmation_required(self) -> bool:
        """True, если перед запуском требуется подтверждение оператора."""
        return self.check_class in CONFIRMABLE_CLASSES

    @property
    def class_label(self) -> str:
        """Человекочитаемый класс проверки."""
        return CLASS_LABELS.get(self.check_class, str(self.check_class))


@dataclass
class CheckResult:
    """Результат выполнения проверки (сохраняется в сессии)."""

    check_id: str
    status: CheckStatus = CheckStatus.NOT_RUN
    verdict: str = ""
    operator_note: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float | None = None
    journal_from: int | None = None
    journal_to: int | None = None

    @property
    def is_done(self) -> bool:
        """True, если проверка выполнена (любой терминальный статус)."""
        return self.status in DONE_STATUSES

    @property
    def icon(self) -> str:
        """Индикатор статуса для интерфейса."""
        return STATUS_ICONS.get(self.status, "⚪")

    def to_dict(self) -> dict[str, Any]:
        """Сериализует результат (статус — строкой)."""
        payload = asdict(self)
        payload["status"] = str(self.status)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckResult:
        """Восстанавливает результат из JSON."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        raw_status = payload.get("status", CheckStatus.NOT_RUN)
        payload["status"] = _coerce_status(raw_status)
        payload.setdefault("check_id", "TC-?-00")
        return cls(**payload)


def _coerce_status(value: Any) -> CheckStatus:
    """Приводит сохранённый статус к `CheckStatus` (терпимо к неизвестным)."""
    if isinstance(value, CheckStatus):
        return value
    text = str(value)
    for status in CheckStatus:
        if status.value == text or status.name == text:
            return status
    return CheckStatus.NOT_RUN


def new_result(spec: CheckSpec) -> CheckResult:
    """Создаёт пустой результат для проверки."""
    return CheckResult(check_id=spec.check_id)


def summarize(results: list[dict[str, Any] | CheckResult]) -> dict[str, Any]:
    """Сводка по результатам проверок (для KPI и отчёта)."""
    restored = [
        item if isinstance(item, CheckResult) else CheckResult.from_dict(item) for item in results
    ]
    counts: dict[str, int] = {str(status): 0 for status in CheckStatus}
    for result in restored:
        counts[str(result.status)] = counts.get(str(result.status), 0) + 1

    return {
        "total": len(restored),
        "done": sum(1 for result in restored if result.is_done),
        "counts": counts,
        "failed": counts.get(str(CheckStatus.FAILED), 0),
        "blocked": counts.get(str(CheckStatus.BLOCKED), 0),
        "passed": counts.get(str(CheckStatus.PASSED), 0),
        "not_run": counts.get(str(CheckStatus.NOT_RUN), 0),
    }
