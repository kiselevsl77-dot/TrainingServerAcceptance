"""Исполнение программы испытаний по одной команде и с паузой (пожелания 21.09.2026, п. 2).

Кнопка «Выполнить» отправляет **следующий** включённый пункт плана, кнопка
«Авто с паузой» — идёт по плану с заданным интервалом. Модуль не зависит от
интерфейса: он получает `PlanState`, сессию и `Runtime`, а результат пишет в
пункт плана (и, для уровня «проверка», — в `session.checks` через движок).

Два режима (решение заказчика, вариант C):

    * `MODE_CHECK` — исполняется сценарий проверки: статус и вердикт берутся из
      движка (`acceptance.checks.engine`), дочерние пункты-вызовы закрываются по
      фактическим обменам проверки (метка `TC-…` + диапазон журнала);
    * `MODE_CALL` — отправляется **ровно один** запрос по шаблону пункта
      (метод/путь/query/тело из `PlanItem`), вердикт считается по ожиданию
      (`acceptance.plan.judge_call`), проверка при этом не помечается выполненной:
      в отчёте видно, что она выполнялась «по вызовам».

Остановы авто-прогона: пункт требует подтверждения (класс `live`/`heavy` или
удаляющий вызов), вызов завершился отказом, оператор нажал «Стоп», план кончился.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from acceptance import plan as plan_api
from acceptance.checks import engine as checks_engine
from acceptance.checks import runner as checks_runner
from acceptance.checks.registry import CheckStatus
from acceptance.exchange import execute_request
from acceptance.logging_setup import log_event
from acceptance.plan import (
    LEVEL_CHECK,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    PlanItem,
    PlanState,
    judge_call,
)

#: Статусы движка → статусы пункта плана (уровень «проверка»).
CHECK_STATUS_MAP: dict[str, str] = {
    str(CheckStatus.PASSED): STATUS_PASSED,
    str(CheckStatus.MANUAL_OK): STATUS_PASSED,
    str(CheckStatus.FAILED): STATUS_FAILED,
    str(CheckStatus.INTERRUPTED): STATUS_FAILED,
    str(CheckStatus.BLOCKED): STATUS_BLOCKED,
    str(CheckStatus.SKIPPED): STATUS_SKIPPED,
    str(CheckStatus.NOT_RUN): plan_api.STATUS_PENDING,
}


@dataclass
class ItemRun:
    """Итог исполнения одного пункта плана."""

    item: PlanItem
    status: str
    verdict: str
    detail: str = ""
    journal_from: int | None = None
    journal_to: int | None = None
    duration_ms: float | None = None
    check_result: Any = None
    stop: bool = False

    @property
    def ok(self) -> bool:
        """True, если пункт завершился успехом."""
        return self.status == STATUS_PASSED


def _stop(item: PlanItem, message: str, *, status: str = STATUS_SKIPPED) -> ItemRun:
    """Итог пункта, который на стенд не отправлялся (пропуск или отказ до отправки).

    Вердикт и пояснение совпадают с текстом: оператор видит причину и в ленте плана,
    и в сообщении интерфейса (NFR-T3 — ошибки объясняются, а не «молча» теряются).
    """
    return ItemRun(item.mark(status, verdict=message), status, message, detail=message)


def next_item(state: PlanState) -> PlanItem | None:
    """Следующий включённый пункт плана в текущем режиме."""
    return state.next_item()


def item_needs_confirmation(item: PlanItem) -> tuple[bool, str]:
    """Нужно ли подтверждение оператора перед исполнением пункта (NFR-T4, FR-T10).

    Проверки классов `live`/`heavy` требуют заполненной карточки запуска, а вызовы
    изменяющих/удаляющих операций — подтверждения на экране плана. Авто-прогон
    останавливается, а не отправляет такое молча.
    """
    if item.level == LEVEL_CHECK:
        if item.check_class in ("live", "heavy"):
            return True, f"проверка класса `{item.check_class}`: нужна карточка запуска"
        return False, ""
    if item.is_destructive:
        return True, "удаляющий вызов: нужно подтверждение оператора"
    if item.safety in ("write", "heavy"):
        return True, f"изменяющий/ресурсоёмкий вызов (`{item.safety}`): нужно подтверждение"
    return False, ""


def execute_item(
    state: PlanState,
    *,
    item: PlanItem,
    session: Any,
    runtime: Any,
    monitor: Any = None,
    client: Any = None,
    params: dict[str, Any] | None = None,
    run_evidence: dict[str, Any] | None = None,
    keep_going_on_failure: bool = True,
) -> ItemRun:
    """Исполняет один пункт плана и фиксирует результат в пункте и сессии.

    Args:
        state: состояние плана (пункт будет заменён исполненным).
        item: пункт к исполнению (уровень «проверка» или «вызов»).
        session: сессия испытаний (результаты проверок и история).
        runtime: рантайм пульта (`state.Runtime`): API, журнал, настройки.
        monitor: монитор задач (нужен сценариям FSM-1).
        client: «сырой» клиент консоли для негативных проб и вызовов по шаблону.
        params: параметры запуска (переопределяют `item.params`).
        run_evidence: доказательства подтверждения (карточка запуска).
        keep_going_on_failure: продолжать ли авто-прогон после отказа (по умолчанию да).
    """
    if item.is_check:
        run = _execute_check(
            session=session,
            runtime=runtime,
            monitor=monitor,
            client=client,
            item=item,
            params=params,
            run_evidence=run_evidence,
        )
    else:
        run = _execute_call(
            session=session,
            runtime=runtime,
            client=client,
            item=item,
            params=params,
            label=_call_label(item),
        )

    state.replace_item(run.item)
    state.last_item_id = run.item.item_id
    state.last_at = datetime.now().isoformat(timespec="seconds")
    state.pointer = max(state.pointer, state.index_of(run.item.item_id) + 1)

    if item.is_check:
        run.item = _close_child_calls(state, item=run.item, journal=runtime.journal)

    if not run.ok and not keep_going_on_failure:
        run.stop = True
    return run


def _execute_check(
    *,
    session: Any,
    runtime: Any,
    monitor: Any,
    client: Any,
    item: PlanItem,
    params: dict[str, Any] | None,
    run_evidence: dict[str, Any] | None,
) -> ItemRun:
    """Прогон сценария проверки (уровень «проверка») через движок чек-листа."""
    spec = _spec(item)
    if spec is None:
        return _stop(item, "проверка отсутствует в каталоге")

    context = _context(
        session=session,
        spec=spec,
        runtime=runtime,
        monitor=monitor,
        client=client,
        params={**item.params, **(params or {})},
    )
    result = checks_runner.automate(context)
    if result is None:
        return _stop(item, "у проверки нет автоматического сценария (ручная проверка)")

    if run_evidence:
        result.evidence["run_card"] = dict(run_evidence)
    if result.status == CheckStatus.BLOCKED:
        checks_engine.ensure_defect_note(session, spec, result)
    checks_engine.record_result(session, result)

    status = CHECK_STATUS_MAP.get(str(result.status), STATUS_SKIPPED)
    executed = item.mark(
        status,
        verdict=result.verdict or str(result.status),
        journal_from=result.journal_from,
        journal_to=result.journal_to,
        duration_ms=result.duration_ms,
        started_at=result.started_at,
        ended_at=result.ended_at,
    )
    return ItemRun(
        executed,
        status,
        result.verdict or str(result.status),
        journal_from=result.journal_from,
        journal_to=result.journal_to,
        duration_ms=result.duration_ms,
        check_result=result,
    )


def _execute_call(
    *,
    session: Any,
    runtime: Any,
    client: Any,
    item: PlanItem,
    params: dict[str, Any] | None,
    label: str,
) -> ItemRun:
    """Отправляет **один** запрос по шаблону пункта и оценивает ответ."""
    if client is None:
        return _stop(
            item,
            "клиент консоли недоступен (проверьте адрес стенда в `.env`)",
            status=STATUS_FAILED,
        )
    if item.probe:
        return _stop(item, "негативная проба выполняется сценарием проверки")

    values = {**item.params, **(params or {})}
    path = item.path
    for param in _path_params(item):
        value = str(values.get(param, "")).strip()
        if not value:
            return _stop(
                item,
                f"не задан path-параметр `{param}` — вызов не отправлен",
            )
        path = path.replace(f"{{{param}}}", value)

    query = {
        str(key): str(values[key])
        for key in _query_params(item)
        if str(values.get(key, "")).strip()
    }
    json_body, body_error = _json_body(item, values)
    if body_error:
        return _stop(item, body_error)
    started = datetime.now().isoformat(timespec="seconds")
    before = runtime.journal.peek_next_seq()
    result = execute_request(
        client,
        method=item.method,
        path=path,
        query=query,
        json_body=json_body,
        label=label,
        binary=_is_binary(item),
        timeout=runtime.settings.download_timeout if _is_binary(item) else None,
        body_limit=runtime.config.body_limit,
        journal=runtime.journal,
        keep_content=False,
    )
    verdict = judge_call(
        item,
        status=result.status,
        error=result.error,
        task_id=str((result.body_json or {}).get("id", "")) if result.body_json else "",
    )
    executed = item.mark(
        verdict.status,
        verdict=verdict.verdict,
        journal_from=before,
        journal_to=result.journal_seq,
        duration_ms=result.duration_ms,
        started_at=started,
        ended_at=datetime.now().isoformat(timespec="seconds"),
    )
    log_event(
        "plan_call_executed",
        f"Программа испытаний: {item.operation} → {result.status_label} ({verdict.verdict})",
        level="INFO" if verdict.is_match else "WARNING",
        module="plan",
        check_id=item.parent_id or "",
        payload={
            "item_id": item.item_id,
            "operation": item.operation,
            "expected": item.expected,
            "status": result.status,
            "verdict": verdict.verdict,
            "detail": verdict.detail,
            "journal_seq": result.journal_seq,
            **result.as_dict(),
        },
    )
    return ItemRun(
        executed,
        verdict.status,
        verdict.verdict,
        detail=verdict.detail,
        journal_from=before,
        journal_to=result.journal_seq,
        duration_ms=result.duration_ms,
    )


# ---------------------------------------------------------------------------
# Помощники: контекст сценария, параметры вызова, закрытие вызовов проверки
# ---------------------------------------------------------------------------
def _spec(item: PlanItem) -> Any:
    """Описание проверки каталога по пункту плана."""
    from acceptance.checks import catalog

    return catalog.find(item.item_id)


def _context(
    *, session: Any, spec: Any, runtime: Any, monitor: Any, client: Any, params: dict
) -> Any:
    """Контекст автоматического сценария проверки (без участия интерфейса)."""
    probe = (
        checks_runner.RawProbe(
            client=client,
            journal=runtime.journal,
            body_limit=runtime.config.body_limit,
        )
        if client is not None
        else None
    )
    return checks_runner.AutomationContext(
        session=session,
        spec=spec,
        tasks=runtime.apis.tasks,
        journal=runtime.journal,
        monitor=monitor,
        params=dict(params),
        apis=runtime.apis,
        probe=probe,
    )


def call_label(item: PlanItem) -> str:
    """Метка журнала для вызова плана: id проверки-родителя (как у сценариев)."""
    return item.parent_id or item.item_id


def _call_label(item: PlanItem) -> str:
    """Внутренний псевдоним `call_label` (для читаемости `_execute_call`)."""
    return call_label(item)


def _path_params(item: PlanItem) -> list[str]:
    """Имена path-параметров вызова: из реестра операций либо из шаблона пути."""
    endpoint = plan_endpoint(item)
    if endpoint is not None:
        names = [param.name for param in endpoint.path_params()]
        if names:
            return names
    return [part.split("}")[0] for part in item.path.split("{")[1:]]


def _query_params(item: PlanItem) -> list[str]:
    """Имена query-параметров вызова (из реестра операций)."""
    endpoint = plan_endpoint(item)
    return [param.name for param in endpoint.query_params()] if endpoint is not None else []


def plan_endpoint(item: PlanItem) -> Any:
    """Описание операции реестра по пункту плана (None — для проб и пробелов)."""
    from acceptance import endpoints as ep

    if item.endpoint_key:
        return ep.find(item.endpoint_key)
    return ep.find(f"{item.method.lower()} {item.path}") if item.method else None


def path_params(item: PlanItem) -> list[str]:
    """Имена path-параметров вызова (публичная обёртка для интерфейса)."""
    return _path_params(item)


def query_params(item: PlanItem) -> list[str]:
    """Имена query-параметров вызова (публичная обёртка для интерфейса)."""
    return _query_params(item)


def _json_body(item: PlanItem, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """JSON-тело вызова: из редактора плана или заготовка спецификации.

    Returns:
        Кортеж (тело, текст ошибки). Ошибка заполнения тела — «пропущено»,
        а не «отказ»: оператору нужно поправить параметры пункта.
    """
    from acceptance import endpoints as ep

    if item.body_kind != ep.BODY_JSON:
        return None, ""
    text = str(values.get("body") or item.body_sample or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return None, f"тело пункта не является корректным JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "тело пункта должно быть JSON-объектом"
    return parsed, ""


def _is_binary(item: PlanItem) -> bool:
    """True, если операция отдаёт файл (нужен таймаут скачивания)."""
    endpoint = plan_endpoint(item)
    return bool(endpoint is not None and endpoint.is_binary)


def _close_child_calls(state: PlanState, *, item: PlanItem, journal: Any) -> PlanItem:
    """Закрывает вызовы проверки по фактическим обменам её прогона.

    Проверка выполняется сценарием (внутри — несколько запросов), но в программе
    испытаний её вызовы видны отдельными пунктами: после прогона они получают статус
    по реально ушедшим запросам (диапазон `journal_from`…`journal_to` и метка проверки).
    Так оператор видит, какие вызовы проверки действительно были отправлены, а какие
    сценарий не делал (например, из-за предусловия).
    """
    children = state.calls_of(item.item_id)
    if not children or journal is None:
        return item

    records = [
        record
        for record in journal.records
        if record.label == item.item_id
        and (item.journal_from is None or record.seq >= item.journal_from)
        and (item.journal_to is None or record.seq <= item.journal_to)
    ]
    for call in children:
        matched = [record for record in records if _path_matches(call.path, record.path)]
        if not matched:
            state.replace_item(
                call.mark(
                    STATUS_SKIPPED,
                    verdict="в этом прогоне проверки вызов не выполнялся",
                )
            )
            continue
        last = matched[-1]
        failed = (last.status or 0) >= 400
        state.replace_item(
            call.mark(
                STATUS_FAILED if failed else STATUS_PASSED,
                verdict=(
                    f"ответ {last.status} в прогоне проверки"
                    if last.status is not None
                    else "нет ответа (ошибка соединения)"
                ),
                journal_from=matched[0].seq,
                journal_to=last.seq,
                duration_ms=sum(record.duration_ms for record in matched),
            )
        )
    return item


def _path_matches(template: str, path: str) -> bool:
    """True, если фактический путь соответствует шаблону с `{…}`-параметрами."""
    parts = re.split(r"\{[^}]+\}", template)
    pattern = "".join(re.escape(part) + "[^/]+" for part in parts[:-1]) + re.escape(parts[-1])
    return re.fullmatch(pattern, path) is not None
