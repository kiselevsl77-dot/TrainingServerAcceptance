"""Движок проверок чек-листа: запуск, вердикт, фиксация результата (FR-T4).

Задача движка — сделать каждую проверку **воспроизводимой**: пока проверка идёт,
все её запросы помечаются меткой `TC-…` (`label_context` + `check_context`), поэтому
в журнале обмена и в JSONL сессии видно, к какой проверке относится каждый вызов;
в результате сохраняются параметры запуска, время, длительность и диапазон номеров
записей журнала (`journal_from`…`journal_to`).

Результат проверки — `CheckResult` (`registry.py`), который складывается в
`session.checks` и попадает в отчёт. Движок отвечает и за требования к запуску:
`live`/`heavy` проверки выполняются только после подтверждения оператора
(цель, используемые данные, ответственный) — `check_confirmation`.

Этап T4 использует движок для группы `TC-TASK`; остальные группы подключаются в T3
без изменения этого модуля (каталог → `catalog.py`, сценарии → `tasks.py` и далее).
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from typing import Any, Literal

from acceptance.checks.registry import (
    CheckResult,
    CheckSpec,
    CheckStatus,
    summarize,
)
from acceptance.http_log import Journal, label_context
from acceptance.logging_setup import check_context, log_event
from acceptance.session import TestSession, now_iso

#: Статусы, которые оператор ставит вручную (без автоматического прогона).
MANUAL_STATUSES: tuple[CheckStatus, ...] = (
    CheckStatus.MANUAL_OK,
    CheckStatus.SKIPPED,
    CheckStatus.BLOCKED,
    CheckStatus.INTERRUPTED,
)


@contextmanager
def check_label(label: str) -> Iterator[None]:
    """Помечает запросы и записи журнала внутри блока меткой проверки (`TC-…`)."""
    with label_context(label), check_context(label):
        yield


def journal_bounds(journal: Journal | None, label: str) -> tuple[int | None, int | None]:
    """Диапазон номеров записей журнала, помеченных меткой проверки.

    Returns:
        Кортеж (первый, последний) номер; `(None, None)`, если записей нет.
    """
    if journal is None:
        return None, None
    sequences = [record.seq for record in journal.for_label(label)]
    if not sequences:
        return None, None
    return min(sequences), max(sequences)


def journal_start(journal: Journal | None) -> int | None:
    """Номер, с которого начинается диапазон записей проверки (`journal_from`).

    Проверка начинается «с текущего места» журнала, поэтому первый её запрос
    получает именно этот номер (а не минимум из прежних записей с той же меткой —
    проверку можно перезапускать, не теряя границ нового прогона).
    """
    if journal is None:
        return None
    return journal.peek_next_seq()


def snapshot(
    spec: CheckSpec,
    *,
    journal: Journal | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Состояние «начата проверка» для хранения между перерисовками UI.

    Ассистируемые проверки (оператор выполняет шаги на других экранах) живут
    несколько перерисовок Streamlit, поэтому начало проверки фиксируется обычными
    данными (`dict`), а не контекстом: контексты не переносятся между прогонами.
    """
    first, _ = journal_bounds(journal, spec.check_id)
    return {
        "label": spec.check_id,
        "started_at": now_iso(),
        "journal_from": journal_start(journal) if first is None else first,
        "params": dict(params or {}),
    }


class CheckRun:
    """Запущенная проверка: метка, параметры, диапазон журнала, время.

    Используется как контекстный менеджер: пока он открыт, все запросы получают
    метку проверки и попадают в её диапазон журнала::

        with start_check(spec, journal=journal, params={"duration": 2}) as run:
            ...
            result = run.finish(CheckStatus.PASSED, verdict="задача создана")
    """

    def __init__(
        self,
        spec: CheckSpec,
        *,
        journal: Journal | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.spec = spec
        self.journal = journal
        self.params: dict[str, Any] = dict(params or {})
        self.started_at = now_iso()
        self.started_monotonic = time.monotonic()
        self.journal_from: int | None = None
        self.finished = False
        self._stack = ExitStack()

    # -- контекст ------------------------------------------------------------
    def __enter__(self) -> CheckRun:
        """Открывает метку проверки и фиксирует начало диапазона журнала."""
        self._stack.enter_context(check_label(self.spec.check_id))
        self.journal_from = journal_start(self.journal)
        log_event(
            "check_started",
            f"Начата проверка {self.spec.check_id}: {self.spec.title}",
            module="checks",
            check_id=self.spec.check_id,
            payload={
                "check_id": self.spec.check_id,
                "title": self.spec.title,
                "check_class": str(self.spec.check_class),
                "params": self.params,
                "journal_from": self.journal_from,
                "started_at": self.started_at,
            },
        )
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        """Закрывает метку проверки (результат записывает вызывающий код)."""
        self.close()
        return False

    def close(self) -> None:
        """Закрывает контекст проверки (идемпотентно)."""
        if not self.finished:
            self._stack.close()
            self.finished = True

    # -- результат -----------------------------------------------------------
    @property
    def duration_ms(self) -> float:
        """Длительность проверки в миллисекундах."""
        return round((time.monotonic() - self.started_monotonic) * 1000, 1)

    def result(
        self,
        status: CheckStatus,
        *,
        verdict: str = "",
        operator_note: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Формирует результат проверки с диапазоном журнала и временем."""
        last: int | None = None
        if self.journal is not None:
            sequences = [
                record.seq
                for record in self.journal.for_label(self.spec.check_id)
                if self.journal_from is None or record.seq >= self.journal_from
            ]
            last = max(sequences) if sequences else None
        return CheckResult(
            check_id=self.spec.check_id,
            status=status,
            verdict=verdict,
            operator_note=operator_note,
            evidence=dict(evidence or {}),
            params=dict(self.params),
            started_at=self.started_at,
            ended_at=now_iso(),
            duration_ms=self.duration_ms,
            journal_from=self.journal_from,
            journal_to=last,
        )

    def finish(
        self,
        status: CheckStatus,
        *,
        verdict: str = "",
        operator_note: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Закрывает проверку и возвращает её результат."""
        result = self.result(
            status, verdict=verdict, operator_note=operator_note, evidence=evidence
        )
        label = self.spec.check_id
        self.close()
        log_event(
            "check_finished",
            f"Проверка {label}: {status}",
            level="INFO" if status == CheckStatus.PASSED else "WARNING",
            module="checks",
            check_id=label,
            payload=result.to_dict(),
        )
        return result


def start_check(
    spec: CheckSpec,
    *,
    journal: Journal | None = None,
    params: dict[str, Any] | None = None,
) -> CheckRun:
    """Создаёт запуск проверки (открывается через `with` или вручную `__enter__`)."""
    return CheckRun(spec, journal=journal, params=params)


# ---------------------------------------------------------------------------
# Фиксация результатов в сессии
# ---------------------------------------------------------------------------
def record_result(session: TestSession, result: CheckResult | dict[str, Any]) -> dict[str, Any]:
    """Сохраняет результат проверки в сессии (по одной записи на проверку)."""
    payload = result.to_dict() if isinstance(result, CheckResult) else dict(result)
    check_id = str(payload.get("check_id") or "")
    for index, item in enumerate(session.checks):
        if str(item.get("check_id")) == check_id:
            session.checks[index] = payload
            break
    else:
        session.checks.append(payload)

    session.add_history(
        "check_recorded",
        f"Проверка {check_id}: {payload.get('status')}",
        check_id=check_id,
        status=payload.get("status"),
        journal_from=payload.get("journal_from"),
        journal_to=payload.get("journal_to"),
    )
    return payload


def results_map(session: TestSession) -> dict[str, CheckResult]:
    """Результаты проверок сессии по `check_id` (терпимо к повреждённым записям)."""
    restored: dict[str, CheckResult] = {}
    for item in session.checks:
        try:
            result = item if isinstance(item, CheckResult) else CheckResult.from_dict(item)
        except (TypeError, ValueError):
            continue
        restored[result.check_id] = result
    return restored


def result_of(session: TestSession, check_id: str) -> CheckResult:
    """Результат проверки или пустой результат (статус «не выполнена»)."""
    found = results_map(session).get(check_id)
    return found if found is not None else CheckResult(check_id=check_id)


def mark(
    session: TestSession,
    spec: CheckSpec,
    status: CheckStatus,
    *,
    verdict: str = "",
    operator_note: str = "",
    evidence: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    duration_ms: float | None = None,
    journal_from: int | None = None,
    journal_to: int | None = None,
) -> CheckResult:
    """Ставит результат проверки без автоматического прогона (ветки оператора).

    Используется для статусов «пропущена», «блокировано API», «выполнена вручную»,
    «прервана» (BR-R5/BR-R6, NFR-T4) и для проверок, выполненных в консоли вручную.
    """
    result = CheckResult(
        check_id=spec.check_id,
        status=status,
        verdict=verdict,
        operator_note=operator_note,
        evidence=dict(evidence or {}),
        params=dict(params or {}),
        started_at=now_iso(),
        ended_at=now_iso(),
        duration_ms=duration_ms,
        journal_from=journal_from,
        journal_to=journal_to,
    )
    record_result(session, result)
    log_event(
        "check_marked",
        f"Проверка {spec.check_id}: {status} (отметка оператора)",
        level="INFO" if status == CheckStatus.MANUAL_OK else "WARNING",
        module="checks",
        check_id=spec.check_id,
        payload=result.to_dict(),
    )
    return result


def group_stats(session: TestSession, check_ids: Sequence[str]) -> dict[str, Any]:
    """Сводка результатов группы проверок (KPI экрана чек-листа)."""
    return summarize([result_of(session, check_id) for check_id in check_ids])


# ---------------------------------------------------------------------------
# Требования к запуску (NFR-T4, FR-T10)
# ---------------------------------------------------------------------------
def check_confirmation(
    spec: CheckSpec, card: Any | None = None
) -> tuple[bool, str, dict[str, Any]]:
    """Проверяет требования подтверждения для класса проверки.

    Карточка запуска (`acceptance.ui.common.run_card.RunCard`) передаётся как
    «утиная» структура: движок не зависит от Streamlit и проверяет только поля
    `missing`, `confirmed` и `to_dict()`.

    Returns:
        Кортеж (можно запускать, причина отказа, доказательства подтверждения).
    """
    if not spec.is_confirmation_required:
        return True, "", {}
    if card is None:
        return (
            False,
            f"проверка класса «{spec.class_label}» выполняется только с подтверждением оператора",
            {},
        )

    missing = tuple(getattr(card, "missing", ()) or ())
    to_dict = getattr(card, "to_dict", None)
    evidence = dict(to_dict()) if callable(to_dict) else {}
    if missing:
        return False, "заполните карточку проверки: " + ", ".join(map(str, missing)), evidence
    if not bool(getattr(card, "confirmed", False)):
        return False, "нет подтверждения расхода ресурсов (FR-T10)", evidence

    evidence.setdefault("confirmation", f"запуск подтверждён оператором ({spec.check_id})")
    return True, "", evidence


def status_options() -> tuple[CheckStatus, ...]:
    """Статусы, доступные оператору для ручной отметки проверки."""
    return MANUAL_STATUSES
