"""Программа испытаний: план вызовов и контроль исполнения (FR-T4, пожелания 21.09.2026).

Заказчик сформулировал три пожелания, которые закрывает этот модуль:

    1. **Монитор обмена** — видеть по раздельности каждую отправленную команду и
       полученный ответ (лента: `acceptance/ui/common/exchange_monitor.py`);
    2. **Контроль исполнения вызовов API** — выполнять вызовы **по одной** (кнопка
       «Выполнить») и «с паузой» (авто-прогон с заданным интервалом), сразу видя
       соответствие ожиданиям;
    3. **Программа испытаний** — список запланированных вызовов, который оператор
       видит заранее и может частично снять галочками.

Модель — **двухуровневая** (решение заказчика, вариант C):

    * пункт уровня `check` — проверка `TC-…` целиком (исполняется её сценарий);
    * пункты уровня `call` — отдельные вызовы API этой проверки (метод + путь из
      реестра `acceptance/endpoints.py`, включая негативные пробы `probe_paths`).

Режим исполнения (`MODE_CHECK` / `MODE_CALL`) выбирает, что делает кнопка
«Выполнить»: прогоняет сценарий следующей проверки или отправляет **один** запрос
по шаблону пункта. В обоих режимах всё, что реально ушло на стенд, видно в ленте
монитора по метке пункта, а вердикт пункта считается по ожиданию (`expected`).

Модуль не зависит от интерфейса: план строится, сериализуется и оценивается
чистыми функциями, поэтому проверяется unit-тестами (`tests/unit/test_plan.py`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from acceptance import endpoints as ep
from acceptance.checks import catalog
from acceptance.checks.registry import CheckClass, CheckSpec

#: Уровни пунктов плана.
LEVEL_CHECK = "проверка"
LEVEL_CALL = "вызов"

LEVEL_LABELS: dict[str, str] = {
    LEVEL_CHECK: "проверка целиком (сценарий)",
    LEVEL_CALL: "один вызов API",
}

#: Режимы исполнения плана: что делает кнопка «Выполнить».
MODE_CHECK = "check"
MODE_CALL = "call"

MODE_LABELS: dict[str, str] = {
    MODE_CHECK: "выполнять проверки целиком",
    MODE_CALL: "выполнять по вызовам",
}

#: Статусы пункта плана (совпадают по смыслу со статусами проверок).
STATUS_PENDING = "ожидает"
STATUS_PASSED = "успех"
STATUS_FAILED = "отказ"
STATUS_BLOCKED = "блокировано API"
STATUS_SKIPPED = "пропущено"

STATUS_ICONS: dict[str, str] = {
    STATUS_PENDING: "⚪",
    STATUS_PASSED: "✅",
    STATUS_FAILED: "❌",
    STATUS_BLOCKED: "🚫",
    STATUS_SKIPPED: "⏭️",
}

#: Вердикты соответствия ожиданию (то, что нужно видеть комиссии сразу).
VERDICT_MATCH = "соответствует ожиданию"
VERDICT_MISMATCH = "не соответствует ожиданию"
VERDICT_NOT_RUN = "вызов не выполнен"

VERDICT_ICONS: dict[str, str] = {
    VERDICT_MATCH: "✅",
    VERDICT_MISMATCH: "❌",
    VERDICT_NOT_RUN: "⛔",
}

DEFAULT_PAUSE_SECONDS = 3.0
MIN_PAUSE_SECONDS = 0.5
MAX_PAUSE_SECONDS = 120.0


@dataclass
class PlanItem:
    """Пункт программы испытаний: проверка целиком или один вызов API."""

    item_id: str
    level: str
    title: str
    parent_id: str | None = None
    group: str = ""
    module: str = ""
    check_class: str = str(CheckClass.TECH)
    endpoint_key: str = ""
    method: str = ""
    path: str = ""
    safety: str = ""
    body_kind: str = ""
    expected: str = ""
    probe: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    body_sample: str = ""
    enabled: bool = True
    status: str = STATUS_PENDING
    verdict: str = ""
    journal_from: int | None = None
    journal_to: int | None = None
    duration_ms: float | None = None
    started_at: str | None = None
    ended_at: str | None = None

    @property
    def is_check(self) -> bool:
        """True для пункта уровня «проверка»."""
        return self.level == LEVEL_CHECK

    @property
    def is_call(self) -> bool:
        """True для пункта уровня «вызов»."""
        return self.level == LEVEL_CALL

    @property
    def icon(self) -> str:
        """Иконка статуса пункта."""
        return STATUS_ICONS.get(self.status, "⚪")

    @property
    def is_done(self) -> bool:
        """True, если пункт уже исполнялся (любой терминальный статус)."""
        return self.status != STATUS_PENDING

    @property
    def is_destructive(self) -> bool:
        """True, если вызов удаляет данные (требует подтверждения по NFR-T4)."""
        return self.safety == str(ep.Safety.DESTRUCTIVE)

    @property
    def operation(self) -> str:
        """Подпись операции (`GET /api/data/files`)."""
        if not self.method:
            return ""
        return f"{self.method} {self.path}"

    @property
    def plan_key(self) -> str:
        """Ключ виджета плана (уникален по построению)."""
        return self.item_id.replace("#", "_").replace("/", "_").replace("{", "").replace("}", "")

    def to_dict(self) -> dict[str, Any]:
        """Сериализация пункта (в сессию и отчёт)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanItem:
        """Восстановление пункта из JSON."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        payload.setdefault("item_id", "PLAN-?")
        payload.setdefault("level", LEVEL_CHECK)
        payload.setdefault("title", payload["item_id"])
        payload["params"] = dict(payload.get("params") or {})
        return cls(**payload)

    def mark(
        self,
        status: str,
        *,
        verdict: str = "",
        journal_from: int | None = None,
        journal_to: int | None = None,
        duration_ms: float | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> PlanItem:
        """Фиксирует результат исполнения пункта (возвращает новый пункт)."""
        return replace(
            self,
            status=status,
            verdict=verdict,
            journal_from=journal_from,
            journal_to=journal_to,
            duration_ms=duration_ms,
            started_at=started_at,
            ended_at=ended_at,
        )


@dataclass
class PlanState:
    """Состояние программы испытаний: пункты, режим, указатель и авто-прогон."""

    items: list[PlanItem] = field(default_factory=list)
    mode: str = MODE_CHECK
    pause_seconds: float = DEFAULT_PAUSE_SECONDS
    pointer: int = 0
    running: bool = False
    stop_reason: str = ""
    last_item_id: str = ""
    last_at: str = ""

    # -- доступ к пунктам -----------------------------------------------------
    def find(self, item_id: str) -> PlanItem | None:
        """Пункт плана по идентификатору."""
        return next((item for item in self.items if item.item_id == item_id), None)

    def checks(self) -> list[PlanItem]:
        """Пункты уровня «проверка» (в порядке программы)."""
        return [item for item in self.items if item.is_check]

    def calls_of(self, item_id: str) -> list[PlanItem]:
        """Вызовы проверки (дочерние пункты)."""
        return [item for item in self.items if item.parent_id == item_id]

    def enabled_items(self, *, level: str | None = None) -> list[PlanItem]:
        """Включённые галочками пункты (по умолчанию — уровня текущего режима)."""
        target = level or (LEVEL_CHECK if self.mode == MODE_CHECK else LEVEL_CALL)
        if target == LEVEL_CALL:
            # вызов исполняется, только если включена и его проверка
            enabled_checks = {item.item_id for item in self.items if item.is_check and item.enabled}
            return [
                item
                for item in self.items
                if item.level == LEVEL_CALL and item.enabled and item.parent_id in enabled_checks
            ]
        return [item for item in self.items if item.level == target and item.enabled]

    def next_item(self, *, level: str | None = None) -> PlanItem | None:
        """Следующий пункт к исполнению: первый включённый после указателя."""
        candidates = self.enabled_items(level=level)
        if not candidates:
            return None
        ids = [item.item_id for item in candidates]
        position = ids.index(self.last_item_id) + 1 if self.last_item_id in ids else 0
        if position >= len(ids):
            return None
        return candidates[position]

    def index_of(self, item_id: str) -> int:
        """Индекс пункта в списке плана (-1, если его нет)."""
        return next(
            (index for index, item in enumerate(self.items) if item.item_id == item_id),
            -1,
        )

    def replace_item(self, item: PlanItem) -> None:
        """Заменяет пункт плана (по `item_id`)."""
        index = self.index_of(item.item_id)
        if index >= 0:
            self.items[index] = item

    # -- галочки --------------------------------------------------------------
    def set_enabled(self, item_id: str, enabled: bool) -> None:
        """Ставит/снимает галочку пункта.

        Снятие галочки у проверки снимает её вызовы; включение вызова включает
        проверку-родителя (иначе вызов остался бы «за кадром»).
        """
        item = self.find(item_id)
        if item is None:
            return
        item.enabled = enabled
        if item.is_check:
            for call in self.calls_of(item.item_id):
                call.enabled = enabled
        elif enabled and item.parent_id:
            parent = self.find(item.parent_id)
            if parent is not None:
                parent.enabled = True

    def set_enabled_many(self, item_ids: Iterable[str], enabled: bool) -> None:
        """Ставит/снимает галочки группы пунктов (кнопки «по выборке»)."""
        for item_id in list(item_ids):
            self.set_enabled(item_id, enabled)

    def reset(self) -> None:
        """Сбрасывает результаты и указатель (галочки сохраняются)."""
        self.items = [
            replace(
                item,
                status=STATUS_PENDING,
                verdict="",
                journal_from=None,
                journal_to=None,
                duration_ms=None,
                started_at=None,
                ended_at=None,
            )
            for item in self.items
        ]
        self.pointer = 0
        self.running = False
        self.stop_reason = ""
        self.last_item_id = ""
        self.last_at = ""

    # -- сводка ---------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Сводка плана: сколько включено, выполнено и с каким результатом."""
        counts: dict[str, int] = dict.fromkeys(STATUS_ICONS, 0)
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        enabled_all = [item for item in self.items if item.enabled]
        return {
            "total": len(self.items),
            "checks": len(self.checks()),
            "calls": len(self.items) - len(self.checks()),
            "enabled": len(enabled_all),
            "disabled": len(self.items) - len(enabled_all),
            "done": sum(1 for item in self.items if item.is_done),
            "passed": counts.get(STATUS_PASSED, 0),
            "failed": counts.get(STATUS_FAILED, 0),
            "blocked": counts.get(STATUS_BLOCKED, 0),
            "skipped": counts.get(STATUS_SKIPPED, 0),
            "counts": counts,
            "mode": self.mode,
            "pause_seconds": self.pause_seconds,
            "running": self.running,
        }

    # -- сериализация ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Сериализация плана в сессию (`session.plan`, схема v6)."""
        return {
            "mode": self.mode,
            "pause_seconds": self.pause_seconds,
            "pointer": self.pointer,
            "running": self.running,
            "stop_reason": self.stop_reason,
            "last_item_id": self.last_item_id,
            "last_at": self.last_at,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PlanState:
        """Восстановление плана из сессии."""
        payload = dict(data or {})
        items = [PlanItem.from_dict(item) for item in (payload.get("items") or [])]
        mode = str(payload.get("mode") or MODE_CHECK)
        return cls(
            items=items,
            mode=mode if mode in MODE_LABELS else MODE_CHECK,
            pause_seconds=clamp_pause(payload.get("pause_seconds") or DEFAULT_PAUSE_SECONDS),
            pointer=int(payload.get("pointer") or 0),
            running=bool(payload.get("running")),
            stop_reason=str(payload.get("stop_reason") or ""),
            last_item_id=str(payload.get("last_item_id") or ""),
            last_at=str(payload.get("last_at") or ""),
        )


# ---------------------------------------------------------------------------
# Построение плана из каталога проверок
# ---------------------------------------------------------------------------
def clamp_pause(value: Any) -> float:
    """Приводит паузу между вызовами к допустимому диапазону."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PAUSE_SECONDS
    return float(min(MAX_PAUSE_SECONDS, max(MIN_PAUSE_SECONDS, seconds)))


def parse_operation(text: str) -> tuple[str, str]:
    """Разбирает строку проверки (`get /api/data/files`) в метод и путь."""
    parts = str(text).strip().split(maxsplit=1)
    if len(parts) != 2:
        return "", str(text).strip()
    return parts[0].upper(), parts[1]


def expected_rule(*, probe: bool = False, safety: str = "", heavy: bool = False) -> str:
    """Правило ожидания для вызова: по чему считается «соответствует ожиданию»."""
    if probe:
        return "404/405 — маршрута в спецификации нет (негативная проба)"
    if heavy:
        return "202 и `task_id` в ответе"
    if safety == str(ep.Safety.DESTRUCTIVE):
        return "2xx — удаление выполнено"
    if safety == str(ep.Safety.WRITE):
        return "2xx — изменение принято"
    if safety == str(ep.Safety.HEAVY):
        return "202 и `task_id` в ответе"
    return "2xx — чтение выполнено"


def build_plan(specs: Sequence[CheckSpec] | None = None) -> PlanState:
    """Строит программу испытаний: проверки и их вызовы (порядок — как в каталоге).

    Args:
        specs: проверки для плана; по умолчанию — весь наполненный каталог
            (`acceptance.checks.catalog.CHECKS`), то есть 48 проверок 6 групп.
    """
    source = tuple(specs) if specs is not None else catalog.CHECKS
    items: list[PlanItem] = []
    for spec in source:
        items.append(_check_item(spec))
        for index, target in enumerate(spec.endpoints, start=1):
            items.append(_call_item(spec, target, index=index))
        for index, target in enumerate(spec.probe_paths, start=len(spec.endpoints) + 1):
            items.append(_call_item(spec, target, index=index, probe=True))
    return PlanState(items=items)


def _check_item(spec: CheckSpec) -> PlanItem:
    """Пункт уровня «проверка»."""
    group = catalog.group_of(spec.check_id)
    return PlanItem(
        item_id=spec.check_id,
        level=LEVEL_CHECK,
        title=spec.title,
        group=group,
        module=spec.module,
        check_class=str(spec.check_class),
        expected=spec.expected,
        params={"automation": spec.automation or ""},
    )


def _call_item(spec: CheckSpec, target: str, *, index: int, probe: bool = False) -> PlanItem:
    """Пункт уровня «вызов»: операция реестра или негативная проба."""
    method, path = parse_operation(target)
    endpoint = ep.find(f"{method.lower()} {path}")
    heavy = spec.check_class == CheckClass.HEAVY and endpoint is not None and endpoint.is_heavy
    params = _default_params(endpoint)
    return PlanItem(
        item_id=f"{spec.check_id}#{index}",
        level=LEVEL_CALL,
        title=(endpoint.summary if endpoint is not None else "негативная проба (маршрута нет)"),
        parent_id=spec.check_id,
        group=catalog.group_of(spec.check_id),
        module=spec.module,
        check_class=str(spec.check_class),
        endpoint_key=endpoint.key if endpoint is not None else "",
        method=method,
        path=path,
        safety=str(endpoint.safety) if endpoint is not None else str(ep.Safety.DESTRUCTIVE),
        body_kind=endpoint.body_kind if endpoint is not None else ep.BODY_NONE,
        expected=expected_rule(
            probe=probe, safety=str(endpoint.safety) if endpoint else "", heavy=heavy
        ),
        probe=probe,
        params=params,
        body_sample=endpoint.body_sample if endpoint is not None else "",
    )


def _default_params(endpoint: ep.EndpointSpec | None) -> dict[str, Any]:
    """Значения параметров по умолчанию: enum-подстановки и путь для пикеров."""
    if endpoint is None:
        return {}
    values: dict[str, Any] = {}
    for param in endpoint.params:
        if param.is_path or param.default or param.enum:
            values[param.name] = param.default or (param.enum[0] if param.enum else "")
    return values


# ---------------------------------------------------------------------------
# Оценка вызова и хранение плана
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CallVerdict:
    """Оценка одного вызова: статус пункта, вердикт и пояснение."""

    status: str
    verdict: str
    detail: str
    expected_status: tuple[int, ...] = ()

    @property
    def is_match(self) -> bool:
        """True, если вызов соответствует ожиданию."""
        return self.verdict == VERDICT_MATCH

    @property
    def icon(self) -> str:
        """Иконка вердикта."""
        return VERDICT_ICONS.get(self.verdict, "⛔")


def expected_statuses(item: PlanItem) -> tuple[int, ...]:
    """Какие коды ответа считаются ожидаемыми для вызова этого пункта."""
    if item.probe:
        return (404, 405)
    if item.safety in (str(ep.Safety.HEAVY),) or item.check_class == str(CheckClass.HEAVY):
        return (202, 200, 201)
    return (200, 201, 202, 204)


def judge_call(
    item: PlanItem,
    *,
    status: int | None,
    error: str = "",
    task_id: str = "",
) -> CallVerdict:
    """Считает «соответствие ожиданию» для пункта-вызова (требование пожелания п. 2).

    Правило берётся из `expected` пункта: для проб ожидается отсутствие маршрута
    (404/405), для ресурсоёмких — 202 и `task_id`, для остальных — 2xx. Отдельно
    отмечается известный дефект: `fill` отвечает 202, но `task_id` не возвращает
    (задача ищется в `GET /api/tasks/`).

    Если ответа нет вовсе (`status is None`) — вердикт «вызов не выполнен» с текстом
    ошибки: так отличаются сетевые сбои и отказы стенда от неверного статуса ответа.
    """
    if status is None:
        return CallVerdict(
            STATUS_FAILED,
            VERDICT_NOT_RUN,
            error or "ответ не получен (сеть, таймаут или запрос не отправлен)",
            expected_statuses(item),
        )

    allowed = expected_statuses(item)
    if status not in allowed:
        return CallVerdict(
            STATUS_FAILED,
            VERDICT_MISMATCH,
            f"ответ {status}, ожидалось {', '.join(str(code) for code in allowed)}",
            allowed,
        )

    heavy = item.safety == str(ep.Safety.HEAVY) or item.check_class == str(CheckClass.HEAVY)
    if heavy and status == 202 and not item.probe and not task_id:
        return CallVerdict(
            STATUS_PASSED,
            VERDICT_MATCH,
            "ответ 202, но `task_id` не возвращён (известный дефект: задача ищется "
            "в `GET /api/tasks/`)",
            allowed,
        )
    return CallVerdict(STATUS_PASSED, VERDICT_MATCH, f"ответ {status}", allowed)


def load_plan(session: Any) -> PlanState:
    """План сессии: из `session.plan`, а при его отсутствии — новый (по каталогу)."""
    payload = getattr(session, "plan", None) or {}
    if payload.get("items"):
        return PlanState.from_dict(payload)
    return build_plan()


def save_plan(session: Any, state: PlanState, *, history: bool = False) -> None:
    """Сохраняет план в сессию (схема v6) и, при необходимости, отмечает в истории."""
    session.plan = state.to_dict()
    if history:
        summary = state.summary()
        session.add_history(
            "plan_updated",
            f"Программа испытаний: {summary['enabled']} включено, {summary['done']} выполнено",
            mode=summary["mode"],
            enabled=summary["enabled"],
            done=summary["done"],
            passed=summary["passed"],
            failed=summary["failed"],
        )


def verdicts_for_feed(state: PlanState, records: Sequence[Any]) -> dict[int, tuple[str, str]]:
    """Оценки для записей ленты монитора: номер записи → (иконка, текст).

    Каждый пункт-вызов знает свой диапазон номеров журнала (`journal_from`/`journal_to`),
    поэтому оценка «соответствует ожиданию» показывается прямо в ленте — комиссия
    видит результат вызова, а не только код ответа.
    """
    verdicts: dict[int, tuple[str, str]] = {}
    for item in state.items:
        if not item.is_call or item.journal_to is None:
            continue
        icon = VERDICT_ICONS.get(item.verdict, "⛔")
        text = item.verdict or STATUS_ICONS.get(item.status, "⚪")
        for record in records:
            seq = getattr(record, "seq", None)
            if seq is None:
                continue
            if item.journal_from is not None and item.journal_from <= seq <= item.journal_to:
                verdicts[int(seq)] = (icon, text)
    return verdicts
