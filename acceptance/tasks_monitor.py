"""Монитор асинхронных задач испытуемого сервера (FR-8, FR-T11; BR-R5/BR-R6).

Задачи сервера асинхронны (`celery-test`, `training`, `dataset-fill`,
`model-testing`, `inference`), поэтому монитор — фундамент всех «длинных» проверок
и этапов T5–T8: он ставит задачу на наблюдение, опрашивает
`GET /api/tasks/{task_id}`, определяет переходы автомата состояний FSM-1
(`lib.task_status`) и складывает историю переходов в сессию испытаний
(`session.tasks`, схема v4).

Ключевые решения:

    * наблюдение живёт в **сессии** (`session.tasks`), а не в памяти пульта: сессия
      переживает перезапуск приложения и перезагрузку страницы, поэтому обрыв связи
      или перезапуск пульта не теряет задачу (BR-R5, BR-R6) — наблюдение
      возобновляется по сохранённому `task_id`;
    * статус задачи берётся из последнего прогона (`TaskWithRuntimes.runtimes[]`):
      отдельного поля `status` у задачи в API нет;
    * `not_found` — это **результат** (BR-R7): сервер отвечает неизвестной задачей,
      а не 404; монитор различает «не найдена» и «сервер недоступен»;
    * команды FSM-1 отправляются только по `available_actions(status)`, кроме явной
      проверки идемпотентности (`force=True`, TC-TASK-06), и фиксируются вместе с
      вариантом поведения сервера (no-op / 409 / переход);
    * источник задачи может быть любой: ответ `train`/`check`/`inference`, обходной
      поиск задачи `dataset-fill` (её `fill` не отдаёт `task_id`, P1), список задач,
      ручной ввод `task_id` внешней задачи (TC-TASK-08) и ответы консоли запросов
      (`session.console_task_ids`, FR-T3).

Опрос не блокирует интерфейс: экран «Задачи» вызывает `poll_all` (один проход за
перерисовку), а `watch` — блокирующий цикл `lib.polling.poll_until` для консоли,
интеграционных тестов и «довести задачу до терминального статуса».
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from acceptance.checks.engine import check_label
from acceptance.http_log import Journal, current_label
from acceptance.logging_setup import log_event
from acceptance.session import (
    ORIGIN_EXTERNAL,
    ORIGIN_PULT,
    TestSession,
    add_task,
    console_task_ids,
    find_task,
    set_task_check,
    tasks_summary,
    update_task,
)
from client.errors import ApiError, ClientError, NotFoundError, ServerUnavailableError
from client.schemas import CeleryTask, TaskStatus, TaskWithRuntimes
from client.tasks import TasksApi
from lib.polling import poll_until
from lib.task_status import (
    available_actions,
    get_status_presentation,
    is_terminal,
    presentation_for,
)

#: Метка журнала для опросов, не привязанных к конкретной проверке.
MONITOR_LABEL = "TC-TASK"

#: Команды управления задачей (FSM-1, UC-28).
TASK_COMMANDS: tuple[str, ...] = ("pause", "resume", "interrupt")

#: Подписи и индикаторы статусов FSM-1 (одни на весь пульт — `lib.task_status`).
STATUS_LABELS: dict[str, str] = {
    str(status): get_status_presentation(status).label for status in TaskStatus
}
STATUS_ICONS: dict[str, str] = {
    str(status): get_status_presentation(status).indicator for status in TaskStatus
}

#: Компактные пиктограммы статусов для таблиц (экран «Задачи»).
STATUS_GLYPHS: dict[str, str] = {
    str(status): get_status_presentation(status).glyph for status in TaskStatus
}

#: Цвета пиктограмм статусов (CSS-имена для `pandas.Styler`).
STATUS_COLORS: dict[str, str] = {
    str(status): get_status_presentation(status).color for status in TaskStatus
}

#: Порядок состояний в легенде таблицы: сначала «в работе», затем завершённые.
STATUS_ORDER: tuple[str, ...] = (
    str(TaskStatus.NEW),
    str(TaskStatus.RUNNING),
    str(TaskStatus.PAUSING),
    str(TaskStatus.PAUSED),
    str(TaskStatus.COMPLETED),
    str(TaskStatus.FAILED),
    str(TaskStatus.INTERRUPTED),
    str(TaskStatus.NOT_FOUND),
)


def parse_status(value: Any) -> TaskStatus | None:
    """Разбирает статус задачи (`TaskStatus`), None — если значение неизвестно."""
    try:
        return TaskStatus(str(value))
    except ValueError:
        return None


def status_label(value: Any) -> str:
    """Подпись статуса задачи для интерфейса и отчёта."""
    text = str(value or "")
    return STATUS_LABELS.get(text, text or "неизвестен")


def status_icon(value: Any) -> str:
    """Индикатор статуса задачи для интерфейса."""
    return STATUS_ICONS.get(str(value or ""), "⚪")


def status_glyph(value: Any) -> str:
    """Компактная пиктограмма статуса для таблиц («▶», «✔», «?»)."""
    return STATUS_GLYPHS.get(str(value or ""), presentation_for(value).glyph)


def status_color(value: Any) -> str:
    """Цвет пиктограммы статуса для таблиц («green», «yellow», «red», «grey»)."""
    return STATUS_COLORS.get(str(value or ""), presentation_for(value).color)


def status_text(value: Any) -> str:
    """Ячейка статуса для таблицы: пиктограмма и подпись («▶ Выполняется»)."""
    return f"{status_glyph(value)} {status_label(value)}"


def status_legend() -> str:
    """Легенда пиктограмм таблицы: «▶ выполняется · ✔ завершена · …»."""
    return " · ".join(
        f"{status_glyph(status)} {status_label(status).lower()}" for status in STATUS_ORDER
    )


def is_terminal_status(value: Any) -> bool:
    """True, если статус терминальный (задача больше не изменится)."""
    parsed = parse_status(value)
    return bool(parsed is not None and is_terminal(parsed))


def available_actions_for(value: Any) -> tuple[str, ...]:
    """Допустимые команды для статуса (пусто, если статус неизвестен)."""
    parsed = parse_status(value)
    return available_actions(parsed) if parsed is not None else ()


def current_status(task: TaskWithRuntimes) -> str:
    """Текущий статус задачи — статус последнего прогона `runtimes[]`.

    Отдельного поля `status` у задачи в API нет: состояние живёт в прогонах, а
    последний прогон описывает текущее выполнение.
    """
    if not task.runtimes:
        return ""
    return str(task.runtimes[-1].status)


def task_card(task: TaskWithRuntimes) -> dict[str, Any]:
    """Проекция карточки задачи для списка: статус, тип, название, времена прогона.

    Полная карточка в таблице не нужна (там десятки полей прогона и результатов),
    а нужны ровно те значения, которые показывает список: статус последнего прогона,
    тип, название и время начала/окончания этого прогона.
    """
    last = task.runtimes[-1] if task.runtimes else None

    def stamp(value: Any) -> str:
        """Отметка времени в ISO-виде (пустая строка, если времени нет)."""
        if value is None:
            return ""
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    return {
        "status": current_status(task),
        "type": str(task.type),
        "name": task.name,
        "start_time": stamp(getattr(last, "start_time", None)),
        "end_time": stamp(getattr(last, "end_time", None)),
    }


#: Источники статуса строки списка: серверная карточка — самый свежий ответ.
SOURCE_CARD = "карточка"
SOURCE_SNAPSHOT = "снимок"
SOURCE_POLL = "опрос"
SOURCE_NONE = "нет данных"


@dataclass
class TaskStatusView:
    """Статус строки списка: откуда он взят и что ещё известно о задаче.

    Три источника, по убыванию свежести: карточка `GET /api/tasks/{id}` (запрошена
    только что), снимок `task_snapshot.json` (последний известный серверный статус,
    переживает перезапуск пульта) и последний опрос задачи монитором в текущей сессии
    (`session.tasks`). Если нет ничего — «нет данных»: пульт не показывает выдуманный
    статус, а честно помечает неизвестность.
    """

    status: str = ""
    source: str = SOURCE_NONE
    observed_at: str = ""
    start_time: str = ""
    end_time: str = ""
    error: str = ""

    @property
    def known(self) -> bool:
        """True, если статус известен хотя бы из одного источника."""
        return bool(self.status)

    @property
    def text(self) -> str:
        """Ячейка таблицы: пиктограмма и подпись статуса."""
        return status_text(self.status)

    @property
    def color(self) -> str:
        """Цвет пиктограммы статуса для таблицы."""
        return status_color(self.status)

    @property
    def is_terminal(self) -> bool:
        """True, если статус терминальный (задача больше не изменится)."""
        return is_terminal_status(self.status)


def task_status_view(
    *,
    card: Mapping[str, Any] | None = None,
    snapshot: Any = None,
    poll_status: str = "",
) -> TaskStatusView:
    """Собирает статус строки списка из карточки, снимка и последнего опроса.

    Args:
        card: проекция карточки (`task_card`) — свежайший источник.
        snapshot: запись снимка (`TaskSnapshotEntry`) с последним известным статусом.
        poll_status: статус последнего опроса задачи в текущей сессии.

    Returns:
        `TaskStatusView` с приоритетом «карточка → снимок → опрос»: времена берутся
        из первого источника, где они есть, а ошибка карточки сохраняется для
        подсказки в строке.
    """
    view = TaskStatusView()
    if isinstance(card, Mapping):
        view.error = str(card.get("error") or "")
        if str(card.get("status") or ""):
            view.status = str(card["status"])
            view.source = SOURCE_CARD
            view.observed_at = str(card.get("fetched_at") or "")
        view.start_time = str(card.get("start_time") or "")
        view.end_time = str(card.get("end_time") or "")
    if not view.status and snapshot is not None:
        status = str(getattr(snapshot, "status", "") or "")
        if status:
            view.status = status
            view.source = SOURCE_SNAPSHOT
            view.observed_at = str(getattr(snapshot, "fetched_at", "") or "")
    if not view.status and poll_status:
        view.status = str(poll_status)
        view.source = SOURCE_POLL
    if snapshot is not None:  # времена могут быть только в снимке
        view.start_time = view.start_time or str(getattr(snapshot, "start_time", "") or "")
        view.end_time = view.end_time or str(getattr(snapshot, "end_time", "") or "")
        view.error = view.error or str(getattr(snapshot, "error", "") or "")
    return view


def runtime_row(runtime: Any) -> dict[str, Any]:
    """Строка таблицы прогонов задачи (`runtimes[]`) для интерфейса и артефактов."""

    def _value(name: str) -> Any:
        value = getattr(runtime, name, None)
        if value is None:
            return None
        return value.isoformat() if hasattr(value, "isoformat") else value

    return {
        "status": str(getattr(runtime, "status", "")),
        "status_label": status_label(getattr(runtime, "status", "")),
        "celery_task_id": getattr(runtime, "celery_task_id", None),
        "start_time": _value("start_time"),
        "end_time": _value("end_time"),
        "parameters": dict(getattr(runtime, "parameters", {}) or {}),
        "result": getattr(runtime, "result", None),
        "intermediate_result": getattr(runtime, "intermediate_result", None),
        "runtime_id": str(getattr(runtime, "id", "")),
    }


@dataclass
class TaskPoll:
    """Результат одного опроса задачи (`GET /api/tasks/{task_id}`)."""

    task_id: str
    ok: bool = False
    unknown: bool = False
    status: str = ""
    previous: str = ""
    changed: bool = False
    terminal: bool = False
    journal_seq: int | None = None
    error: str = ""
    label: str = MONITOR_LABEL
    record: dict[str, Any] | None = None
    task: TaskWithRuntimes | None = None

    @property
    def status_label(self) -> str:
        """Подпись статуса (после опроса)."""
        return status_label(self.status)

    @property
    def status_icon(self) -> str:
        """Индикатор статуса (после опроса)."""
        return status_icon(self.status)

    def as_dict(self) -> dict[str, Any]:
        """Представление опроса для журнала, истории сессии и отчёта."""
        return {
            "task_id": self.task_id,
            "ok": self.ok,
            "unknown": self.unknown,
            "status": self.status,
            "previous": self.previous,
            "changed": self.changed,
            "terminal": self.terminal,
            "journal_seq": self.journal_seq,
            "error": self.error,
            "label": self.label,
        }


@dataclass
class TaskCommand:
    """Результат команды управления задачей (`pause`/`resume`/`interrupt`)."""

    task_id: str
    action: str
    allowed: bool = False
    sent: bool = False
    status: int | None = None
    variant: str = ""
    error: str = ""
    body: Any = None
    journal_seq: int | None = None
    label: str = MONITOR_LABEL
    poll: TaskPoll | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        """True, если команда отправлена и не привела к ошибке."""
        return self.sent and not self.error

    def as_dict(self) -> dict[str, Any]:
        """Представление команды для журнала, истории сессии и отчёта."""
        return {
            "task_id": self.task_id,
            "action": self.action,
            "allowed": self.allowed,
            "sent": self.sent,
            "status": self.status,
            "variant": self.variant,
            "error": self.error,
            "body": self.body,
            "journal_seq": self.journal_seq,
            "label": self.label,
            "note": self.note,
            "status_after": self.poll.status if self.poll is not None else None,
        }


@dataclass
class TaskObservation:
    """Строка таблицы «Наблюдение» на экране «Задачи»."""

    task_id: str
    task_type: str = ""
    name: str = ""
    origin: str = ORIGIN_PULT
    check_id: str = ""
    status: str = ""
    interval: float = 0.0
    active: bool = True
    polls: int = 0
    last_poll: str | None = None
    first_seen: str = ""
    last_seen: str = ""
    journal_from: int | None = None
    journal_seq: int | None = None
    intermediate_result: dict[str, Any] | None = None
    note: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def status_label(self) -> str:
        """Подпись статуса для таблицы."""
        return status_label(self.status)

    @property
    def status_icon(self) -> str:
        """Индикатор статуса для таблицы."""
        return status_icon(self.status)

    @property
    def is_terminal(self) -> bool:
        """True, если задача в терминальном состоянии."""
        return is_terminal_status(self.status)

    @property
    def actions(self) -> tuple[str, ...]:
        """Команды, допустимые для текущего статуса (FSM-1)."""
        return available_actions_for(self.status)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> TaskObservation:
        """Строка наблюдения из записи сессии (`session.tasks[]`)."""
        poll = dict(record.get("poll") or {})
        errors = tuple(
            str(item.get("error"))
            for item in (record.get("errors") or [])
            if isinstance(item, dict) and item.get("error")
        )
        return cls(
            task_id=str(record.get("task_id") or ""),
            task_type=str(record.get("type") or ""),
            name=str(record.get("name") or ""),
            origin=str(record.get("origin") or ORIGIN_PULT),
            check_id=str(record.get("check_id") or ""),
            status=str(record.get("status") or ""),
            interval=float(poll.get("interval") or 0.0),
            active=bool(poll.get("active")),
            polls=int(poll.get("polls") or 0),
            last_poll=poll.get("last_poll"),
            first_seen=str(record.get("first_seen") or ""),
            last_seen=str(record.get("last_seen") or ""),
            journal_from=record.get("journal_from"),
            journal_seq=record.get("journal_seq"),
            intermediate_result=record.get("intermediate_result"),
            note=str(record.get("note") or ""),
            errors=errors,
        )


class TaskMonitor:
    """Наблюдение за асинхронными задачами: регистрация, опрос, команды FSM-1.

    Монитор не хранит состояние: источник истины — `session.tasks` (схема v4),
    поэтому он собирается на каждую перерисовку экрана из `Runtime` и не теряет
    наблюдение при перезапуске пульта (BR-R5, BR-R6).
    """

    def __init__(
        self,
        tasks: TasksApi,
        *,
        journal: Journal | None = None,
        interval: float | None = None,
    ) -> None:
        self._tasks = tasks
        self._journal = journal
        self.interval = float(interval) if interval else None

    # -- регистрация задач ---------------------------------------------------
    def register(
        self,
        session: TestSession,
        *,
        task_id: str,
        task_type: str = "",
        name: str = "",
        origin: str = ORIGIN_PULT,
        check_id: str = "",
        status: str = "",
        journal_seq: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Ставит задачу на наблюдение (идемпотентно: одна запись на `task_id`)."""
        record = add_task(
            session,
            task_id=task_id,
            task_type=task_type,
            name=name,
            origin=origin,
            check_id=check_id,
            status=status,
            interval=self.interval,
            journal_from=journal_seq,
            journal_seq=journal_seq,
            note=note,
        )
        log_event(
            "task_registered",
            f"Задача на наблюдении: {record['task_id']}",
            module="tasks",
            check_id=record.get("check_id") or None,
            payload={
                "task_id": record["task_id"],
                "task_type": record["type"],
                "origin": record["origin"],
                "interval": (record.get("poll") or {}).get("interval"),
            },
        )
        return record

    def register_task(
        self, session: TestSession, task: CeleryTask, *, check_id: str = ""
    ) -> dict[str, Any]:
        """Ставит на наблюдение задачу из списка (`GET /api/tasks/`)."""
        return self.register(
            session,
            task_id=str(task.id),
            task_type=str(task.type),
            name=str(task.name),
            check_id=check_id,
            note="задача выбрана из списка задач",
        )

    def register_external(
        self, session: TestSession, task_id: str, *, check_id: str = "TC-TASK-08"
    ) -> dict[str, Any]:
        """Ставит на наблюдение задачу, запущенную вне пульта (TC-TASK-08, BR-R5)."""
        return self.register(
            session,
            task_id=task_id,
            origin=ORIGIN_EXTERNAL,
            check_id=check_id,
            note="внешняя задача: task_id введён оператором",
        )

    def register_from_response(
        self,
        session: TestSession,
        payload: Any,
        *,
        check_id: str = "",
        journal_seq: int | None = None,
        task_type: str = "",
        note: str = "задача создана пультом",
    ) -> dict[str, Any] | None:
        """Ставит на наблюдение задачу, найденную в ответе API (`task_id`).

        Источники: `POST /api/tasks/test`, `train`, `check`, async-`inference`,
        обходной поиск задачи `dataset-fill` (`fill` не отдаёт `task_id`, P1).
        """
        task_id = task_id_from_payload(payload)
        if not task_id:
            return None
        return self.register(
            session,
            task_id=task_id,
            task_type=task_type,
            check_id=check_id,
            journal_seq=journal_seq,
            note=note,
        )

    def adopt_from_session(self, session: TestSession) -> list[dict[str, Any]]:
        """Подхватывает задачи, созданные ранее (в том числе из консоли, FR-T3)."""
        known = {str(task.get("task_id")) for task in session.tasks}
        adopted: list[dict[str, Any]] = []
        for call in console_task_ids(session):
            if call["task_id"] in known:
                continue
            adopted.append(
                self.register(
                    session,
                    task_id=call["task_id"],
                    check_id=str(call.get("label") or ""),
                    journal_seq=call.get("journal_seq"),
                    note=f"задача подхвачена из вызова консоли: {call.get('operation') or '—'}",
                )
            )
        return adopted

    # -- управление наблюдением ---------------------------------------------
    def observed(self, session: TestSession, *, active_only: bool = False) -> list[TaskObservation]:
        """Строки наблюдения (для таблицы «Наблюдение»)."""
        records = [dict(item) for item in session.tasks]
        if active_only:
            records = [item for item in records if (item.get("poll") or {}).get("active")]
        return [TaskObservation.from_record(item) for item in records]

    def observation_index(self, session: TestSession | None) -> dict[str, TaskObservation]:
        """Наблюдения по `task_id` — статус для списка задач без запросов к серверу.

        `GET /api/tasks/` не возвращает статус задачи (в спецификации у `CeleryTask`
        только `type`, `name`, `description`, `id`, `created_at`), поэтому статус для
        строки списка берётся из последнего опроса монитора (`session.tasks`). Список
        без сессии даёт пустой индекс.
        """
        if session is None:
            return {}
        return {item.task_id: item for item in self.observed(session) if item.task_id}

    def set_active(self, session: TestSession, task_id: str, active: bool) -> dict[str, Any] | None:
        """Ставит поллинг задачи на паузу или возобновляет его."""
        return update_task(
            session,
            task_id,
            active=active,
            note="поллинг возобновлён" if active else "поллинг приостановлен оператором",
        )

    def set_interval(
        self, session: TestSession, task_id: str, interval: float
    ) -> dict[str, Any] | None:
        """Меняет интервал поллинга задачи (секунды)."""
        return update_task(session, task_id, interval=interval)

    def attach_to_check(
        self, session: TestSession, task_id: str, check_id: str
    ) -> dict[str, Any] | None:
        """Привязывает задачу к проверке чек-листа (`TC-…`)."""
        return set_task_check(session, task_id, check_id)

    def unregister(self, session: TestSession, task_id: str) -> bool:
        """Снимает задачу с наблюдения (карточка задачи и журнал остаются)."""
        key = str(task_id).strip()
        record = find_task(session, key)
        if record is None:
            return False
        session.tasks = [task for task in session.tasks if str(task.get("task_id")) != key]
        session.add_history(
            "task_unobserved",
            f"Задача снята с наблюдения: {key}",
            task_id=key,
            status=record.get("status"),
        )
        return True

    def kpi(self, session: TestSession) -> dict[str, Any]:
        """KPI монитора: наблюдаемых, активных, завершённых, прерванных, внешних."""
        return tasks_summary(session)

    # -- вспомогательное -----------------------------------------------------
    def label_for(self, session: TestSession, task_id: str, label: str = "") -> str:
        """Метка журнала для работы с задачей.

        Приоритет: явная метка → активная метка проверки (сценарий проверки уже
        открыл контекст) → проверка, привязанная к задаче → метка монитора.
        """
        if label:
            return label
        active = current_label()
        if active:
            return active
        record = find_task(session, task_id)
        check_id = str((record or {}).get("check_id") or "")
        return check_id or MONITOR_LABEL

    def last_seq(self) -> int | None:
        """Номер последней записи журнала обмена (для привязки задачи)."""
        if self._journal is None or not self._journal.records:
            return None
        return self._journal.records[-1].seq

    def card(self, task_id: str) -> TaskWithRuntimes:
        """Карточка задачи с прогонами (`GET /api/tasks/{task_id}`, UC-27)."""
        return self._tasks.get_task(task_id)

    def card_snapshot(self, task: TaskWithRuntimes) -> dict[str, Any]:
        """Карточка задачи как JSON-совместимый словарь (артефакты и доказательства)."""
        return {
            "task_id": str(task.id),
            "type": str(task.type),
            "name": str(task.name),
            "description": task.description,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "runtimes": [runtime_row(runtime) for runtime in task.runtimes],
        }

    def find_recent(
        self, *, task_type: str = "", limit: int = 100, offset: int = 0
    ) -> list[CeleryTask]:
        """Последние задачи сервера — обходной путь для операций без `task_id`.

        `POST /api/datasets/fill/{id}` отвечает 202 без `task_id` (P1), поэтому
        связанная задача `dataset-fill` ищется в списке задач (этапы T4/T5).
        """
        response = self._tasks.list_tasks(
            task_type=task_type or None, limit=limit, offset=offset or None
        )
        return list(response.tasks)

    # -- опрос задач ---------------------------------------------------------
    def poll_once(self, session: TestSession, task_id: str, *, label: str = "") -> TaskPoll:
        """Один опрос задачи: `GET /api/tasks/{task_id}` и запись перехода в сессию.

        Опрос помечается меткой проверки (`TC-…`), поэтому все обмены монитора
        видны в журнале фильтром по метке и попадают в диапазон проверки.
        """
        key = str(task_id).strip()
        record = find_task(session, key)
        previous = str((record or {}).get("status") or "")
        poll = TaskPoll(task_id=key, previous=previous, label=self.label_for(session, key, label))
        if record is None:
            poll.error = "задача не наблюдается: поставьте её на наблюдение"
            return poll

        with check_label(poll.label):
            try:
                task = self._tasks.get_task(key)
            except NotFoundError as exc:
                poll.unknown = True
                poll.error = f"404: {exc}"
                self._record_error(session, key, poll)
                return poll
            except ServerUnavailableError as exc:
                poll.error = f"сервер недоступен: {exc}"
                self._record_error(session, key, poll)
                return poll
            except ValidationError as exc:
                # ответ не соответствует схеме (чаще всего — `task_id` не UUID)
                poll.error = f"некорректный task_id: {exc.error_count()} ошибок схемы"
                self._record_error(session, key, poll)
                return poll
            except (ApiError, ClientError, httpx.HTTPError) as exc:
                poll.error = str(exc)
                self._record_error(session, key, poll)
                return poll

        status = current_status(task)
        terminal = is_terminal_status(status)
        poll.ok = True
        poll.status = status
        poll.unknown = bool(status == str(TaskStatus.NOT_FOUND))
        poll.terminal = terminal
        poll.changed = status != previous
        poll.task = task
        poll.journal_seq = self.last_seq()

        note = ""
        if poll.unknown:
            note = "задача не найдена на сервере (BR-R7)"
        elif terminal:
            note = "терминальный статус — наблюдение остановлено"

        poll.record = update_task(
            session,
            key,
            status=status or None,
            task_type=str(task.type),
            name=str(task.name),
            runtimes=[runtime_row(runtime) for runtime in task.runtimes],
            intermediate_result=last_intermediate(task),
            journal_seq=poll.journal_seq,
            polled=True,
            active=False if terminal else None,
            note=note,
        )
        log_event(
            "task_status_changed" if poll.changed else "task_polled",
            f"Задача {key}: {status_label(previous)} → {status_label(status)}",
            level="INFO",
            module="tasks",
            check_id=poll.label,
            payload=poll.as_dict(),
        )
        return poll

    def _record_error(self, session: TestSession, task_id: str, poll: TaskPoll) -> None:
        """Фиксирует неудачный опрос, сохраняя наблюдение (BR-R5)."""
        poll.journal_seq = self.last_seq()
        poll.record = update_task(session, task_id, error=poll.error, journal_seq=poll.journal_seq)
        log_event(
            "task_poll_error",
            f"Задача {task_id}: опрос не выполнен ({poll.error})",
            level="WARNING",
            module="tasks",
            check_id=poll.label,
            payload=poll.as_dict(),
        )

    def poll_all(
        self, session: TestSession, *, active_only: bool = True, label: str = ""
    ) -> list[TaskPoll]:
        """Один проход опроса по наблюдаемым задачам (без блокировки интерфейса)."""
        return [
            self.poll_once(session, observation.task_id, label=label)
            for observation in self.observed(session, active_only=active_only)
        ]

    def due(self, session: TestSession, *, moment: datetime | None = None) -> list[str]:
        """Задачи, у которых истёк интервал поллинга (для опроса при перерисовке).

        Интервал — свойство наблюдения (`session.tasks[].poll.interval`), поэтому
        оператор может замедлить или ускорить опрос конкретной задачи. Задачи, ни
        разу не опрошенные, попадают в список сразу.
        """
        now = moment or datetime.now()
        pending: list[str] = []
        for record in session.tasks:
            poll = dict(record.get("poll") or {})
            if not poll.get("active"):
                continue
            task_id = str(record.get("task_id") or "")
            last_poll = poll.get("last_poll")
            if not task_id:
                continue
            if not last_poll:
                pending.append(task_id)
                continue
            interval = float(poll.get("interval") or 0.0) or float(self.interval or 0.0)
            try:
                elapsed = (now - datetime.fromisoformat(str(last_poll))).total_seconds()
            except ValueError:
                pending.append(task_id)
                continue
            if interval <= 0 or elapsed >= interval:
                pending.append(task_id)
        return pending

    def watch(
        self,
        session: TestSession,
        task_id: str,
        *,
        interval: float | None = None,
        timeout: float | None = None,
    ) -> TaskPoll | None:
        """Доводит задачу до терминального статуса (или обрыва связи) поллингом.

        Блокирующий цикл (`lib.polling.poll_until`) для консоли, интеграционных
        тестов и режима «дождаться завершения». Экран «Задачи» его не использует:
        там достаточно `poll_all` (один проход за перерисовку).
        """
        key = str(task_id).strip()
        record = find_task(session, key)
        step = float(interval or (record or {}).get("poll", {}).get("interval") or 0.0)
        result: TaskPoll | None = None

        def fetch() -> TaskPoll:
            nonlocal result
            result = self.poll_once(session, key)
            return result

        poll_until(
            fetch,
            lambda poll: poll.terminal or not poll.ok,
            interval=step if step > 0 else 1.0,
            timeout=timeout,
        )
        return result

    # -- команды FSM-1 (UC-28, FSM-1) ----------------------------------------
    def actions_for(self, status: str) -> tuple[str, ...]:
        """Команды, допустимые для статуса (из `lib.task_status`)."""
        return available_actions_for(status)

    def run_command(
        self,
        session: TestSession,
        task_id: str,
        action: str,
        *,
        label: str = "",
        force: bool = False,
    ) -> TaskCommand:
        """Отправляет команду управления и фиксирует реакцию сервера (FSM-1).

        Команда отправляется только если она допустима для текущего статуса
        (`available_actions`); `force=True` — осознанное исключение для проверки
        идемпотентности и запрета из терминального состояния (TC-TASK-06). После
        команды статус опрашивается заново: так фиксируется факт перехода или
        вариант «no-op», а ответ `409` не роняет пульт (BR-R3).
        """
        key = str(task_id).strip()
        command_name = str(action).strip().lower()
        record = find_task(session, key)
        command = TaskCommand(
            task_id=key, action=command_name, label=self.label_for(session, key, label)
        )
        if command_name not in TASK_COMMANDS:
            command.error = f"неизвестная команда: {action}"
            return command
        if record is None:
            command.error = "задача не наблюдается: поставьте её на наблюдение"
            return command

        status = str(record.get("status") or "")
        allowed = command_name in available_actions_for(status)
        command.allowed = allowed
        if not allowed and not force:
            command.variant = "команда не отправлена: запрещена текущим статусом (FSM-1)"
            command.note = (
                f"статус «{status_label(status)}», допустимо: "
                f"{', '.join(available_actions_for(status)) or '—'}"
            )
        if not allowed and force:
            command.variant = (
                "команда отправлена вне допустимого состояния "
                "(проверка идемпотентности, TC-TASK-06)"
            )

        if command.allowed or force:
            self._send_command(command)
            if command.sent and not command.error:
                poll = self.poll_once(session, key, label=command.label)
                command.poll = poll
                effect = (
                    "no-op: статус не изменился"
                    if poll.status == status
                    else f"переход: {status_label(status)} → {status_label(poll.status)}"
                )
                command.variant = f"{command.variant} · {effect}".lstrip(" ·")

        session.add_history(
            "task_command",
            f"Задача {key}: {command_name} → {command.variant or command.error}",
            task_id=key,
            action=command_name,
            allowed=command.allowed,
            variant=command.variant,
            error=command.error,
            journal_seq=command.journal_seq,
            check_id=command.label,
        )
        log_event(
            "task_command",
            f"Задача {key}: {command_name} → {command.variant or command.error}",
            level="INFO" if command.ok else "WARNING",
            module="tasks",
            check_id=command.label,
            payload=command.as_dict(),
        )
        return command

    def _send_command(self, command: TaskCommand) -> TaskCommand:
        """Выполняет HTTP-вызов команды (эффект команды определяет следующий опрос)."""
        call = {
            "pause": self._tasks.pause_task,
            "resume": self._tasks.resume_task,
            "interrupt": self._tasks.interrupt_task,
        }[command.action]
        with check_label(command.label):
            try:
                command.body = short_body(call(command.task_id))
            except ApiError as exc:  # 409/400 и прочие ответы сервера
                command.sent = True
                command.status = exc.status_code
                command.error = str(exc)
                command.variant = f"HTTP {exc.status_code}: команда не выполнена"
            except (ClientError, httpx.HTTPError) as exc:
                command.sent = True
                command.error = f"сервер недоступен: {exc}"
            else:
                command.sent = True
                command.status = 200
            command.journal_seq = self.last_seq()
        return command


# ---------------------------------------------------------------------------
# Помощники (используются экранами, консолью и интеграционными тестами)
# ---------------------------------------------------------------------------
def task_id_from_payload(payload: Any) -> str:
    """Идентификатор задачи из ответа API (`task_id`/`task`/`id`).

    `POST /api/tasks/test` отвечает словарём строк, `train`/`check`/`inference`
    возвращают `task_id` явно — все варианты приводятся к одному виду.
    """
    if isinstance(payload, (TaskWithRuntimes, CeleryTask)):
        return str(payload.id)
    if not isinstance(payload, dict):
        return ""
    for key in ("task_id", "task", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def last_intermediate(task: TaskWithRuntimes) -> dict[str, Any] | None:
    """Промежуточный результат последнего прогона задачи (прогресс, эпохи)."""
    if not task.runtimes:
        return None
    value = task.runtimes[-1].intermediate_result
    return dict(value) if isinstance(value, dict) else None


def short_body(body: Any, limit: int = 400) -> Any:
    """Обрезает тело ответа команды для журнала и отчёта."""
    if isinstance(body, dict):
        return {
            str(key): (value[:limit] if isinstance(value, str) else value)
            for key, value in body.items()
        }
    if isinstance(body, str):
        return body[:limit]
    return body
