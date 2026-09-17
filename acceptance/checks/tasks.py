"""Автоматические сценарии проверок группы `TC-TASK` (FR-T4, этап T4).

Модуль выполняет **дешёвые** проверки задач без участия оператора
(`TC-TASK-02/03/06/07`) и формирует вердикты для проверок, которые оператор ведёт
руками на экране «Задачи» (`TC-TASK-01/04/05/08`): вердикт строится по
фактической истории наблюдения FSM-1 из сессии, а не по предположениям.

Все сценарии возвращают `CheckOutcome` (статус, вердикт, доказательства) и
проходят через движок (`acceptance.checks.engine`): проверка получает метку
`TC-TASK-NN`, диапазон записей журнала и запись в `session.checks`.

Правило испытаний (NFR-T4): изменяющие операции (`pause`/`resume`/`interrupt`)
выполняются только осознанно и только с задачей `celery-test`; сами сценарии
проверок ничего не запускают — запуск делает оператор с карточкой запуска.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from acceptance.checks.engine import record_result as save_result
from acceptance.checks.engine import start_check
from acceptance.checks.registry import CheckResult, CheckSpec, CheckStatus
from acceptance.http_log import Journal
from acceptance.session import ORIGIN_EXTERNAL, TestSession, task_history
from acceptance.tasks_monitor import (
    TASK_COMMANDS,
    TaskMonitor,
    current_status,
    parse_status,
    status_label,
)
from client.errors import (
    ApiError,
    ClientError,
    NotFoundError,
    ServerUnavailableError,
    ValidationApiError,
)
from client.schemas import CeleryTask, TaskStatus, TaskType, TaskWithRuntimes
from client.tasks import TasksApi
from lib.period import date_time_bound, day_bounds

#: Задача для проверки «неизвестная задача», если оператор не задал свою (BR-R7).
DEFAULT_UNKNOWN_TASK = "00000000-0000-4000-8000-000000000000"

#: Предел `limit` в пробе «список целиком» (пульт читает список полностью:
#: `acceptance.ui.state.LOAD_TASK_CAP`; спецификация верхнюю границу не объявляет).
LIST_PROBE_LIMIT = 500


@dataclass
class CheckOutcome:
    """Итог автоматического сценария: статус, вердикт, доказательства."""

    status: CheckStatus
    verdict: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def is_passed(self) -> bool:
        """True, если проверка пройдена."""
        return self.status == CheckStatus.PASSED


class PreconditionError(Exception):
    """Проверку невозможно выполнить: не задано предусловие (нет задачи, нет монитора).

    Отличается от «отказа» проверки: оператору нужно выбрать задачу, а не разбираться
    с дефектом API. Такие проверки получают статус «пропущена».
    """


@dataclass
class AutomationContext:
    """Что нужно сценарию: сессия, описание проверки, API задач, монитор, параметры."""

    session: TestSession
    spec: CheckSpec
    tasks: TasksApi
    journal: Journal | None = None
    monitor: TaskMonitor | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def task_id(self) -> str:
        """Задача, с которой работает сценарий (`task_id` из параметров)."""
        return str(self.params.get("task_id") or "").strip()

    @property
    def action(self) -> str:
        """Команда FSM-1 для сценария идемпотентности (`pause` по умолчанию)."""
        return str(self.params.get("action") or "pause").strip().lower()

    def require_task(self) -> str:
        """`task_id` для сценария.

        Raises:
            PreconditionError: если задача не задана — оператор не выбрал её в карточке.
        """
        if not self.task_id:
            raise PreconditionError(
                "не выбран task_id: откройте карточку задачи на экране «Задачи»"
            )
        return self.task_id

    def require_monitor(self) -> TaskMonitor:
        """Монитор для сценария.

        Raises:
            PreconditionError: если монитор недоступен (стенд не настроен).
        """
        if self.monitor is None:
            raise PreconditionError("монитор задач недоступен: не задан адрес испытуемого сервера")
        return self.monitor


def observed_chain(session: TestSession, task_id: str) -> list[str]:
    """Цепочка статусов задачи по истории наблюдения (пустые значения отброшены)."""
    return [
        str(item.get("status"))
        for item in task_history(session, task_id)
        if str(item.get("status") or "").strip()
    ]


def chain_outcome(
    chain: list[str],
    expected: tuple[str, ...],
    *,
    optional: tuple[str, ...] = (),
    note: str = "",
) -> CheckOutcome:
    """Сравнивает наблюдаемую цепочку FSM-1 с ожидаемой (порядок сохраняется).

    Args:
        chain: фактически наблюдаемые статусы (по истории задачи).
        expected: ожидаемая цепочка переходов (порядок важен, повторы допустимы).
        optional: статусы, которые могут не попасть в опрос из-за его шага.
        note: дополнительное пояснение к вердикту.
    """
    index = 0
    missing: list[str] = []
    for status in expected:
        if status in chain[index:]:
            index = chain.index(status, index) + 1
        else:
            missing.append(status)

    required_missing = [status for status in missing if status not in optional]
    optional_missing = [status for status in missing if status in optional]
    required_missing = list(dict.fromkeys(required_missing))
    optional_missing = list(dict.fromkeys(optional_missing))
    shown = " → ".join(status_label(status) for status in chain) or "нет наблюдений"
    evidence = {
        "observed": chain,
        "expected": list(expected),
        "missing": missing,
    }

    if required_missing:
        return CheckOutcome(
            CheckStatus.FAILED,
            "не наблюдались переходы: "
            + ", ".join(status_label(status) for status in required_missing)
            + f" (наблюдалось: {shown})",
            evidence,
            note,
        )

    verdict = f"наблюдались переходы: {shown}"
    if optional_missing:
        verdict += (
            " · не попали в опрос: "
            + ", ".join(status_label(status) for status in optional_missing)
            + " (шаг поллинга)"
        )
    if note:
        verdict = f"{verdict} · {note}"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def fetch_card(context: AutomationContext, task_id: str) -> tuple[TaskWithRuntimes | None, str]:
    """Читает карточку задачи, возвращая текст ошибки вместо исключения."""
    try:
        return context.tasks.get_task(task_id), ""
    except NotFoundError as exc:
        return None, f"404: {exc}"
    except ValidationError as exc:
        return None, f"ответ не по схеме задачи: {exc.error_count()} ошибок"
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# TC-TASK-01 — запуск диагностической задачи (запуск делает оператор)
# ---------------------------------------------------------------------------
def _run_test_task(context: AutomationContext) -> CheckOutcome:
    """Проверяет факт создания задачи `celery-test` и её постановку на наблюдение."""
    task_id = context.require_task()
    card, error = fetch_card(context, task_id)
    if card is None:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"карточка созданной задачи не получена ({error})",
            {"task_id": task_id, "error": error},
        )

    registered = next(
        (task for task in context.session.tasks if str(task.get("task_id")) == task_id), None
    )
    evidence = {
        "task_id": task_id,
        "type": str(card.type),
        "name": str(card.name),
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "runtimes": len(card.runtimes),
        "observed": registered is not None,
        "observed_origin": (registered or {}).get("origin"),
        "journal_from": (registered or {}).get("journal_from"),
    }
    if str(card.type) != str(TaskType.CELERY_TEST):
        return CheckOutcome(
            CheckStatus.FAILED,
            f"создана задача типа {card.type}, ожидалась {TaskType.CELERY_TEST}",
            evidence,
        )
    if registered is None:
        return CheckOutcome(
            CheckStatus.FAILED,
            "задача создана, но не поставлена на наблюдение (нет записи в сессии)",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"задача {TaskType.CELERY_TEST} создана и поставлена на наблюдение",
        evidence,
    )


# ---------------------------------------------------------------------------
# TC-TASK-02 — список задач, фильтры и пагинация
# ---------------------------------------------------------------------------
def _date_only_probe(context: AutomationContext) -> str:
    """Проба «чистой» даты в `start_date`: каким статусом отвечает стенд (TC-TASK-02).

    Спецификация объявляет `start_date`/`end_date` как `format: date-time`, поэтому
    отказ на строку `2026-09-09` — не дефект сервера, а факт для протокола:
    испытуемый сервер требует дату-время, а пульт обязан передавать именно её
    (`lib.period.day_bounds`). Факт фиксируется, но вердикт не портит.
    """
    try:
        context.tasks.list_tasks(start_date=date.today().isoformat())
    except ValidationApiError as exc:
        return f"{exc.status_code} — «чистая» дата отклонена сервером"
    except ApiError as exc:
        return f"{exc.status_code} — неожиданный отказ сервера"
    except (ClientError, ServerUnavailableError) as exc:
        return f"нет ответа: {exc}"
    return "200 — сервер принимает дату без времени (расхождение со спецификацией)"


def _list_tasks(context: AutomationContext) -> CheckOutcome:
    """Проверяет `GET /api/tasks/`: состав ответа, фильтры и пагинацию."""
    problems: list[str] = []
    facts: dict[str, Any] = {}

    try:
        base = context.tasks.list_tasks()
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(CheckStatus.FAILED, f"список задач не получен: {exc}", {})

    facts["count"] = base.count
    facts["returned"] = len(base.tasks)
    if base.count < len(base.tasks):
        problems.append(f"count={base.count} меньше числа возвращённых задач ({len(base.tasks)})")

    # состав элемента списка: статуса задачи в ответе нет (только в карточке)
    fields = tuple(CeleryTask.model_fields)
    facts["list_fields"] = list(fields)
    if "status" in fields:
        facts["status_in_list"] = "статус есть в элементе списка"
    else:
        facts["status_in_list"] = (
            "статуса в элементе списка нет: он приходит только в карточке "
            "`runtimes[-1].status` (проверка статуса списка — N запросов на N задач)"
        )

    present_types = sorted({str(task.type) for task in base.tasks})
    facts["types"] = present_types
    for task_type in present_types[:2]:
        filtered = context.tasks.list_tasks(task_type=task_type)
        wrong = [str(task.id) for task in filtered.tasks if str(task.type) != task_type]
        facts[f"filter_task_type={task_type}"] = len(filtered.tasks)
        if wrong:
            problems.append(f"фильтр task_type={task_type} вернул задачи других типов")
        if filtered.count > base.count:
            problems.append(f"фильтр task_type={task_type} увеличил count")

    # статуса в элементе списка нет: проверяется только отзывчивость фильтра
    status_page = context.tasks.list_tasks(status=str(TaskStatus.COMPLETED))
    facts["filter_status=completed"] = status_page.count
    facts["status_filter_note"] = (
        "статус задачи в GET /api/tasks/ не возвращается (состав не проверить)"
    )

    # период создания: спецификация объявляет параметры как `format: date-time`
    today = date.today()
    start_iso, end_iso = day_bounds(today - timedelta(days=7), today)
    period_page = context.tasks.list_tasks(start_date=start_iso, end_date=end_iso)
    facts["filter_period"] = f"{start_iso}..{end_iso} → {period_page.count}"
    if period_page.count > base.count:
        problems.append("фильтр периода вернул больше задач, чем список без фильтров")

    # границы периода: start_date включающая, end_date исключающая (замечание P2)
    yesterday_start = date_time_bound(today - timedelta(days=1))
    yesterday_end = date_time_bound(today)
    yesterday = context.tasks.list_tasks(start_date=yesterday_start, end_date=yesterday_end)
    facts["filter_period_yesterday"] = (
        f"{yesterday_start}<=created_at<{yesterday_end} → {yesterday.count} из {base.count}"
    )
    if yesterday.count > base.count:
        problems.append("фильтр «за сутки» вернул больше задач, чем список без фильтров")

    facts["date_only_probe"] = _date_only_probe(context)
    facts["date_only_note"] = (
        "«чистая» дата не принимается (`format: date-time` в спецификации): период "
        "передаётся как `YYYY-MM-DDTHH:MM:SS` (пульт — `lib.period.day_bounds`)"
    )

    first_page = context.tasks.list_tasks(limit=1)
    facts["limit=1"] = len(first_page.tasks)
    if len(first_page.tasks) > 1:
        problems.append(f"limit=1 вернул {len(first_page.tasks)} задач — пагинация не соблюдается")
    if base.count > 1:
        second_page = context.tasks.list_tasks(limit=1, offset=1)
        ids = {str(task.id) for task in first_page.tasks}
        facts["offset=1"] = len(second_page.tasks)
        if ids and ids & {str(task.id) for task in second_page.tasks}:
            problems.append("offset=1 вернул ту же задачу, что limit=1 — смещение не работает")

    # верхняя граница `limit` не объявлена спецификацией: пульт читает список целиком
    # (`acceptance.ui.state.LOAD_TASK_CAP`) и сортирует его сам — на этом держатся
    # виды «Активные | Архив | Все» и клиентская пагинация экрана «Задачи»
    big_page = context.tasks.list_tasks(limit=LIST_PROBE_LIMIT)
    facts["limit_uncapped"] = (
        f"limit={LIST_PROBE_LIMIT} → {len(big_page.tasks)} из {big_page.count}"
    )
    if base.count > 1 and len(big_page.tasks) < base.count:
        problems.append(
            f"limit={LIST_PROBE_LIMIT} вернул {len(big_page.tasks)} задач при count="
            f"{base.count} — список нельзя прочитать целиком"
        )

    # порядок ответа: спецификация его не оговаривает, пульт сортирует сам
    created = [task.created_at for task in base.tasks]
    if created == sorted(created, reverse=True):
        facts["list_unsorted"] = (
            "порядок ответа уже отсортирован по `created_at` ↓ (пульт сортирует сам — "
            "порядок API не гарантирован)"
        )
    else:
        facts["list_unsorted"] = (
            "порядок ответа не отсортирован по `created_at`: пульт сортирует список сам "
            "(«создана ↓», `created_at` + `id`)"
        )
    facts["list_sort_note"] = (
        "сортировка и пагинация списка выполняются в пульте: серверные `limit`/`offset` "
        "используются только для проверки пагинации"
    )

    # архива задач в API нет: это рабочая заметка пульта (файл вне сессии)
    archive_fields = [name for name in fields if "archiv" in name]
    facts["archive_is_local"] = (
        "архив задач ведёт пульт (`acceptance_data/task_snapshot.json`): в API нет ни "
        "признака архива, ни параметра перехода в архив — правила «завершена ≥ суток назад» "
        "и переноса завершённых выполняются на стороне пульта"
    )
    if archive_fields:
        facts["archive_fields"] = archive_fields
        problems.append(f"в элементе списка появились поля архива: {archive_fields}")

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), facts)
    return CheckOutcome(
        CheckStatus.PASSED,
        f"tasks + count получены: задач {base.count}, возвращено {len(base.tasks)}; "
        "фильтры (тип, статус, период) и пагинация отвечают",
        facts,
    )


# ---------------------------------------------------------------------------
# TC-TASK-03 — карточка задачи с прогонами
# ---------------------------------------------------------------------------
def _task_card(context: AutomationContext) -> CheckOutcome:
    """Проверяет состав карточки задачи: `TaskWithRuntimes` и поля прогонов."""
    task_id = context.require_task()
    card, error = fetch_card(context, task_id)
    if card is None:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"карточка задачи не получена ({error})",
            {"task_id": task_id, "error": error},
        )

    if not card.runtimes:
        return CheckOutcome(
            CheckStatus.FAILED,
            "в карточке нет ни одного прогона (runtimes[] пуст)",
            {"task_id": task_id, "type": str(card.type)},
        )

    runtime = card.runtimes[-1]
    status = str(runtime.status)
    parsed = parse_status(status)
    filled = [
        name
        for name, value in (
            ("start_time", runtime.start_time),
            ("end_time", runtime.end_time),
            ("result", runtime.result),
            ("intermediate_result", runtime.intermediate_result),
            ("celery_task_id", runtime.celery_task_id),
        )
        if value is not None
    ]
    evidence = {
        "task_id": task_id,
        "type": str(card.type),
        "runtimes": len(card.runtimes),
        "last_status": status,
        "parameters": dict(runtime.parameters),
        "filled_fields": filled,
    }
    if parsed is None:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"в прогоне неизвестный статус «{status}» (вне FSM-1)",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"карточка с {len(card.runtimes)} прогонами; текущий статус «{status_label(status)}»"
        + (f"; заполнены: {', '.join(filled)}" if filled else ""),
        evidence,
    )


# ---------------------------------------------------------------------------
# TC-TASK-04 — переходы FSM-1 без расхода ресурсов
# ---------------------------------------------------------------------------
def _fsm_transitions(context: AutomationContext) -> CheckOutcome:
    """Вердикт по фактической истории переходов задачи (`pause → resume`)."""
    task_id = context.require_task()
    chain = observed_chain(context.session, task_id)
    return chain_outcome(
        chain,
        ("running", "pausing", "paused", "running"),
        optional=("pausing",),
        note="цепочка собрана монитором из поллингов GET /api/tasks/{task_id}",
    )


# ---------------------------------------------------------------------------
# TC-TASK-05 — прерывание задачи
# ---------------------------------------------------------------------------
def _interrupt(context: AutomationContext) -> CheckOutcome:
    """Вердикт по истории задачи после команды `interrupt`."""
    task_id = context.require_task()
    chain = observed_chain(context.session, task_id)
    return chain_outcome(
        chain,
        ("interrupted",),
        note="проверяется факт перехода в терминальный статус `interrupted`",
    )


# ---------------------------------------------------------------------------
# TC-TASK-06 — идемпотентность команд и запрет из терминального состояния
# ---------------------------------------------------------------------------
def _idempotency(context: AutomationContext) -> CheckOutcome:
    """Дважды отправляет команду FSM-1 и фиксирует вариант поведения сервера."""
    task_id = context.require_task()
    monitor = context.require_monitor()
    action = context.action
    if action not in TASK_COMMANDS:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"команда «{action}» не управляет задачей (доступны: {', '.join(TASK_COMMANDS)})",
            {"action": action},
        )

    # force=True: команда отправляется осознанно и вне допустимого состояния —
    # именно так проверяются идемпотентность и запрет из терминального состояния.
    first = monitor.run_command(context.session, task_id, action, force=True)
    second = monitor.run_command(context.session, task_id, action, force=True)
    evidence = {
        "action": action,
        "first": first.as_dict(),
        "second": second.as_dict(),
        "status_chain": observed_chain(context.session, task_id),
    }

    network_errors = [
        command.error
        for command in (first, second)
        if command.error and not str(command.error).startswith("HTTP")
    ]
    if network_errors:
        return CheckOutcome(
            CheckStatus.FAILED,
            "команда не выполнена: " + "; ".join(network_errors),
            evidence,
        )
    if not (first.sent and second.sent):
        return CheckOutcome(
            CheckStatus.FAILED,
            "сервер не получил повтор команды (успешная отправка предусмотрена сценарием)",
            evidence,
        )

    transitions = [text for text in (first.variant, second.variant) if "переход:" in str(text)]
    if len(transitions) > 1:
        return CheckOutcome(
            CheckStatus.FAILED,
            "повтор команды вызвал побочный эффект (второй переход): " + "; ".join(transitions),
            evidence,
        )

    return CheckOutcome(
        CheckStatus.PASSED,
        f"варианты повторов: {first.variant or '—'} | {second.variant or '—'}",
        evidence,
    )


# ---------------------------------------------------------------------------
# TC-TASK-07 — неизвестная задача
# ---------------------------------------------------------------------------
def _unknown_task(context: AutomationContext) -> CheckOutcome:
    """Проверяет поведение сервера на неизвестный `task_id` (BR-R7)."""
    probe = str(context.params.get("unknown_task_id") or DEFAULT_UNKNOWN_TASK)
    try:
        card = context.tasks.get_task(probe)
    except NotFoundError as exc:
        return CheckOutcome(
            CheckStatus.PASSED,
            f"сервер ответил 404 на неизвестный task_id: {exc}",
            {"variant": "404", "task_id": probe},
        )
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"запрос неизвестной задачи не выполнен: {exc}",
            {"task_id": probe},
        )
    except ValidationError:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"сервер ответил не по схеме задачи на task_id «{probe}» (ожидается UUID)",
            {"task_id": probe, "variant": "schema_error"},
        )

    status = current_status(card)
    evidence = {
        "task_id": probe,
        "variant": "not_found" if status == str(TaskStatus.NOT_FOUND) else status,
        "runtimes": len(card.runtimes),
        "type": str(card.type),
    }
    if status == str(TaskStatus.NOT_FOUND):
        return CheckOutcome(
            CheckStatus.PASSED,
            "сервер вернул статус not_found (BR-R7): UI различает «не найдена» и ошибку запроса",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.FAILED,
        f"на неизвестный task_id пришёл статус «{status_label(status)}» вместо not_found",
        evidence,
    )


# ---------------------------------------------------------------------------
# TC-TASK-08 — наблюдение за внешней задачей (подтверждает оператор)
# ---------------------------------------------------------------------------
def external_observation(context: AutomationContext) -> CheckOutcome:
    """Проверяет, что внешняя задача наблюдается и её статусы получены (BR-R5)."""
    task_id = context.require_task()
    record = next(
        (task for task in context.session.tasks if str(task.get("task_id")) == task_id), None
    )
    chain = observed_chain(context.session, task_id)
    evidence = {
        "task_id": task_id,
        "origin": (record or {}).get("origin"),
        "chain": chain,
        "polls": ((record or {}).get("poll") or {}).get("polls"),
    }
    if record is None:
        return CheckOutcome(
            CheckStatus.FAILED, "внешняя задача не поставлена на наблюдение", evidence
        )
    if str(record.get("origin")) != ORIGIN_EXTERNAL:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"задача не помечена как «внешняя» (происхождение: {record.get('origin')})",
            evidence,
        )
    if not chain:
        return CheckOutcome(
            CheckStatus.FAILED,
            "статусы внешней задачи не получены (ни одного успешного опроса)",
            evidence,
        )
    shown = " → ".join(status_label(status) for status in chain)
    return CheckOutcome(
        CheckStatus.MANUAL_OK,
        f"внешняя задача наблюдается; статусы: {shown}",
        evidence,
    )


# ---------------------------------------------------------------------------
# Прогон сценария через движок
# ---------------------------------------------------------------------------
#: Сценарии проверок: ключ — `CheckSpec.automation`.
AUTOMATIONS: dict[str, Callable[[AutomationContext], CheckOutcome]] = {
    "tasks.run_test_task": _run_test_task,
    "tasks.list_tasks": _list_tasks,
    "tasks.task_card": _task_card,
    "tasks.fsm_transitions": _fsm_transitions,
    "tasks.interrupt": _interrupt,
    "tasks.idempotency": _idempotency,
    "tasks.unknown_task": _unknown_task,
    "tasks.external_observation": external_observation,
}


def scenario(spec: CheckSpec) -> Callable[[AutomationContext], CheckOutcome] | None:
    """Сценарий проверки по её описанию (None — если сценарий не автоматизирован)."""
    return AUTOMATIONS.get(str(spec.automation or ""))


def evaluate(context: AutomationContext, *, automation: str = "") -> CheckOutcome | None:
    """Выполняет сценарий проверки и возвращает её итог (None — если сценария нет).

    Args:
        context: сессия, описание проверки, API задач, монитор и параметры.
        automation: явный ключ сценария — для ручных проверок, где оператор
            подтверждает факт (`tasks.external_observation` для TC-TASK-08).

    Raises:
        Exception: ошибки программирования (опечатки в сценарии) не подавляются;
            отказы API, обрыв связи и отклонения схемы превращаются в вердикт
            «отказ» — проверка должна завершаться фактом, а не падением пульта.
    """
    function = AUTOMATIONS.get(automation or str(context.spec.automation or ""))
    if function is None:
        return None
    try:
        return function(context)
    except PreconditionError as exc:
        return CheckOutcome(CheckStatus.SKIPPED, str(exc), {})
    except (ApiError, ClientError, httpx.HTTPError, ValidationError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"сценарий не выполнен ({type(exc).__name__}): {exc}",
            {"error": str(exc)},
        )


def automate(context: AutomationContext) -> CheckResult | None:
    """Прогоняет автоматический сценарий через движок и сохраняет результат в сессии.

    Returns:
        Результат проверки или None, если у проверки нет автоматического сценария
        (ручные проверки отмечает оператор через `engine.mark`).
    """
    if scenario(context.spec) is None:
        return None

    with start_check(context.spec, journal=context.journal, params=context.params) as run:
        outcome = evaluate(context) or CheckOutcome(
            CheckStatus.SKIPPED, "сценарий проверки не определён", {}
        )
        result = run.finish(
            outcome.status,
            verdict=outcome.verdict,
            operator_note=outcome.note,
            evidence=outcome.evidence,
        )
    save_result(context.session, result)
    return result
