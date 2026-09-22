"""Результаты проверок: единый след (`DR-P-5`) и история повторов (`FR-P-36`).

Статус проверки хранится **в одном месте** — в сессии (`session.checks`), по одной
записи на проверку. Ни очередь (`queue.py`), ни программа (`programme.py`) статус
не хранят: очередь отвечает за состояние прогона, программа — за состав. Так
исключается двойной учёт — «две правды» о результате проверки.

Что делает модуль:

* `prepare_repeat` — перед повторным прогоном убирает прежний результат в историю
  (`session.check_history`): новый результат ляжет поверх, а прежний останется
  читаемым (`FR-P-36` — повтор сохраняет предыдущий результат как историю);
* `record` — записывает результат (когда запись делает не движок сценариев) и
  `annotate` — дополняет уже записанный результат полями пульта (`origin`, автор,
  ревизия программы), **не** создавая повтора;
* `suspend` / `resume` — снятие пункта с причиной и возврат: снятие — это
  **результат прогона** (`FR-P-15`), а не правка состава программы, поэтому оно
  живёт здесь, а не в очереди (`IR-P-18`);
* `state` / `row` / `rows` / `summary` — представление для таблиц «Прогон»,
  «Карточка проверки» и «Протокол» (вход `Programme.rows(results)`).

Разделение с движком (`acceptance/checks/engine.py`): движок пишет «последний
результат» проверки, а историю повторов и снятие ведёт этот модуль. Поэтому
исполнитель вызывает `prepare_repeat` **до** прогона сценария, а не записывает
результат второй раз (иначе повтор попал бы в историю сам на себя).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from acceptance.checks.registry import STATUS_ICONS, CheckStatus
from acceptance.session import now_iso

#: Статус «результата нет» — проверка ещё не выполнялась.
STATUS_NOT_RUN = str(CheckStatus.NOT_RUN)

#: Чем получен результат (видно в протоколе: прогон, отметка, снятие).
ORIGIN_RUN = "прогон"
ORIGIN_MANUAL = "отметка оператора"
ORIGIN_SUSPENDED = "снято с причиной"

#: Поля результата, которые понимает движок (`CheckResult`).
RESULT_FIELDS: tuple[str, ...] = (
    "check_id",
    "status",
    "verdict",
    "operator_note",
    "evidence",
    "params",
    "started_at",
    "ended_at",
    "duration_ms",
    "journal_from",
    "journal_to",
)

#: Дополнительные поля пульта. Хранятся в той же записи: `CheckResult.from_dict`
#: читает только известные ему поля, поэтому лишние ключи движку не мешают.
TRACE_FIELDS: tuple[str, ...] = ("origin", "author", "at", "revision", "suspended", "reason")


def _key(check_id: str) -> str:
    """Идентификатор проверки в нормальном виде (без пробелов, верхний регистр)."""
    return str(check_id).strip().upper()


def _add_history(session: Any, event: str, message: str, **payload: Any) -> None:
    """Пишет событие в историю сессии, если сессия это умеет."""
    notify = getattr(session, "add_history", None)
    if callable(notify):
        notify(event, message, **payload)


def records_of(session: Any) -> list[dict[str, Any]]:
    """Записи результатов сессии (терпимо к отсутствию и повреждению записей).

    Мусор (записи не-словари) пропускается: сессия может быть восстановлена из
    частично испорченного файла, и чтение не должно из-за этого падать.
    """
    items = getattr(session, "checks", None)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _has_result(record: Mapping[str, Any]) -> bool:
    """True, если запись — результат (а не пустая заготовка «не выполнена»)."""
    status = str(record.get("status") or "")
    return bool(status) and status != STATUS_NOT_RUN


def _same_result(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True, если две записи описывают один и тот же результат (защита от дубля)."""
    keys = ("check_id", "status", "at", "journal_from", "journal_to")
    return all(left.get(key) == right.get(key) for key in keys)


# ---------------------------------------------------------------------------
# Чтение: единственный след результата
# ---------------------------------------------------------------------------
def result_of(session: Any, check_id: str) -> dict[str, Any] | None:
    """Результат проверки: её запись в `session.checks` (None — результата нет)."""
    key = _key(check_id)
    if not key:
        return None
    for record in records_of(session):
        if _key(str(record.get("check_id") or "")) == key:
            return record
    return None


def status_of(session: Any, check_id: str) -> str:
    """Статус проверки: значение `CheckStatus` или «не выполнена»."""
    record = result_of(session, check_id)
    return STATUS_NOT_RUN if record is None else str(record.get("status") or STATUS_NOT_RUN)


def is_suspended(session: Any, check_id: str) -> bool:
    """True, если проверка снята с причиной (снятие — результат, `FR-P-15`)."""
    record = result_of(session, check_id)
    if record is None:
        return False
    return bool(record.get("suspended")) or str(record.get("origin") or "") == ORIGIN_SUSPENDED


def history(session: Any, check_id: str) -> list[dict[str, Any]]:
    """История прежних результатов проверки (`FR-P-36`)."""
    log = getattr(session, "check_history", None)
    if not isinstance(log, dict):
        return []
    return [dict(item) for item in (log.get(_key(check_id)) or []) if isinstance(item, dict)]


def repeats(session: Any, check_id: str) -> int:
    """Сколько результатов проверки уже ушло в историю (число повторов)."""
    return len(history(session, check_id))


def row(session: Any, check_id: str) -> dict[str, Any]:
    """Строка результата для таблиц «Прогон», «Карточка проверки» и «Протокол»."""
    key = _key(check_id)
    record = result_of(session, key) or {}
    status = str(record.get("status") or STATUS_NOT_RUN)
    suspended = bool(record.get("suspended")) or str(record.get("origin") or "") == ORIGIN_SUSPENDED
    return {
        "check_id": key,
        "status": status,
        "icon": STATUS_ICONS.get(status, "⚪"),
        "verdict": str(record.get("verdict") or ""),
        "operator_note": str(record.get("operator_note") or ""),
        "at": str(record.get("at") or record.get("ended_at") or ""),
        "origin": str(record.get("origin") or ""),
        "author": str(record.get("author") or ""),
        "revision": record.get("revision"),
        "journal_from": record.get("journal_from"),
        "journal_to": record.get("journal_to"),
        "duration_ms": record.get("duration_ms"),
        "evidence": dict(record.get("evidence") or {}),
        "suspended": suspended,
        "reason": str(record.get("reason") or ""),
        "repeats": repeats(session, key),
        "has_result": _has_result(record),
    }


def state(session: Any, check_ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Состояние проверок по `check_id` — вход `Programme.rows(results)` и таблиц.

    Без `check_ids` возвращаются только проверки, у которых есть запись результата.
    """
    if check_ids is None:
        keys = [
            _key(str(record.get("check_id") or ""))
            for record in records_of(session)
            if _key(str(record.get("check_id") or ""))
        ]
    else:
        keys = [_key(check_id) for check_id in check_ids]
    return {key: row(session, key) for key in keys}


def rows(session: Any, check_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Строки результатов в переданном порядке (нет результата — «не выполнена»)."""
    return [row(session, check_id) for check_id in check_ids]


def summary(session: Any) -> dict[str, Any]:
    """Сводка результатов: KPI протокола, число снятий и повторов."""
    items = [
        row(session, str(record.get("check_id") or ""))
        for record in records_of(session)
        if _key(str(record.get("check_id") or ""))
    ]
    counts: dict[str, int] = {str(status): 0 for status in CheckStatus}
    for item in items:
        status = str(item["status"])
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(items),
        "done": sum(1 for item in items if item["has_result"]),
        "passed": counts.get(str(CheckStatus.PASSED), 0),
        "failed": counts.get(str(CheckStatus.FAILED), 0),
        "blocked": counts.get(str(CheckStatus.BLOCKED), 0),
        "skipped": counts.get(str(CheckStatus.SKIPPED), 0),
        "manual": counts.get(str(CheckStatus.MANUAL_OK), 0),
        "interrupted": counts.get(str(CheckStatus.INTERRUPTED), 0),
        "not_run": counts.get(STATUS_NOT_RUN, 0),
        "suspended": sum(1 for item in items if item["suspended"]),
        "repeats": sum(int(item["repeats"]) for item in items),
        "counts": counts,
    }


def normalize(
    payload: Any,
    *,
    origin: str = ORIGIN_RUN,
    author: str = "",
    revision: int | None = None,
) -> dict[str, Any]:
    """Приводит результат (движка или словарь) к записи сессии с полями пульта.

    Raises:
        TypeError: если передан не `CheckResult` и не словарь.
    """
    if hasattr(payload, "to_dict"):
        raw: dict[str, Any] = dict(payload.to_dict())
    elif isinstance(payload, Mapping):
        raw = dict(payload)
    else:  # pragma: no cover - защита от неверного вызова
        raise TypeError("результат проверки должен быть CheckResult или словарём")
    record: dict[str, Any] = {field: raw.get(field) for field in RESULT_FIELDS}
    record["check_id"] = _key(str(raw.get("check_id") or ""))
    record["status"] = str(raw.get("status") or STATUS_NOT_RUN)
    record["verdict"] = str(raw.get("verdict") or "")
    record["operator_note"] = str(raw.get("operator_note") or "")
    record["evidence"] = dict(raw.get("evidence") or {})
    record["params"] = dict(raw.get("params") or {})
    record["origin"] = str(raw.get("origin") or origin)
    record["author"] = str(raw.get("author") or author)
    record["at"] = str(raw.get("at") or now_iso())
    stored_revision = raw.get("revision")
    record["revision"] = stored_revision if stored_revision is not None else revision
    record["suspended"] = bool(raw.get("suspended")) or record["origin"] == ORIGIN_SUSPENDED
    record["reason"] = str(raw.get("reason") or "")
    return record


def _history_slot(session: Any, check_id: str) -> list[dict[str, Any]]:
    """Список истории проверки в сессии (создаёт при необходимости)."""
    log = getattr(session, "check_history", None)
    if not isinstance(log, dict):
        log = {}
        session.check_history = log
    key = _key(check_id)
    slot = log.get(key)
    if not isinstance(slot, list):
        slot = []
        log[key] = slot
    return slot


def _store(session: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Кладёт запись в `session.checks`: замена по `check_id`, иначе добавление."""
    items = getattr(session, "checks", None)
    if not isinstance(items, list):
        items = []
        session.checks = items
    key = _key(str(entry.get("check_id") or ""))
    for index, existing in enumerate(items):
        if isinstance(existing, dict) and _key(str(existing.get("check_id") or "")) == key:
            items[index] = entry
            break
    else:
        items.append(entry)
    return entry


def prepare_repeat(session: Any, check_id: str) -> dict[str, Any] | None:
    """Убирает прежний результат проверки в историю перед повтором (`FR-P-36`).

    Вызывается **до** повторного прогона: движок запишет новый результат поверх,
    а прежний останется читаемым в `session.check_history`.

    Returns:
        Прежний результат, ушедший в историю, или None (результата ещё не было).
    """
    key = _key(check_id)
    current = result_of(session, key)
    if current is None or not _has_result(current):
        return None
    slot = _history_slot(session, key)
    if slot and isinstance(slot[-1], dict) and _same_result(slot[-1], current):
        return current
    slot.append(dict(current))
    _add_history(
        session,
        "check_repeat_prepared",
        f"Проверка {key}: прежний результат «{current.get('status')}» сохранён в историю",
        check_id=key,
        status=current.get("status"),
        repeats=len(slot),
    )
    return current


def record(
    session: Any,
    payload: Any,
    *,
    origin: str = ORIGIN_RUN,
    author: str = "",
    revision: int | None = None,
    remember: bool = True,
) -> dict[str, Any]:
    """Записывает результат проверки: **одна** запись на проверку (`DR-P-5`).

    Args:
        session: сессия испытаний.
        payload: `CheckResult` движка или словарь результата.
        origin: чем получен результат (`ORIGIN_RUN`, `ORIGIN_MANUAL`, снятие).
        author: исполнитель (попадает в протокол и отчёт).
        revision: ревизия программы, по которой получен результат (`DR-P-14`).
        remember: убрать прежний результат в историю (`FR-P-36`).

    Raises:
        ValueError: если в записи нет `check_id`.
    """
    entry = normalize(payload, origin=origin, author=author, revision=revision)
    if not entry["check_id"]:
        raise ValueError("результат проверки без `check_id` записать нельзя")
    if remember:
        prepare_repeat(session, entry["check_id"])
    _store(session, entry)
    _add_history(
        session,
        "check_recorded",
        f"Проверка {entry['check_id']}: {entry['status']}",
        check_id=entry["check_id"],
        status=entry["status"],
        origin=entry["origin"],
        revision=entry["revision"],
    )
    return entry


def annotate(
    session: Any,
    payload: Any,
    *,
    origin: str = ORIGIN_RUN,
    author: str = "",
    revision: int | None = None,
) -> dict[str, Any]:
    """Дополняет уже записанный результат полями пульта, **не** создавая повтора.

    Нужен там, где результат пишет движок (`checks_runner.automate` →
    `engine.record_result`): исполнитель дописывает в ту же запись происхождение,
    автора и ревизию программы, а история повторов остаётся нетронутой.
    """
    entry = normalize(payload, origin=origin, author=author, revision=revision)
    current = result_of(session, entry["check_id"])
    if current is None:
        return record(session, entry, remember=False)
    if not entry["suspended"]:
        entry["suspended"] = bool(current.get("suspended"))
        entry["reason"] = str(current.get("reason") or "")
    _store(session, entry)
    return entry


def suspend(
    session: Any,
    check_id: str,
    reason: str,
    *,
    author: str = "",
    revision: int | None = None,
) -> dict[str, Any]:
    """Снимает пункт с причиной — это **результат прогона**, а не правка состава.

    Raises:
        ValueError: если причина не задана (`FR-P-15`; `AC-P-15`: снятие и ручная
            отметка без заключения не принимаются).
    """
    key = _key(check_id)
    text = str(reason or "").strip()
    if not text:
        raise ValueError(f"снятие пункта {key or '?'} требует причины (FR-P-15)")
    entry: dict[str, Any] = {
        "check_id": key,
        "status": str(CheckStatus.SKIPPED),
        "verdict": text,
        "operator_note": text,
        "origin": ORIGIN_SUSPENDED,
        "suspended": True,
        "reason": text,
        "evidence": {"origin": ORIGIN_SUSPENDED, "reason": text},
    }
    return record(session, entry, origin=ORIGIN_SUSPENDED, author=author, revision=revision)


def resume(session: Any, check_id: str, *, author: str = "") -> bool:
    """Возвращает снятый пункт в очередь: результат снятия убирается.

    Проверка снова становится «не выполнена», а событие возврата остаётся в истории
    сессии — ничего не исчезает молча.

    Returns:
        True, если снятие было снято; False, если проверка не была снята.
    """
    key = _key(check_id)
    if not is_suspended(session, key):
        return False
    items = getattr(session, "checks", None)
    if isinstance(items, list):
        items[:] = [
            item
            for item in items
            if not (isinstance(item, dict) and _key(str(item.get("check_id") or "")) == key)
        ]
    _add_history(
        session,
        "check_resumed",
        f"Пункт {key} возвращён в очередь: результат снятия убран",
        check_id=key,
        author=author,
    )
    return True
