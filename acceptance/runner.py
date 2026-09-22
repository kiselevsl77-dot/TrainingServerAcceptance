"""Исполнение очереди: одна команда «▶ Следующая: `TC-…`» (`FR-P-16`, `IR-P-4`).

Единственная команда запуска пульта отправляет **ровно один следующий пункт**
очереди и называет его идентификатором. Обойти пункт нельзя: чтобы пропустить его,
нужно снять пункт с причиной или выпустить новую ревизию программы (`FR-P-16`).

Режимы (`Q2` — сохраняем оба):

* `MODE_CHECK` («сценарием») — исполняется сценарий проверки целиком; статус,
  вердикт и диапазон журнала берутся из результата движка, пункт закрывается;
* `MODE_CALL` («по вызовам») — отправляется **один** вызов проверки (операция
  реестра), пункт остаётся «следующей», пока не отправлен последний вызов.
  Результат проверки при этом **не ставится** (`A2`): след виден только в журнале
  обмена по метке `TC-…`.

Остановы авто-прогона (`A3`): пункт требует карточки запуска (`live`/`heavy`),
пункт ручной (`manual`), «Стоп» оператора, конец очереди. Отказ проверки
авто-прогон **не** останавливает — он фиксируется и виден в протоколе.

Очередь и результат не дублируются: состав и порядок — в `queue.py` (снимок
ревизии программы), статус и вердикт — в `results.py` (`DR-P-5`), а этот модуль
соединяет их со сценариями проверок и обменом.

Помощники оценки вызова (`parse_operation`, `expected_rule`, `expected_statuses`,
`judge_response`, `path_params`, `query_params`, `plan_endpoint`) перенесены сюда
из `plan.py` копией: новый исполнитель не должен зависеть от модуля, который
удаляется на этапе 2 (журнал решений `llm_tasks/23.2`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from acceptance import endpoints as ep
from acceptance import results as results_api
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks import runner as checks_runner
from acceptance.checks.registry import CheckClass, CheckSpec, CheckStatus
from acceptance.exchange import execute_request
from acceptance.logging_setup import log_event
from acceptance.programme import Programme, ProgrammeItem
from acceptance.queue import MODE_CALL, QUEUE_DONE, Queue, QueueItem

#: Уровень шага прогона: пункт целиком («проверка») или его вызов.
LEVEL_CHECK = "проверка"
LEVEL_CALL = "вызов"

#: Итоги шага (в режиме «по вызовам» — статус самого вызова).
STATUS_PASSED = str(CheckStatus.PASSED)
STATUS_FAILED = str(CheckStatus.FAILED)
STEP_SKIPPED = "пропущено"

STEP_ICONS: dict[str, str] = {
    STATUS_PASSED: "✅",
    STATUS_FAILED: "❌",
    STEP_SKIPPED: "⏭",
}

#: Вердикты соответствия ожиданию (то, что видит комиссия).
VERDICT_MATCH = "соответствует ожиданию"
VERDICT_MISMATCH = "не соответствует ожиданию"
VERDICT_NOT_RUN = "вызов не выполнен"

VERDICT_ICONS: dict[str, str] = {
    VERDICT_MATCH: "✅",
    VERDICT_MISMATCH: "❌",
    VERDICT_NOT_RUN: "⛔",
}


@dataclass(frozen=True)
class CallStep:
    """Шаг режима «по вызовам»: одна операция реестра из описания проверки."""

    index: int
    method: str
    path: str
    summary: str = ""
    safety: str = ""
    body_kind: str = ""
    check_class: str = str(CheckClass.TECH)
    endpoint_key: str = ""
    expected: str = ""
    body_sample: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def operation(self) -> str:
        """Подпись операции: «POST /api/data/file»."""
        return f"{self.method} {self.path}" if self.method else self.path

    @property
    def is_heavy(self) -> bool:
        """True, если операция ресурсоёмка (ожидается `202` и `task_id`)."""
        return self.safety == str(ep.Safety.HEAVY) or self.check_class == str(CheckClass.HEAVY)

    def to_dict(self) -> dict[str, Any]:
        """Сериализует шаг (для предпросмотра «что будет отправлено»)."""
        payload = dict(self.__dict__)
        payload["params"] = dict(self.params)
        payload["operation"] = self.operation
        return payload


@dataclass(frozen=True)
class CallVerdict:
    """Оценка одного вызова: статус шага, вердикт и пояснение."""

    status: str
    verdict: str
    detail: str
    expected: tuple[int, ...] = ()

    @property
    def is_match(self) -> bool:
        """True, если вызов соответствует ожиданию."""
        return self.verdict == VERDICT_MATCH

    @property
    def icon(self) -> str:
        """Иконка вердикта."""
        return VERDICT_ICONS.get(self.verdict, "⛔")


def parse_operation(text: str) -> tuple[str, str]:
    """Разбирает строку операции (`get /api/data/files`) в метод и путь."""
    parts = str(text).strip().split(maxsplit=1)
    if len(parts) != 2:
        return "", str(text).strip()
    return parts[0].upper(), parts[1]


def expected_rule(*, safety: str = "", heavy: bool = False) -> str:
    """Правило ожидания для вызова: по чему считается «соответствует ожиданию»."""
    if heavy:
        return "202 и `task_id` в ответе"
    if safety == str(ep.Safety.DESTRUCTIVE):
        return "2xx — удаление выполнено"
    if safety == str(ep.Safety.WRITE):
        return "2xx — изменение принято"
    return "2xx — чтение выполнено"


def expected_statuses(step: CallStep) -> tuple[int, ...]:
    """Какие коды ответа считаются ожидаемыми для шага."""
    if step.is_heavy:
        return (202, 200, 201)
    return (200, 201, 202, 204)


def judge_response(
    step: CallStep,
    *,
    status: int | None,
    error: str = "",
    task_id: str = "",
) -> CallVerdict:
    """Считает «соответствие ожиданию» для вызова (`FR-P-29`).

    Отдельно отмечается известный дефект: `fill` отвечает `202`, но `task_id`
    не возвращает — задача ищется в `GET /api/tasks/`. Если ответа нет вовсе,
    вердикт «вызов не выполнен»: сетевой сбой отличается от неверного статуса.
    """
    allowed = expected_statuses(step)
    if status is None:
        return CallVerdict(
            STATUS_FAILED,
            VERDICT_NOT_RUN,
            error or "ответ не получен (сеть, таймаут или запрос не отправлен)",
            allowed,
        )
    if status not in allowed:
        return CallVerdict(
            STATUS_FAILED,
            VERDICT_MISMATCH,
            f"ответ {status}, ожидалось {', '.join(str(code) for code in allowed)}",
            allowed,
        )
    if step.is_heavy and status == 202 and not task_id:
        return CallVerdict(
            STATUS_PASSED,
            VERDICT_MATCH,
            "ответ 202, но `task_id` не возвращён (известный дефект: задача ищется "
            "в `GET /api/tasks/`)",
            allowed,
        )
    return CallVerdict(STATUS_PASSED, VERDICT_MATCH, f"ответ {status}", allowed)


def plan_endpoint(step: CallStep) -> Any:
    """Описание операции реестра по шагу (None — если операции в реестре нет)."""
    if step.endpoint_key:
        return ep.find(step.endpoint_key)
    return ep.find(f"{step.method.lower()} {step.path}") if step.method else None


def path_params(step: CallStep) -> list[str]:
    """Имена path-параметров вызова (из реестра либо из шаблона пути)."""
    endpoint = plan_endpoint(step)
    if endpoint is not None:
        names = [param.name for param in endpoint.path_params()]
        if names:
            return names
    return [part.split("}")[0] for part in step.path.split("{")[1:]]


def query_params(step: CallStep) -> list[str]:
    """Имена query-параметров вызова (из реестра операций)."""
    endpoint = plan_endpoint(step)
    return [param.name for param in endpoint.query_params()] if endpoint is not None else []


def _default_params(endpoint: ep.EndpointSpec | None) -> dict[str, Any]:
    """Значения параметров по умолчанию: enum-подстановки и значения пути."""
    if endpoint is None:
        return {}
    values: dict[str, Any] = {}
    for param in endpoint.params:
        if param.is_path or param.default or param.enum:
            values[param.name] = param.default or (param.enum[0] if param.enum else "")
    return values


def call_steps(spec: CheckSpec) -> list[CallStep]:
    """Шаги режима «по вызовам»: операции реестра из описания проверки.

    Негативные пробы (`probe_paths`) шагами не являются: они подтверждают
    отсутствие маршрута и выполняются сценарием проверки, а вручную — консолью
    (`SCR-501`). Так же вёл себя прежний планировщик (удалён на этапе 2 big bang).
    """
    steps: list[CallStep] = []
    for index, target in enumerate(spec.endpoints, start=1):
        method, path = parse_operation(target)
        endpoint = ep.find(f"{method.lower()} {path}") if method else None
        safety = str(endpoint.safety) if endpoint is not None else str(ep.Safety.DESTRUCTIVE)
        heavy = spec.check_class == CheckClass.HEAVY
        steps.append(
            CallStep(
                index=index,
                method=method,
                path=path,
                summary=endpoint.summary if endpoint is not None else "",
                safety=safety,
                body_kind=endpoint.body_kind if endpoint is not None else ep.BODY_NONE,
                check_class=str(spec.check_class),
                endpoint_key=endpoint.key if endpoint is not None else "",
                expected=expected_rule(safety=safety, heavy=heavy),
                body_sample=endpoint.body_sample if endpoint is not None else "",
                params=_default_params(endpoint),
            )
        )
    return steps


# ---------------------------------------------------------------------------
# Готовность прогона: что запустится следующим и что для этого нужно
# ---------------------------------------------------------------------------
@dataclass
class RunOutcome:
    """Итог шага прогона: чем закончился пункт (или его вызов)."""

    check_id: str = ""
    level: str = LEVEL_CHECK
    state: str = ""
    status: str = ""
    verdict: str = ""
    detail: str = ""
    journal_from: int | None = None
    journal_to: int | None = None
    duration_ms: float | None = None
    call_index: int = 0
    steps: int = 0
    repeats: int = 0
    item: QueueItem | None = None
    check_result: Any = None
    stopped: bool = False
    stop_reason: str = ""
    confirmation_required: bool = False
    operator_required: bool = False

    @property
    def ok(self) -> bool:
        """True, если шаг завершился успехом."""
        return self.status == STATUS_PASSED

    @property
    def icon(self) -> str:
        """Иконка итога шага."""
        return STEP_ICONS.get(self.status, "⚪")

    @property
    def journal_range(self) -> str:
        """Диапазон номеров журнала обмена: «#214–#217»."""
        if self.journal_from is None and self.journal_to is None:
            return ""
        start = "" if self.journal_from is None else f"#{self.journal_from}"
        finish = "" if self.journal_to is None else f"#{self.journal_to}"
        return f"{start}–{finish}".strip("–")

    def summary(self) -> dict[str, Any]:
        """Итог шага для интерфейса и журнала приложения."""
        return {
            "check_id": self.check_id,
            "level": self.level,
            "state": self.state,
            "status": self.status,
            "verdict": self.verdict,
            "detail": self.detail,
            "journal_range": self.journal_range,
            "duration_ms": self.duration_ms,
            "call_index": self.call_index,
            "steps": self.steps,
            "repeats": self.repeats,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
        }


def next_check_id(queue: Queue) -> str:
    """Идентификатор пункта, который запустит «Следующая» (пусто — очередь пройдена)."""
    following = queue.next_item()
    return "" if following is None else following.check_id


def next_label(queue: Queue) -> str:
    """Подпись единственной команды запуска: «▶ Следующая: `TC-FILE-13`» (`IR-P-20`)."""
    check_id = next_check_id(queue)
    return f"▶ Следующая: {check_id}" if check_id else "▶ Следующая"


def needs_confirmation(item: ProgrammeItem) -> tuple[bool, str]:
    """Нужна ли карточка запуска перед прогоном пункта (`FR-P-19`, `NFR-P-5`).

    Проверки классов `live` (реальные данные) и `heavy` (ресурсоёмкие) без
    заполненной карточки запуска не запускаются: расход ресурсов и используемые
    данные подтверждает оператор.
    """
    if item.check_class in (str(CheckClass.LIVE), str(CheckClass.HEAVY)):
        return True, f"проверка класса `{item.check_class}`: нужна карточка запуска"
    return False, ""


def requires_operator(item: ProgrammeItem) -> tuple[bool, str]:
    """Нужен ли человек: ручные проверки автоматически не выполняются (`FR-P-32`)."""
    if item.check_class == str(CheckClass.MANUAL):
        return True, "ручная проверка: нужна отметка оператора с заключением"
    return False, ""


def can_run(queue: Queue, programme: Programme) -> tuple[bool, str]:
    """Готовность прогона: можно ли нажимать «Следующая» (пустая строка — можно)."""
    if programme.size == 0 or queue.is_empty:
        return False, (
            "программа не собрана: выберите наборы и утвердите программу "
            "(«Программа сессии», SCR-102)"
        )
    if not queue.is_current(programme):
        return False, (
            f"очередь собрана по ревизии {queue.revision}, а программа — ревизия "
            f"{programme.revision}: пересоберите очередь на SCR-102"
        )
    if queue.next_item() is None:
        return False, "очередь пройдена: осталось оформить протокол и отчёт"
    return True, ""


def programme_notice(queue: Queue) -> str:
    """Баннер программы для экрана «Прогон»: чем помечен текущий прогон (`A1`)."""
    if queue.is_empty:
        return (
            "Очередь пуста: соберите и утвердите программу на экране «Программа сессии» (SCR-102)"
        )
    if queue.draft:
        return (
            f"Прогон идёт по черновику программы (ревизия {queue.revision}): "
            "он помечен в протоколе и отчёте"
        )
    return f"Программа утверждена (ревизия {queue.revision})"


def batch_tech_ids(queue: Queue, programme: Programme) -> list[str]:
    """Что заберёт «▶▶ Пачка tech»: ожидающие `tech`-пункты до первого не-`tech`.

    Пачка останавливается на первом пункте, требующем карточки запуска или
    оператора (`A4`): после изменяющей проверки контекст стенда меняется, и решение
    принимает человек (в прототипе пачка такие пункты «перепрыгивала» — выбран
    безопасный вариант, см. журнал решений `llm_tasks/23.2`).
    """
    ids: list[str] = []
    for item in queue.items:
        if not item.is_pending:
            continue
        found = programme.find(item.check_id)
        if found is None or found.check_class != str(CheckClass.TECH):
            break
        ids.append(item.check_id)
    return ids


def _context(
    *,
    session: Any,
    spec: CheckSpec,
    runtime: Any,
    monitor: Any,
    client: Any,
    params: dict[str, Any],
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


def _skip_item(
    queue: Queue,
    session: Any,
    item: QueueItem,
    programme: Programme,
    reason: str,
) -> RunOutcome:
    """Закрывает пункт статусом «пропущена» с пояснением: иначе его не проверить."""
    queue.begin(item.check_id)
    results_api.record(
        session,
        {
            "check_id": item.check_id,
            "status": str(CheckStatus.SKIPPED),
            "verdict": reason,
            "operator_note": reason,
        },
        revision=programme.revision,
    )
    finished = queue.finish(item.check_id, note=reason)
    log_event(
        "queue_item_skipped",
        f"Пункт {item.check_id} пропущен: {reason}",
        level="WARNING",
        module="runner",
        check_id=item.check_id,
    )
    return RunOutcome(
        check_id=item.check_id,
        state=QUEUE_DONE,
        status=str(CheckStatus.SKIPPED),
        verdict=reason,
        detail=reason,
        item=finished,
    )


# ---------------------------------------------------------------------------
# Прогон пункта: «сценарием» (проверка целиком) и «по вызовам»
# ---------------------------------------------------------------------------
def _target(queue: Queue, check_id: str | None) -> QueueItem | None:
    """Пункт для прогона: указанный оператором или «следующий» по программе."""
    return queue.find(check_id) if check_id else queue.next_item()


def execute_check(
    queue: Queue,
    programme: Programme,
    session: Any,
    runtime: Any,
    *,
    check_id: str | None = None,
    monitor: Any = None,
    client: Any = None,
    params: dict[str, Any] | None = None,
    run_evidence: dict[str, Any] | None = None,
    author: str = "",
) -> RunOutcome:
    """Прогон пункта целиком: сценарий проверки, результат и история повтора.

    Args:
        queue: очередь прогона (пункт будет переведён в «выполняется», затем закрыт).
        programme: программа сессии (состав, класс проверки, ревизия).
        session: сессия испытаний (единственный след результата, `DR-P-5`).
        runtime: рантайм пульта (`ui.state.Runtime`): API, журнал, настройки.
        check_id: конкретный пункт (по умолчанию — «следующий»).
        monitor: монитор задач (нужен сценариям FSM-1).
        client: «сырой» клиент консоли (нужен негативным пробам проверки).
        params: параметры запуска, дополняющие параметры пункта.
        run_evidence: подтверждение карточки запуска (`FR-P-19`).
        author: исполнитель (попадёт в протокол).

    Returns:
        Итог шага. `confirmation_required`/`operator_required` означают, что запрос
        на стенд **не отправлялся**: интерфейс показывает карточку запуска или
        предложение выполнить проверку вручную.
    """
    item = _target(queue, check_id)
    if item is None:
        reason = "очередь пройдена: осталось оформить протокол и отчёт"
        queue.stop_reason = reason
        return RunOutcome(detail=reason, stopped=True, stop_reason=reason)

    pitem = programme.find(item.check_id)
    if pitem is None:
        reason = f"пункт {item.check_id} отсутствует в программе: прогон остановлен"
        queue.stop_reason = reason
        return RunOutcome(
            check_id=item.check_id,
            state=item.state,
            detail=reason,
            stopped=True,
            stop_reason=reason,
            item=item,
        )

    need, note = needs_confirmation(pitem)
    if need and not run_evidence:
        reason = f"{item.check_id}: {note}"
        queue.stop_reason = reason
        return RunOutcome(
            check_id=item.check_id,
            state=item.state,
            detail=reason,
            stopped=True,
            stop_reason=reason,
            confirmation_required=True,
            item=item,
        )

    need, note = requires_operator(pitem)
    if need:
        reason = f"{item.check_id}: {note}"
        queue.stop_reason = reason
        return RunOutcome(
            check_id=item.check_id,
            state=item.state,
            detail=reason,
            stopped=True,
            stop_reason=reason,
            operator_required=True,
            item=item,
        )

    spec = catalog.find(item.check_id)
    if spec is None:
        return _skip_item(
            queue,
            session,
            item,
            programme,
            "проверка отсутствует в каталоге: исполнить её нельзя",
        )

    queue.begin(item.check_id)
    # прежний результат уходит в историю, новый результат движка ляжет поверх (FR-P-36)
    results_api.prepare_repeat(session, item.check_id)
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
        return _skip_item(
            queue,
            session,
            item,
            programme,
            "у проверки нет автоматического сценария (ручная проверка)",
        )
    if run_evidence:
        result.evidence["run_card"] = dict(run_evidence)
    if result.status == CheckStatus.BLOCKED:
        checks_engine.ensure_defect_note(session, spec, result)
    # результат уже записан движком: дополняем его полями пульта, историю не трогаем
    results_api.annotate(session, result, author=author, revision=programme.revision)
    finished = queue.finish(
        item.check_id,
        journal_from=result.journal_from,
        journal_to=result.journal_to,
        note=result.verdict,
    )
    repeats = results_api.repeats(session, item.check_id)
    log_event(
        "queue_check_executed",
        f"Прогон: {item.check_id} → {result.status} ({result.verdict})",
        level="INFO" if result.status == CheckStatus.PASSED else "WARNING",
        module="runner",
        check_id=item.check_id,
        payload={
            "repeats": repeats,
            "journal_from": result.journal_from,
            "journal_to": result.journal_to,
            "revision": programme.revision,
        },
    )
    return RunOutcome(
        check_id=item.check_id,
        level=LEVEL_CHECK,
        state=QUEUE_DONE,
        status=str(result.status),
        verdict=result.verdict or str(result.status),
        detail=result.verdict,
        journal_from=result.journal_from,
        journal_to=result.journal_to,
        duration_ms=result.duration_ms,
        repeats=repeats,
        item=finished,
        check_result=result,
    )


def _call_stop(
    queue: Queue,
    item: QueueItem,
    step: CallStep,
    steps: int,
    reason: str,
    *,
    status: str = STEP_SKIPPED,
) -> RunOutcome:
    """Шаг не отправлен: пункт остаётся ожидающим, причина видна оператору.

    В режиме «по вызовам» вызов не отправляется, пока не заданы параметры: пункт
    не закрывается (чтобы оператор поправил параметры и повторил команду), а
    `call_index` не двигается.
    """
    item.note = reason
    return RunOutcome(
        check_id=item.check_id,
        level=LEVEL_CALL,
        state=item.state,
        status=status,
        verdict=reason,
        detail=reason,
        call_index=item.call_index,
        steps=steps,
        item=item,
        stopped=True,
        stop_reason=reason,
    )


def _json_body(step: CallStep, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """JSON-тело вызова: из параметров пункта либо заготовка спецификации.

    Returns:
        Кортеж (тело, текст ошибки). Ошибка заполнения тела — «пропущено», а не
        «отказ»: оператору нужно поправить параметры пункта.
    """
    if step.body_kind != ep.BODY_JSON:
        return None, ""
    text = str(values.get("body") or step.body_sample or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return None, f"тело пункта не является корректным JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "тело пункта должно быть JSON-объектом"
    return parsed, ""


def _is_binary(step: CallStep) -> bool:
    """True, если операция отдаёт файл (нужен таймаут скачивания)."""
    endpoint = plan_endpoint(step)
    return bool(endpoint is not None and endpoint.is_binary)


def execute_call(
    queue: Queue,
    programme: Programme,
    session: Any,
    runtime: Any,
    *,
    check_id: str | None = None,
    client: Any = None,
    params: dict[str, Any] | None = None,
    run_evidence: dict[str, Any] | None = None,
) -> RunOutcome:
    """Один вызов проверки (`MODE_CALL`): результат проверки **не ставится** (`A2`).

    Пункт остаётся «следующей», пока не отправлен последний вызов; прогресс виден
    в примечании пункта («вызов 2/5»). След вызова — запись журнала обмена с меткой
    проверки: протокол показывает, что проверка выполнялась по вызовам, но статус
    проверки ставит только сценарий или оператор.

    Изменяющие и ресурсоёмкие вызовы требуют подтверждения (`FR-P-19`): без
    заполненной карточки запуска запрос не отправляется.

    Returns:
        Итог шага. `stopped` вместе со статусом «пропущено» означает, что вызов
        не отправлен (нет параметров или клиента): пункт закрыт не будет.
    """
    item = _target(queue, check_id)
    if item is None:
        reason = "очередь пройдена: осталось оформить протокол и отчёт"
        queue.stop_reason = reason
        return RunOutcome(level=LEVEL_CALL, detail=reason, stopped=True, stop_reason=reason)

    pitem = programme.find(item.check_id)
    if pitem is None:
        reason = f"пункт {item.check_id} отсутствует в программе: прогон остановлен"
        queue.stop_reason = reason
        return RunOutcome(
            check_id=item.check_id,
            level=LEVEL_CALL,
            state=item.state,
            detail=reason,
            stopped=True,
            stop_reason=reason,
            item=item,
        )

    spec = catalog.find(item.check_id)
    if spec is None:
        return _skip_item(
            queue,
            session,
            item,
            programme,
            "проверка отсутствует в каталоге: исполнить её нельзя",
        )

    steps = call_steps(spec)
    if not steps:
        return _skip_item(
            queue,
            session,
            item,
            programme,
            "у проверки нет операций для пошагового прогона: выполните её сценарием",
        )

    index = min(max(item.call_index, 0), len(steps) - 1)
    step = steps[index]

    if client is None:
        return _call_stop(
            queue,
            item,
            step,
            len(steps),
            "клиент консоли недоступен (проверьте адрес стенда в `.env`)",
            status=STATUS_FAILED,
        )

    values = {**step.params, **item.params, **(params or {})}
    path = step.path
    for param in path_params(step):
        value = str(values.get(param, "")).strip()
        if not value:
            return _call_stop(
                queue,
                item,
                step,
                len(steps),
                f"не задан path-параметр `{param}` — вызов не отправлен",
            )
        path = path.replace(f"{{{param}}}", value)

    query = {
        str(key): str(values[key]) for key in query_params(step) if str(values.get(key, "")).strip()
    }
    body, body_error = _json_body(step, values)
    if body_error:
        return _call_stop(queue, item, step, len(steps), body_error)

    need, note = needs_confirmation(pitem)
    if need and not run_evidence:
        reason = f"{item.check_id}: {note}"
        queue.stop_reason = reason
        return RunOutcome(
            check_id=item.check_id,
            level=LEVEL_CALL,
            state=item.state,
            detail=reason,
            call_index=item.call_index,
            steps=len(steps),
            stopped=True,
            stop_reason=reason,
            confirmation_required=True,
            item=item,
        )

    binary = _is_binary(step)
    before = runtime.journal.peek_next_seq()
    result = execute_request(
        client,
        method=step.method,
        path=path,
        query=query,
        json_body=body,
        label=item.check_id,
        binary=binary,
        timeout=runtime.settings.download_timeout if binary else None,
        body_limit=runtime.config.body_limit,
        journal=runtime.journal,
        keep_content=False,
    )
    task_id = str((result.body_json or {}).get("id", "")) if result.body_json else ""
    verdict = judge_response(step, status=result.status, error=result.error, task_id=task_id)

    item.call_index = index + 1
    if item.journal_from is None:
        item.journal_from = before
    if result.journal_seq is not None:
        item.journal_to = result.journal_seq
    queue.last_check_id = item.check_id
    item.note = f"вызов {item.call_index}/{len(steps)}: {verdict.verdict}"
    if item.call_index >= len(steps):
        queue.finish(
            item.check_id,
            journal_from=item.journal_from,
            journal_to=item.journal_to,
            note=(
                f"выполнено по вызовам ({len(steps)}/{len(steps)}): результат проверки не ставится"
            ),
        )
    log_event(
        "queue_call_executed",
        f"Прогон по вызовам: {item.check_id} → {step.operation} ({verdict.verdict})",
        level="INFO" if verdict.is_match else "WARNING",
        module="runner",
        check_id=item.check_id,
        payload={
            "operation": step.operation,
            "expected": step.expected,
            "status": result.status,
            "verdict": verdict.verdict,
            "call_index": item.call_index,
            "steps": len(steps),
            "journal_seq": result.journal_seq,
        },
    )
    return RunOutcome(
        check_id=item.check_id,
        level=LEVEL_CALL,
        state=item.state,
        status=verdict.status,
        verdict=verdict.verdict,
        detail=verdict.detail,
        journal_from=before,
        journal_to=result.journal_seq,
        duration_ms=result.duration_ms,
        call_index=item.call_index,
        steps=len(steps),
        item=item,
    )


def execute_next(
    queue: Queue,
    programme: Programme,
    session: Any,
    runtime: Any,
    *,
    check_id: str | None = None,
    monitor: Any = None,
    client: Any = None,
    params: dict[str, Any] | None = None,
    run_evidence: dict[str, Any] | None = None,
    author: str = "",
) -> RunOutcome:
    """«▶ Следующая»: прогон одного пункта в текущем режиме очереди (`IR-P-4`)."""
    if queue.mode == MODE_CALL:
        return execute_call(
            queue,
            programme,
            session,
            runtime,
            check_id=check_id,
            client=client,
            params=params,
            run_evidence=run_evidence,
        )
    return execute_check(
        queue,
        programme,
        session,
        runtime,
        check_id=check_id,
        monitor=monitor,
        client=client,
        params=params,
        run_evidence=run_evidence,
        author=author,
    )


def run_batch_tech(
    queue: Queue,
    programme: Programme,
    session: Any,
    runtime: Any,
    *,
    limit: int | None = None,
    monitor: Any = None,
    client: Any = None,
    params: dict[str, Any] | None = None,
    run_evidence: dict[str, Any] | None = None,
    author: str = "",
) -> list[RunOutcome]:
    """«▶▶ Пачка tech»: безопасные проверки подряд, без карточек запуска (`A4`).

    Пункты берутся в порядке программы (`batch_tech_ids`). Отказ проверки пачку
    **не** прерывает: он фиксируется, и оператор видит его в протоколе. Пачка
    останавливается, если пункт требует подтверждения, оператора или клиента
    (причина останова — в последнем итоге).
    """
    outcomes: list[RunOutcome] = []
    for check_id in batch_tech_ids(queue, programme):
        if limit is not None and len(outcomes) >= limit:
            break
        outcome = execute_check(
            queue,
            programme,
            session,
            runtime,
            check_id=check_id,
            monitor=monitor,
            client=client,
            params=params,
            run_evidence=run_evidence,
            author=author,
        )
        outcomes.append(outcome)
        if outcome.stopped:
            break
    return outcomes


def auto_stop_reason(queue: Queue, programme: Programme, *, stop_requested: bool = False) -> str:
    """Почему авто-прогон должен остановиться (пустая строка — можно продолжать)."""
    if stop_requested:
        return "авто-прогон остановлен оператором"
    following = queue.next_item()
    if following is None:
        return "очередь пройдена: осталось оформить протокол и отчёт"
    if programme.find(following.check_id) is None:
        return f"пункт {following.check_id} отсутствует в программе: авто-прогон остановлен"
    return ""


def auto_step(
    queue: Queue,
    programme: Programme,
    session: Any,
    runtime: Any,
    *,
    stop_requested: bool = False,
    monitor: Any = None,
    client: Any = None,
    params: dict[str, Any] | None = None,
    run_evidence: dict[str, Any] | None = None,
    author: str = "",
) -> RunOutcome | None:
    """Один шаг авто-прогона с паузой (`A3`): None — прогон остановлен.

    Пауза между шагами живёт в интерфейсе: здесь ровно один шаг, поэтому правила
    останова проверяются тестами без ожидания по времени. Отказ проверки авто-прогон
    не останавливает, а пункт с карточкой запуска или ручной — останавливает:
    итог возвращается с `confirmation_required`/`operator_required`, и интерфейс
    показывает карточку запуска либо предложение отметить проверку вручную.
    """
    reason = auto_stop_reason(queue, programme, stop_requested=stop_requested)
    if reason:
        queue.stop_reason = reason
        return None
    queue.stop_reason = ""
    outcome = execute_next(
        queue,
        programme,
        session,
        runtime,
        monitor=monitor,
        client=client,
        params=params,
        run_evidence=run_evidence,
        author=author,
    )
    if outcome.stopped:
        queue.stop_reason = outcome.stop_reason or outcome.detail
    return outcome
