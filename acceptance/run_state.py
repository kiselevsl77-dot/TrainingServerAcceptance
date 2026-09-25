"""Состояние прогона: очередь по ревизии и признак «выполнено по факту запроса».

Схема сессии v8 (решение заказчика 25.09.2026, `llm_tasks/31.4` §1 п.6–8):

* очередь прогона **не хранится** — она вычисляется по ревизии программы
  (`queue.queue_for_revision`), а состояние пунктов восстанавливается из
  единственного следа результата (`results.py`, `DR-P-5`);
* пункт считается **выполненным**, если в журнале **этой** сессии уже есть
  успешный обмен по каждому эндпоинту проверки (`CheckSpec.endpoints`), либо если
  результат уже записан в `results.py`. Приоритет — журналу: он показывает, что
  проверка подтверждена обычными запросами (например, чтением реестров на экране
  «Данные»), а не запуском пункта программы;
* режим прогона: `skip_done = True` — «только невыполненное» (`UC-09`),
  `skip_done = False` — «выполнять всё подряд, включая повторы» (тогда история
  повторов ведётся `results.py`, `FR-P-36`).

Модуль чистый: он не знает ни интерфейса, ни HTTP-сервера, ни стенда — журнал
передаётся снаружи (`Journal.records`), поэтому правило проверяется unit-тестами.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from acceptance import endpoints as ep
from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme
from acceptance.session import TestSession

#: Границы «успешного» ответа стенда (2xx).
SUCCESS_MIN = 200
SUCCESS_MAX = 300

#: Подпись способа получения результата: проверка не запускалась пунктом программы,
#: её подтвердил успешный запрос, уже выполненный в этой сессии.
CONFIRMED_VERDICT = "подтверждено ранее выполненным запросом"

#: Пометка в записи результата и в колонке «способ» протокола.
CONFIRMED_NOTE = "выполнено по факту запроса"

#: Режимы прогона (ТЗ §2, решение 7): какие пункты берёт «▶ Следующая».
RUN_ONLY_REMAINING = "only-remaining"
RUN_ALL = "all"
RUN_MODES: tuple[str, ...] = (RUN_ONLY_REMAINING, RUN_ALL)


def run_mode_of(session: TestSession) -> str:
    """Режим прогона сессии: «только невыполненное» (по умолчанию) или «всё подряд»."""
    return RUN_ONLY_REMAINING if bool(getattr(session, "skip_done", True)) else RUN_ALL


def set_run_mode(session: TestSession, mode: str) -> str:
    """Задаёт режим прогона сессии (`skip_done`).

    Неизвестное значение откатывается к «только невыполненное»: молча запускать всё
    подряд, включая повторы, нельзя — это перезаписало бы результаты испытаний
    историей повторов (`FR-P-36`).
    """
    value = str(mode or "").strip()
    if value not in RUN_MODES:
        value = RUN_ONLY_REMAINING
    session.skip_done = value == RUN_ONLY_REMAINING
    return value


def session_records(records: Iterable[Any] | None, session: TestSession) -> tuple[Any, ...]:
    """Обмены сессии: журнал, ограниченный моментом создания сессии.

    Журнал живёт в процессе пульта и переживает переключение сессий, поэтому в
    «факт этой сессии» попадают только записи, сделанные после её создания.
    """
    if not records:
        return ()
    since = str(session.created_at or "")
    return tuple(record for record in records if _started_at(record) >= since)


def _started_at(record: Any) -> str:
    """Время начала обмена строкой (пустая строка — неизвестно)."""
    return str(getattr(record, "started_at", "") or "")


def is_success(record: Any) -> bool:
    """True, если обмен завершился успешным ответом стенда (2xx)."""
    status = getattr(record, "status", None)
    if not isinstance(status, int):
        return False
    return SUCCESS_MIN <= status < SUCCESS_MAX


def endpoint_specs(check_id: str) -> tuple[Any, ...]:
    """Операции контракта, которыми подтверждается проверка (может быть пусто).

    Проверка без объявленных операций (ручная, негативные пробы `probe_paths`)
    запросом не подтверждается: её закрывает только прогон или отметка оператора.
    """
    spec = catalog.find(check_id)
    if spec is None:
        return ()
    found: list[Any] = []
    for key in spec.endpoints:
        known = ep.find(str(key))
        if known is None:
            return ()
        found.append(known)
    return tuple(found)


def _match(record: Any, spec: Any) -> bool:
    """True, если обмен — вызов операции (метод и шаблон пути совпадают)."""
    method = str(getattr(record, "method", "") or "").strip().upper()
    path = str(getattr(record, "path", "") or "").strip()
    if not method or not path:
        return False
    found = ep.match_path(method, path)
    return found is not None and (found.method, found.path) == (spec.method, spec.path)


def evidence_by_request(records: Iterable[Any] | None, check_id: str) -> dict[str, Any] | None:
    """Подтверждение проверки успешными запросами сессии (None — не подтверждена).

    Возвращает доказательство: перечень операций и **диапазон номеров журнала** —
    первая и последняя записи, которыми проверка подтверждена (`FR-P-33`).
    """
    specs = endpoint_specs(check_id)
    if not specs:
        return None
    items = tuple(records or ())
    numbers: list[int] = []
    keys: list[str] = []
    for spec in specs:
        hit = next(
            (record for record in items if is_success(record) and _match(record, spec)), None
        )
        if hit is None:
            return None
        keys.append(f"{str(spec.method).lower()} {spec.path}")
        number = getattr(hit, "seq", None)
        if isinstance(number, int):
            numbers.append(number)
    return {
        "endpoints": keys,
        "journal_from": min(numbers) if numbers else None,
        "journal_to": max(numbers) if numbers else None,
        "note": CONFIRMED_NOTE,
    }


def confirmed_ids(
    session: TestSession, programme: Programme, records: Iterable[Any] | None
) -> list[str]:
    """Проверки программы, которые можно закрыть по факту запросов этой сессии."""
    items = session_records(records, session)
    result: list[str] = []
    for item in programme.items:
        if results_api.result_of(session, item.check_id) is not None:
            continue
        if evidence_by_request(items, item.check_id) is not None:
            result.append(item.check_id)
    return result


def confirm_by_request(
    session: TestSession,
    programme: Programme,
    records: Iterable[Any] | None,
    *,
    revision: int | None = None,
    author: str = "",
) -> list[str]:
    """Записывает результаты проверок, подтверждённых запросами этой сессии.

    Нужно для сценария «читаем данные стенда, а протокол наполняется сам»
    (комментарий заказчика к `UC-03`/`UC-04`): успешные обмены, уже сделанные в
    сессии, дают результат проверки со ссылкой на журнал. Проверки, у которых
    результат уже есть, не трогаются (повтор — отдельное действие, `FR-P-36`).

    Returns:
        Идентификаторы проверок, у которых результат записан этим вызовом.
    """
    items = session_records(records, session)
    written: list[str] = []
    for item in programme.items:
        record = results_api.result_of(session, item.check_id)
        if record is not None and str(record.get("status") or "") != results_api.STATUS_NOT_RUN:
            continue
        evidence = evidence_by_request(items, item.check_id)
        if evidence is None:
            continue
        entry: dict[str, Any] = {
            "check_id": item.check_id,
            "status": str(CheckStatus.PASSED),
            "verdict": CONFIRMED_VERDICT,
            "operator_note": evidence["note"],
            "origin": results_api.ORIGIN_REQUEST,
            "evidence": dict(evidence),
            "journal_from": evidence.get("journal_from"),
            "journal_to": evidence.get("journal_to"),
        }
        results_api.record(
            session,
            entry,
            origin=results_api.ORIGIN_REQUEST,
            author=author,
            revision=revision if revision is not None else programme.revision,
            remember=False,
        )
        written.append(item.check_id)
    return written


def restore_state(queue: queue_api.Queue, session: TestSession) -> queue_api.Queue:
    """Восстанавливает состояние пунктов по результатам сессии (`DR-P-5`).

    Очередь в сессии не хранится (v8), поэтому при каждом чтении состояния прогона
    отметки «выполнена» и «снята» берутся из записей результатов: снятие — это
    результат (`FR-P-15`), а не правка состава.
    """
    for item in queue.items:
        record = results_api.result_of(session, item.check_id)
        if record is None:
            continue
        status = str(record.get("status") or "")
        if not status or status == results_api.STATUS_NOT_RUN:
            continue
        suspended = results_api.is_suspended(session, item.check_id)
        queue.finish(
            item.check_id,
            state=queue_api.QUEUE_OFF if suspended else queue_api.QUEUE_DONE,
            journal_from=_int_or_none(record.get("journal_from")),
            journal_to=_int_or_none(record.get("journal_to")),
            note=str(record.get("reason") or record.get("operator_note") or ""),
        )
    return queue


def _int_or_none(value: Any) -> int | None:
    """Целое число или None (для полей диапазона журнала)."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def run_queue(
    session: TestSession,
    programme: Programme,
    records: Iterable[Any] | None = None,
    *,
    revision: int | None = None,
    mode: str | None = None,
    run_mode: str | None = None,
) -> queue_api.Queue:
    """Очередь прогона по ревизии программы для текущего режима сессии.

    Режим «только невыполненное» (`session.skip_done`, по умолчанию) восстанавливает
    состояние по результатам сессии; режим «выполнять всё» возвращает очередь в
    исходном состоянии — все пункты ожидают запуска, повторы пишут историю.

    Args:
        revision: номер ревизии программы (по умолчанию — ревизия сессии).
        mode: режим **исполнения** очереди («сценарием» / «по вызовам», задел M1/M2).
        run_mode: режим **прогона** («only-remaining» / «all»); None — из сессии.
    """
    number = int(revision if revision is not None else session.run_revision or 0)
    queue = queue_api.queue_for_revision(programme, number)
    if mode:
        queue.set_mode(mode)
    chosen = str(run_mode or "") or run_mode_of(session)
    if chosen == RUN_ONLY_REMAINING:
        restore_state(queue, session)
    return queue


def progress(queue: queue_api.Queue, results: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Прогресс прогона: сколько пунктов закрыто и какой запустится следующим."""
    summary = queue.summary(results)
    return {
        "total": summary["total"],
        "done": int(summary["done"]) + int(summary["off"]),
        "pending": len(queue.pending_ids),
        "next": summary["next"],
        "mode": summary["mode"],
        "mode_label": summary["mode_label"],
        "stop_reason": summary["stop_reason"],
    }


def suspension_reason(session: TestSession, check_id: str) -> str:
    """Причина снятия пункта (пустая строка — пункт не снят)."""
    if not results_api.is_suspended(session, check_id):
        return ""
    record = results_api.result_of(session, check_id) or {}
    return str(record.get("reason") or record.get("operator_note") or "")


__all__ = [
    "CONFIRMED_NOTE",
    "CONFIRMED_VERDICT",
    "RUN_ALL",
    "RUN_MODES",
    "RUN_ONLY_REMAINING",
    "SUCCESS_MAX",
    "SUCCESS_MIN",
    "confirm_by_request",
    "confirmed_ids",
    "endpoint_specs",
    "evidence_by_request",
    "is_success",
    "progress",
    "restore_state",
    "run_mode_of",
    "run_queue",
    "session_records",
    "set_run_mode",
    "suspension_reason",
]
