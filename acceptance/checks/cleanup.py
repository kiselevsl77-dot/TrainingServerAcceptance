"""Автоматические сценарии проверок уборки и учёта ресурсов (TC-CLEAN, этап T9).

Проверки группы ничего не создают и не удаляют: они подводят итог по тому, что
сделали другие проверки. Источник факта — сессия испытаний и снимки стенда:

    * `TC-CLEAN-02` — перечень `__TEST__`-сущностей (`session.test_entities`):
      у каждой записи должны быть идентификатор, тип, действие и время; созданное
      без удаления остаётся в перечне как «не удалено» (вход `TC-CLEAN-01`);
    * `TC-CLEAN-03` — сверка снимков «Начало»/«Окончание» (`session.snapshots`):
      дельта каждого счётчика должна объясняться созданными и удалёнными
      сущностями; необъяснённые расхождения — примечание (замечание отчёта),
      а не отказ: чек-лист требует их «описать в отчёте».

Счётчики стенда приходят из `acceptance.api.take_stand_snapshot`: `files`, `records`,
`loads`, `models`, `datasets`, `tasks`. Типы сущностей в сессии — свободный текст
(`файл RAW`, `dataset`, `модель`), поэтому связь «тип → счётчик» ищется по
подстроке; неизвестный тип попадает в расхождения и виден в отчёте.
"""

from __future__ import annotations

from typing import Any

from acceptance import session as session_api
from acceptance.checks.registry import CheckStatus
from acceptance.checks.runner import (
    AutomationContext,
    CheckOutcome,
    PreconditionError,
    automate,
    evaluate,
    scenario,
)

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: Обязательные поля записи о `__TEST__`-сущности (NFR-T4, FR-T8).
ENTITY_FIELDS: tuple[str, ...] = ("id", "type", "action", "at")

#: Допустимые действия в перечне (`session.ENTITY_CREATED` / `ENTITY_DELETED`).
ENTITY_ACTIONS: tuple[str, ...] = (session_api.ENTITY_CREATED, session_api.ENTITY_DELETED)

#: Связь «тип сущности → счётчик снимка стенда» (по подстроке в типе).
COUNTER_BY_TYPE: tuple[tuple[str, str], ...] = (
    ("файл", "files"),
    ("file", "files"),
    ("dataset", "datasets"),
    ("датасет", "datasets"),
    ("модель", "models"),
    ("model", "models"),
    ("нагрузка", "loads"),
    ("load", "loads"),
)

#: Счётчики, изменение которых объясняется задачами испытаний (обучение, наполнение).
TASK_COUNTERS: tuple[str, ...] = ("tasks",)


def _counter_for(entity_type: str) -> str:
    """Счётчик снимка, соответствующий типу `__TEST__`-сущности (пусто — не известен)."""
    text = str(entity_type).strip().lower()
    for needle, counter in COUNTER_BY_TYPE:
        if needle in text:
            return counter
    return ""


def _entities(context: AutomationContext) -> list[dict[str, Any]]:
    """Перечень `__TEST__`-сущностей сессии.

    Raises:
        PreconditionError: сессия пуста — учитывать нечего.
    """
    rows = list(session_api.test_entities(context.session))
    if not rows:
        raise PreconditionError(
            "в сессии нет `__TEST__`-сущностей: сначала выполните изменяющие проверки "
            "(файлы, датасеты, модели)"
        )
    return rows


def _entities_check(context: AutomationContext) -> CheckOutcome:
    """TC-CLEAN-02: перечень созданного и удалённого полон и попадёт в отчёт (FR-T8)."""
    rows = _entities(context)
    problems: list[str] = []
    incomplete: list[str] = []
    unknown_actions: list[str] = []

    for row in rows:
        missing = [field for field in ENTITY_FIELDS if not str(row.get(field) or "").strip()]
        if missing:
            incomplete.append(f"{row.get('id') or '?'}: нет полей {', '.join(missing)}")
        action = str(row.get("action") or "")
        if action not in ENTITY_ACTIONS:
            unknown_actions.append(f"{row.get('id') or '?'}: действие «{action}»")
        if not _counter_for(str(row.get("type") or "")):
            unknown_actions.append(f"{row.get('id') or '?'}: тип «{row.get('type')}»")

    if incomplete:
        problems.append("неполные записи перечня: " + "; ".join(incomplete))
    unknown = [item for item in unknown_actions if "действие" in item]
    if unknown:
        problems.append("неизвестные действия: " + "; ".join(unknown))

    created = [row for row in rows if str(row.get("action")) == session_api.ENTITY_CREATED]
    deleted = [row for row in rows if str(row.get("action")) == session_api.ENTITY_DELETED]
    pending = list(session_api.pending_test_entities(context.session))
    by_type = {
        str(row.get("type")): sum(
            1 for item in rows if str(item.get("type")) == str(row.get("type"))
        )
        for row in rows
    }
    evidence: dict[str, Any] = {
        "total": len(rows),
        "created": len(created),
        "deleted": len(deleted),
        "pending": len(pending),
        "pending_ids": [str(item.get("id")) for item in pending],
        "by_type": by_type,
        "checks": sorted({str(row.get("check_id") or "") for row in rows if row.get("check_id")}),
        "types_without_counter": [item for item in unknown_actions if "тип" in item],
        "entries": rows,
    }
    notes: list[str] = []
    if pending:
        notes.append(
            f"не удалено {len(pending)} сущностей: их разбирает TC-CLEAN-01 (создано "
            f"{len(created)}, удалено {len(deleted)})"
        )
    if evidence["types_without_counter"]:
        notes.append(
            "типы без соответствия счётчику снимка: " + "; ".join(evidence["types_without_counter"])
        )

    if problems:
        return CheckOutcome(
            CheckStatus.FAILED, "; ".join(problems), evidence, note="; ".join(notes)
        )
    verdict = (
        f"перечень `__TEST__`-сущностей полон: {len(rows)} записей "
        f"(создано {len(created)}, удалено {len(deleted)}, не удалено {len(pending)}) — "
        "он идёт в отчёт приложением"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence, note="; ".join(notes))


def _snapshot(context: AutomationContext, phase: str) -> dict[str, Any]:
    """Снимок стенда фазы (`session.SNAP_START` / `SNAP_END`)."""
    stored = context.session.snapshots.get(phase)
    return dict(stored) if isinstance(stored, dict) else {}


def _expected_by_counter(
    entities: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Ожидаемые изменения счётчиков: создано и удалено по типам сущностей."""
    created: dict[str, int] = {}
    deleted: dict[str, int] = {}
    for row in entities:
        counter = _counter_for(str(row.get("type") or ""))
        if not counter:
            continue
        target = created if str(row.get("action")) == session_api.ENTITY_CREATED else deleted
        target[counter] = target.get(counter, 0) + 1
    return created, deleted


def _snapshot_check(context: AutomationContext) -> CheckOutcome:
    """TC-CLEAN-03: сверка снимков «Начало»/«Окончание» (FR-T1, BR-R6).

    Расхождение — не отказ проверки: чек-лист требует «описать расхождения в отчёте».
    Отказ ставится только тогда, когда снимков нет или в них нет счётчиков: сверять
    нечего.
    """
    start = _snapshot(context, session_api.SNAP_START)
    end = _snapshot(context, session_api.SNAP_END)
    if not start:
        raise PreconditionError("снимок «Начало» не снят: сверку снимков выполнить нечем")
    if not end:
        raise PreconditionError(
            "снимок «Окончание» не снят: снимите его в сессии перед проверкой TC-CLEAN-03"
        )

    start_counts = {key: int(value or 0) for key, value in dict(start.get("counts") or {}).items()}
    end_counts = {key: int(value or 0) for key, value in dict(end.get("counts") or {}).items()}
    entities = list(session_api.test_entities(context.session))
    created, deleted = _expected_by_counter(entities)

    rows: list[dict[str, Any]] = []
    unexplained: list[str] = []
    for counter in sorted(set(start_counts) | set(end_counts)):
        left = start_counts.get(counter, 0)
        right = end_counts.get(counter, 0)
        delta = right - left
        expected = created.get(counter, 0) - deleted.get(counter, 0)
        explained = True
        reason = "изменений нет"
        if delta == 0:
            reason = "счётчик не изменился"
        elif delta == expected:
            reason = (
                f"объяснено `__TEST__`-сущностями (создано {created.get(counter, 0)}, "
                f"удалено {deleted.get(counter, 0)})"
            )
        elif counter in TASK_COUNTERS and delta > 0:
            reason = "задачи испытаний (наполнение/обучение/инференс) — счётчик растёт"
        elif delta < 0:
            reason = "счётчик уменьшился: удалено больше, чем зарегистрировано в сессии"
            explained = False
        else:
            reason = (
                f"не объяснено: изменение {delta:+d} при ожидаемом {expected:+d} по перечню сессии"
            )
            explained = False
        if not explained:
            unexplained.append(f"{counter}: {delta:+d} (ожидалось {expected:+d})")
        rows.append(
            {
                "counter": counter,
                "start": left,
                "end": right,
                "delta": delta,
                "expected": expected,
                "explained": explained,
                "reason": reason,
            }
        )

    evidence: dict[str, Any] = {
        "start_at": str(start.get("at") or ""),
        "end_at": str(end.get("at") or ""),
        "start_version": start.get("version"),
        "end_version": end.get("version"),
        "start_errors": dict(start.get("errors") or {}),
        "end_errors": dict(end.get("errors") or {}),
        "delta": rows,
        "unexplained": unexplained,
        "entities": {
            "total": len(entities),
            "created": created,
            "deleted": deleted,
        },
    }
    notes: list[str] = []
    if unexplained:
        notes.append("расхождения для отчёта: " + "; ".join(unexplained))
    if start.get("errors") or end.get("errors"):
        notes.append(
            "в снимках есть ошибки чтения реестров: "
            + ", ".join(
                sorted(set(dict(start.get("errors") or {})) | set(dict(end.get("errors") or {})))
            )
        )
    if start.get("version") != end.get("version"):
        notes.append("сборка сервера за время сессии изменилась: версии снимков различаются")

    explained_count = len([row for row in rows if row["explained"]])
    verdict = (
        f"снимки «Начало»/«Окончание» сверены: счётчиков {len(rows)}, изменения объяснены "
        f"по {explained_count}; создано сущностей {len(entities)} (не объяснено "
        f"{len(unexplained)})"
    )
    return CheckOutcome(
        CheckStatus.PASSED,
        verdict,
        evidence,
        note="; ".join(notes),
    )


#: Реестр сценариев модуля (ключ — `CheckSpec.automation`).
AUTOMATIONS: dict[str, Any] = {
    "cleanup.entities": _entities_check,
    "cleanup.snapshots": _snapshot_check,
}
