"""Экран «Задачи» — монитор асинхронных задач (FR-8, FR-T11, этап T4).

Экран — рабочее место оператора для всех асинхронных операций сервера:

    * **Список сервера** — `GET /api/tasks/` с фильтрами `task_type`, `status`,
      `start_date`/`end_date` (TC-TASK-02). Список читается целиком (`limit` стенда не
      ограничен, пульт режет его на `state.LOAD_TASK_CAP`) и сортируется в пульте по
      «создана ↓»: порядок ответа сервера не отсортирован. Границы периода считаются
      в `lib.period.day_bounds`: стенд принимает их только как дата-время
      (`2026-09-09` → 422), а `end_date` сравнивает как исключающую границу.
      Статуса в ответе списка нет (в спецификации у `CeleryTask` только `type`,
      `name`, `description`, `id`, `created_at`), поэтому статус берётся из карточки
      задачи (`GET /api/tasks/{id}`, N+1), запоминается в снимке
      `acceptance_data/task_snapshot.json` и показывается пиктограммой с цветом
      (`▶`/`✔`/`✖`, `pandas.Styler`); колонка «Источник» говорит, откуда взят статус —
      из свежей карточки, снимка или наблюдения сессии. Виды «Активные | Архив | Все»
      и архив задач опираются на правило `acceptance.task_snapshot.ARCHIVE_AFTER`
      (завершена ≥ суток назад → архив, вернулась в работу → обратно);
    * **Наблюдение** — задачи, поставленные на наблюдение монитором (пульт и
      внешние): статус FSM-1, прошедшее время, `intermediate_result`, управление
      поллингом (пауза/возобновление/снятие), опрос по интервалу (BR-R5, BR-R6);
    * **Карточка задачи** — `TaskWithRuntimes` с прогонами `runtimes[]`, история
      переходов FSM-1 из сессии, команды **строго по `available_actions`**
      (`pause`/`resume`/`interrupt`) и прогон сценариев проверок `TC-TASK-03…07`;
    * **Диагностика** — запуск дешёвой задачи `celery-test`
      (`POST /api/tasks/test?duration=N`, класс `write` → подтверждение) с
      автоматическим наблюдением созданной задачи (TC-TASK-01);
    * **Внешняя задача** — ручной ввод `task_id` задачи, запущенной вне пульта,
      с пометкой «внешняя» (TC-TASK-08, BR-R5).

Правила испытаний: изменяющие операции выполняются только осознанно (подтверждение
оператора), создаваемые сущности помечаются `__TEST__`, каждая команда и каждый
переход FSM-1 фиксируются в сессии и в структурном журнале.

Монитор не хранит состояние: наблюдение живёт в сессии (`session.tasks`, схема v4),
поэтому перезапуск пульта или перезагрузка страницы не теряет задачи.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from acceptance import endpoints as ep
from acceptance import glossary, notes, task_snapshot
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.logging_setup import log_event
from acceptance.paths import ARTIFACT_DIR
from acceptance.session import (
    ORIGIN_EXTERNAL,
    TestSession,
    add_artifact,
    find_task,
    task_history,
    tasks_summary,
)
from acceptance.tasks_monitor import (
    MONITOR_LABEL,
    SOURCE_SNAPSHOT,
    STATUS_ORDER,
    TASK_COMMANDS,
    TaskMonitor,
    TaskObservation,
    TaskStatusView,
    status_color,
    status_icon,
    status_label,
    status_legend,
    status_text,
    task_status_view,
)
from acceptance.ui import state
from acceptance.ui.common import api_notes, check_run
from acceptance.ui.common.flash import render_flash, repeat_flash, set_flash
from acceptance.ui.common.labels import head_text, short_id
from acceptance.ui.common.pagination import render_pagination
from client.errors import ClientError
from client.schemas import CeleryTask, TaskStatus, TaskType, TaskWithRuntimes
from lib.pagination import paginate
from lib.period import day_bounds

#: Ключи состояния экрана.
KEY_PAGE = "tasks_page"
KEY_CARD = "tasks_card"
KEY_CARD_TASK = "tasks_card_task"
KEY_FOCUS = "tasks_focus"
KEY_DURATION = "tasks_diag_duration"
KEY_DIAG_CONFIRM = "tasks_diag_confirm"
KEY_DIAG_RESULT = "tasks_diag_result"
KEY_FILTER_TYPE = "tasks_filter_type"
KEY_FILTER_STATUS = "tasks_filter_status"
KEY_FILTER_PERIOD = "tasks_filter_period"
KEY_FILTER_FROM = "tasks_filter_from"
KEY_FILTER_TO = "tasks_filter_to"
KEY_VIEW = "tasks_view"
KEY_FULL_VALUES = "tasks_full_values"
KEY_ARCHIVE_FRESH = "tasks_archive_fresh"
KEY_ARCHIVE_CONFIRM = "tasks_archive_confirm"
KEY_ARCHIVE_RESET = "tasks_archive_confirm_reset"
KEY_CARDS = "tasks_cards"
KEY_CARDS_AT = "tasks_cards_at"
KEY_EXTERNAL = "tasks_external_id"
KEY_ACTION = "tasks_action"
KEY_FORCE = "tasks_force"

#: Метки чек-листа, к которым относятся действия экрана (этап T4).
STATUS_LABEL = "TC-TASK-02"
DIAG_LABEL = "TC-TASK-01"
WATCH_LABEL = "TC-TASK-04"
SERIES_LABEL = "TC-TASK-05"
EXTERNAL_LABEL = "TC-TASK-08"

DEFAULT_DURATION = 2
MIN_DURATION = 1
MAX_DURATION = 300
DEFAULT_LIMIT = 100
PER_PAGE = 25

#: Сценарии проверок, доступные из карточки задачи (кнопка «выполнить»).
CARD_SCENARIOS: tuple[str, ...] = (
    "TC-TASK-01",
    "TC-TASK-02",
    "TC-TASK-03",
    "TC-TASK-04",
    "TC-TASK-05",
    "TC-TASK-06",
    "TC-TASK-07",
)


def render() -> None:
    """Отрисовывает экран «$Задачи» (монитор $задач, FR-T11)."""
    st.title(glossary.screen_label("tasks"))
    st.caption(
        "Монитор $задач: список $задач сервера (фильтры и пагинация), наблюдение с историей "
        "переходов FSM-1, карточка $задачи с прогонами, диагностика `celery-test` и наблюдение "
        "за внешними задачами. Команды выполняются только по `available_actions`."
    )
    st.caption(glossary.PREFIX_HINT)
    render_flash()

    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес испытуемого сервера не задан (`TRAINING_SERVER_BASE_URL` в `.env`).")
        return

    monitor = state.task_monitor()
    if monitor is None:
        st.error("Монитор задач недоступен: проверьте настройки подключения к стенду.")
        return

    session = state.current_session()
    focus = st.session_state.pop(KEY_FOCUS, None)
    if focus:
        st.session_state[KEY_CARD_TASK] = str(focus)

    if session is None:
        st.warning(
            "Сессия испытаний не выбрана: наблюдение и переходы FSM-1 выполняются, "
            "но в отчёт об испытаниях не попадут (экран «Сессия испытаний»)."
        )
        _render_kpi(None)
        st.divider()
        _render_tabs(session=None, monitor=monitor, runtime=runtime)
        return

    adopted = monitor.adopt_from_session(session)
    if adopted:
        state.store_session(session)
        set_flash(
            "info",
            "Подхвачены задачи из вызовов консоли: "
            + ", ".join(item["task_id"] for item in adopted),
        )

    _auto_poll(session, monitor)
    _render_kpi(session)
    st.divider()
    _render_tabs(session=session, monitor=monitor, runtime=runtime)


def _auto_poll(session: TestSession, monitor: TaskMonitor) -> None:
    """Опрашивает задачи, у которых истёк интервал поллинга (интерфейс не блокируется).

    Один опрос за перерисовку по каждой «созревшей» задаче: так монитор работает
    сам, а оператор видит переходы FSM-1 без ручных нажатий.
    """
    pending = monitor.due(session)
    if not pending:
        return

    changed: list[str] = []
    for task_id in pending:
        poll = monitor.poll_once(session, task_id)
        if poll.changed:
            changed.append(f"{task_id[:8]}… → {status_label(poll.status)}")
    state.store_session(session)
    if changed:
        set_flash("info", "Опрос задач: " + "; ".join(changed))


def _render_kpi(session: TestSession | None) -> None:
    """KPI монитора: наблюдаемых, активных, завершённых, прерванных, внешних."""
    summary: dict[str, Any] = (
        tasks_summary(session)
        if session is not None
        else {
            "total": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "external": 0,
        }
    )
    columns = st.columns(5)
    columns[0].metric("Наблюдаемых", summary["total"])
    columns[1].metric("Активных", summary["active"])
    columns[2].metric("Завершённых", summary["completed"])
    columns[3].metric("Прерванных и с ошибкой", summary["failed"])
    columns[4].metric("Внешних", summary["external"])
    if session is not None and summary["total"]:
        st.caption(
            f"опросов выполнено: {summary['polls']} · на активном наблюдении: "
            f"{summary['watching']} · по статусам: "
            + ", ".join(
                f"{status_label(key)} — {value}"
                for key, value in sorted(summary["by_status"].items())
            )
        )


def _render_tabs(
    *,
    session: TestSession | None,
    monitor: TaskMonitor,
    runtime: state.Runtime,
) -> None:
    """Вкладки экрана «Задачи»."""
    st.caption(
        f"Стенд: {runtime.settings.base_url} · интервал поллинга по умолчанию: "
        f"{monitor.interval} с · наблюдение хранится в файле сессии (BR-R5, BR-R6)."
    )
    tabs = st.tabs(
        [
            "📋 Список $задач",
            "⏱️ Наблюдение",
            f"🔎 {glossary.term('task_card')}",
            "🩺 Диагностика",
            "🔗 Внешняя задача",
        ]
    )
    with tabs[0]:
        _render_server_list(session=session, monitor=monitor)
    with tabs[1]:
        _render_observation(session=session, monitor=monitor)
    with tabs[2]:
        _render_card(session=session, monitor=monitor)
    with tabs[3]:
        _render_diagnostics(session=session, monitor=monitor)
    with tabs[4]:
        _render_external(session=session, monitor=monitor)


def _observed_options(session: TestSession | None, monitor: TaskMonitor) -> dict[str, str]:
    """Подписи наблюдаемых задач → `task_id` (для выпадающих списков)."""
    if session is None:
        return {}
    return {
        f"{item.task_id} · {item.task_type or '—'} · {item.status_label}": item.task_id
        for item in monitor.observed(session)
    }


def _pick_observed(
    session: TestSession | None,
    monitor: TaskMonitor,
    *,
    key: str,
    label: str,
    empty_hint: str,
) -> str:
    """Выбор задачи из наблюдаемых (возвращает `task_id` или пустую строку)."""
    options = _observed_options(session, monitor)
    if not options:
        st.info(empty_hint)
        return ""
    chosen = st.selectbox(label, list(options), key=key)
    return options[chosen]


def _open_console(spec_key: str, task_id: str) -> None:
    """Открывает консоль запросов с подставленным `task_id` (переход «открыть в консоли»)."""
    spec = ep.find(spec_key)
    if spec is None:
        set_flash("warning", f"Операция {spec_key} не найдена в реестре консоли.")
        return
    st.session_state["console_module"] = spec.module
    st.session_state[f"console_op_{spec.module}"] = spec.menu_label
    st.session_state[f"console_pp_{spec.key}_task_id"] = task_id
    st.session_state["pult_screen"] = "console"
    st.rerun()


def _open_journal(label: str) -> None:
    """Переход в «Журнал» с фильтром по метке проверки (диапазон записей)."""
    st.session_state["logs_label"] = label
    st.session_state["pult_screen"] = "logs"
    st.rerun()


def _duration_text(start_iso: str) -> str:
    """Прошедшее время от момента (ISO) до сейчас — для таблицы наблюдения."""
    if not start_iso:
        return "—"
    try:
        started = datetime.fromisoformat(start_iso)
    except ValueError:
        return "—"
    seconds = max(0.0, (datetime.now() - started).total_seconds())
    if seconds < 60:
        return f"{seconds:.0f} с"
    if seconds < 3600:
        return f"{seconds / 60:.1f} мин"
    return f"{seconds / 3600:.1f} ч"


def _short_value(value: Any, limit: int = 60) -> str:
    """Короткое текстовое представление значения (промежуточный результат, тело)."""
    if value in (None, "", {}):
        return "—"
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


#: Колонки таблицы «Список сервера» (одним местом: состав колонок проверяют тесты).
LIST_COLUMNS: tuple[str, ...] = (
    "Задача",
    "Тип",
    "Статус",
    "Источник",
    "Наблюдение",
    "Проверка",
    "Название",
    "Описание",
    "Создана",
    "Начало",
    "Окончание",
    "Архив",
)

#: Виды списка сервера: активные задачи, архив, всё вместе.
VIEW_ACTIVE = "🗂 Активные"
VIEW_ARCHIVE = "📦 Архив"
VIEW_ALL = "Все"
LIST_VIEWS: tuple[str, ...] = (VIEW_ACTIVE, VIEW_ARCHIVE, VIEW_ALL)

#: Ячейка статуса, когда он неизвестен ни из одного источника.
NO_STATUS_TEXT = "— нет данных"

#: Цвета ячеек колонки «Статус» (ключ — текст ячейки, значение — CSS-цвет).
STATUS_TEXT_COLORS: dict[str, str] = {
    status_text(status): status_color(status) for status in STATUS_ORDER
}
STATUS_TEXT_COLORS[NO_STATUS_TEXT] = "grey"


def _status_style(value: Any) -> str:
    """CSS для ячейки «Статус» (`pandas.Styler`): цвет и полужирный шрифт."""
    color = STATUS_TEXT_COLORS.get(str(value))
    return f"color: {color}; font-weight: 600" if color else ""


def _styled_table(rows: Sequence[Mapping[str, Any]]) -> Any:
    """Таблица списка с раскрашенной колонкой «Статус».

    Пиктограмма статуса (`▶`, `✔`, `✖`, `?`) читается и без цвета, а цвет добавляет
    второй канал различения: исполняется задача сейчас или завершилась (NFR-6).
    Порядок колонок фиксируется `LIST_COLUMNS` — иначе `pandas` собирает его из
    первого словаря, и подсказки `column_config` могут «съехать».

    Returns:
        `pandas.Styler` — `st.dataframe` принимает его напрямую (в тестах значение
        элемента остаётся обычным `DataFrame`).
    """
    frame = pd.DataFrame(list(rows), columns=list(LIST_COLUMNS))
    return frame.style.map(_status_style, subset=["Статус"])


def _archive_text(entry: task_snapshot.TaskSnapshotEntry | None) -> str:
    """Колонка «Архив»: когда задача попала в архив и как (авто или оператором)."""
    if entry is None or not entry.archived:
        return "—"
    moment = str(entry.archived_at).replace("T", " ")[:16]
    how = "авто" if entry.archived_by == task_snapshot.ARCHIVED_BY_AUTO else "оператор"
    return f"{moment} ({how})"


def _task_row(
    task: CeleryTask,
    observation: TaskObservation | None,
    view: TaskStatusView,
    entry: task_snapshot.TaskSnapshotEntry | None,
    *,
    full_values: bool = False,
) -> dict[str, Any]:
    """Строка таблицы «Список сервера»: поля ответа списка, статус и наблюдение.

    `GET /api/tasks/` статус задачи не возвращает (в спецификации у `CeleryTask`
    только `type`, `name`, `description`, `id`, `created_at`), поэтому «Статус»
    берётся из трёх источников (`monitor.task_status_view`): карточки
    `GET /api/tasks/{id}` (догружается батчами с меткой `TC-TASK-02`), снимка
    `task_snapshot.json` (последний известный серверный статус) и последнего опроса
    задачи в сессии. Колонка «Источник» показывает, откуда взят статус, — оператор
    видит, насколько данные свежие, и не принимает снимок за свежий ответ.

    Args:
        task: элемент списка задач (`CeleryTask`).
        observation: строка наблюдения монитора или None.
        view: статус строки (карточка → снимок → опрос).
        entry: запись снимка (архив, времена прогона) или None.
        full_values: не сокращать идентификатор, название и описание.
    """
    return {
        "Задача": str(task.id) if full_values else short_id(task.id),
        "Тип": str(task.type),
        "Статус": view.text if view.known else NO_STATUS_TEXT,
        "Источник": view.source,
        "Наблюдение": "⏱️ да" if observation else "—",
        "Проверка": (observation.check_id if observation else "") or "—",
        "Название": str(task.name) if full_values else head_text(task.name),
        "Описание": str(task.description or "") if full_values else head_text(task.description),
        "Создана": task.created_at.isoformat(timespec="seconds"),
        "Начало": view.start_time or "—",
        "Окончание": view.end_time or "—",
        "Архив": _archive_text(entry),
    }


def _snapshot_row(
    entry: task_snapshot.TaskSnapshotEntry, *, full_values: bool = False
) -> dict[str, Any]:
    """Строка архива для задачи, которой больше нет в выдаче `GET /api/tasks/`.

    Задача могла быть удалена или отфильтрована сервером, но её след в архиве нужен:
    оператор видит, чем задача закончилась, и не ищет её заново.
    """
    return {
        "Задача": entry.task_id if full_values else short_id(entry.task_id),
        "Тип": entry.type or "—",
        "Статус": status_text(entry.status) if entry.status else NO_STATUS_TEXT,
        "Источник": f"{SOURCE_SNAPSHOT} · нет в списке",
        "Наблюдение": "—",
        "Проверка": "—",
        "Название": entry.name if full_values else head_text(entry.name),
        "Описание": "—",
        "Создана": "—",
        "Начало": entry.start_time or "—",
        "Окончание": entry.end_time or "—",
        "Архив": _archive_text(entry),
    }


@dataclass
class _ListRow:
    """Строка списка сервера: значения таблицы и служебные данные для действий."""

    task_id: str
    row: dict[str, Any]
    archived: bool = False
    ended_at: str = ""
    task: CeleryTask | None = None


def _cards_stale(card: Mapping[str, Any] | None, now: datetime) -> bool:
    """True, если карточки строки нет или её данные старше `state.STATUS_CACHE_TTL`.

    Неудачный запрос тоже считается «свежим» до истечения TTL: иначе недоступный
    стенд вызывал бы бесконечные повторные запросы при каждой перерисовке.
    """
    if not isinstance(card, Mapping):
        return True
    try:
        fetched = datetime.fromisoformat(str(card.get("fetched_at") or ""))
    except ValueError:
        return True
    return (now - fetched).total_seconds() >= state.STATUS_CACHE_TTL


def _fetch_cards(
    keys: tuple[str, ...], *, snapshot: task_snapshot.TaskSnapshot, progress: bool = True
) -> tuple[int, int]:
    """Запрашивает карточки задач батчами (`GET /api/tasks/{id}`), обновляя снимок.

    Список сервера статуса не содержит, поэтому каждая строка со статусом — это
    отдельный запрос (N+1, замечание P1 к API). Запросы идут батчами по
    `state.STATUS_BATCH` (внутри батча — параллельно, `STATUS_FETCH_WORKERS`),
    с меткой `TC-TASK-02` и видимым прогрессом: оператор видит цену действия, а
    стенд не получает сотню запросов разом.

    Returns:
        Кортеж (получено карточек, ошибок).
    """
    received = failed = 0
    total = len(keys)
    bar = st.progress(0.0, text=f"Статусы задач: 0 из {total}") if progress and total else None
    for start in range(0, total, state.STATUS_BATCH):
        batch = keys[start : start + state.STATUS_BATCH]
        with checks_engine.check_label(STATUS_LABEL):
            fetched, errors = state.load_task_cards(batch)
        for key, payload in fetched.items():
            snapshot.observe(key, **payload)
        for key, error in errors.items():
            snapshot.observe(key, error=error)
        received += len(fetched)
        failed += len(errors)
        if bar is not None:
            done = min(total, start + len(batch))
            bar.progress(done / total, text=f"Статусы задач: {done} из {total} — карточки (N+1)")
    return received, failed


def _load_status_batch(*, keys: tuple[str, ...], snapshot: task_snapshot.TaskSnapshot) -> None:
    """Догружает статусы видимых строк фоновым батчем, копя карточки в `session_state`.

    Таблица рисуется до этого вызова, поэтому список виден сразу, а статусы
    «подтягиваются» батчами: за одну перерисовку запрашивается `state.STATUS_BATCH`
    карточек, затем страница обновляется и берётся следующий батч. Так экран не
    зависает на минуту, когда в списке сотня задач, и не дёргает стенд зря: уже
    загруженные карточки живут в `session_state` до истечения TTL.
    """
    cards = dict(st.session_state.get(KEY_CARDS) or {})
    now = datetime.now()
    pending = [key for key in keys if _cards_stale(cards.get(key), now)]
    if not pending:
        return

    batch = tuple(pending[: state.STATUS_BATCH])
    with checks_engine.check_label(STATUS_LABEL):
        fetched, errors = state.load_task_cards(batch)
    stamp = now.isoformat(timespec="seconds")
    for key, payload in fetched.items():
        cards[key] = {**payload, "fetched_at": stamp}
        snapshot.observe(key, **payload)
    for key, error in errors.items():
        cards[key] = {"status": "", "fetched_at": stamp, "error": error}
        snapshot.observe(key, error=error)
    st.session_state[KEY_CARDS] = cards
    st.session_state[KEY_CARDS_AT] = stamp
    task_snapshot.save_snapshot(snapshot)

    if len(pending) > len(batch):
        done = len(keys) - len(pending) + len(batch)
        st.progress(
            done / max(1, len(keys)),
            text=f"Статусы задач: {done} из {len(keys)} — карточки батчами по {state.STATUS_BATCH}",
        )
    # таблица рисуется до этого вызова, поэтому после батча страница перерисовывается:
    # так статусы появляются на экране сразу, без действий оператора. Повторных
    # запросов нет — свежие карточки живут в `session_state` до истечения TTL.
    repeat_flash()
    st.rerun()


def _render_server_list(*, session: TestSession | None, monitor: TaskMonitor) -> None:
    """Вкладка «Список сервера»: фильтры, виды, статусы, архив и действия (TC-TASK-02)."""
    st.caption(
        "`GET /api/tasks/` — фильтры `task_type`, `status` и период создания (TC-TASK-02). "
        "Статуса в ответе списка нет: он берётся из карточки $задачи (`GET /api/tasks/{id}`, "
        "N+1) и запоминается в снимке `acceptance_data/task_snapshot.json`, поэтому после "
        "перезапуска пульта статусы видны сразу. Список читается целиком (`limit` стенда не "
        f"ограничен, пульт режет его на {state.LOAD_TASK_CAP} задач) и сортируется по "
        "«создана ↓»: порядок ответа сервера не отсортирован."
    )
    col_type, col_status, col_period, _ = st.columns(4)
    task_type = col_type.selectbox(
        "Тип задачи", ["", *(str(item) for item in TaskType)], key=KEY_FILTER_TYPE
    )
    status = col_status.selectbox(
        "Статус", ["", *(str(item) for item in TaskStatus)], key=KEY_FILTER_STATUS
    )
    with_period = col_period.checkbox(
        "Фильтр по периоду",
        value=False,
        key=KEY_FILTER_PERIOD,
        help="Выключен — показываются все задачи сервера. Период передаётся только как "
        "дата-время: строка из одной даты отклоняется стендом (422, `datetime_parsing`).",
    )
    today = date.today()
    start = end = None
    if with_period:
        start = col_period.date_input(
            "Созданы с", value=today - timedelta(days=7), key=KEY_FILTER_FROM
        )
        end = col_period.date_input("Созданы по", value=today, key=KEY_FILTER_TO)
    start_date, end_date = day_bounds(start, end)
    if start_date or end_date:
        st.caption(
            f"Период: `{start_date or '—'}` … `{end_date or '—'}` — конец периода не "
            "включается (это начало следующих суток, `end_date` на сервере исключающая). "
            "Время задач — UTC сервера, как в таблице ниже."
        )

    col_view, col_full, col_fresh = st.columns([2, 1, 2])
    view_name = col_view.radio(
        "Вид списка",
        LIST_VIEWS,
        horizontal=True,
        key=KEY_VIEW,
        help="«Активные» — задачи, которых нет в архиве; «Архив» — завершённые более суток "
        "назад; «Все» — обе части вместе.",
    )
    col_full.checkbox(
        "Полные значения",
        value=False,
        key=KEY_FULL_VALUES,
        help="Выключено — в таблице «хвост» идентификатора (8 символов) и первые 21 символ "
        "названия и описания; полные значения всегда доступны в панели строки ниже.",
    )
    col_fresh.checkbox(
        "Показать перенесённые за сутки",
        value=False,
        key=KEY_ARCHIVE_FRESH,
        disabled=view_name == VIEW_ACTIVE,
        help="Вид «Архив» по умолчанию показывает только задачи, завершённые более суток "
        "назад (правило архива). Включённый флажок добавляет «только что перенесённые»; "
        "в виде «Активные» флажок не нужен.",
    )

    col_refresh, col_statuses, col_page_size = st.columns([1, 1, 1])
    if col_refresh.button("⟳ Обновить список", key="tasks_refresh"):
        state.refresh_tasks()
        st.session_state.pop(KEY_CARDS, None)
        st.rerun()
    if col_statuses.button(
        "⟳ Обновить статусы",
        key="tasks_statuses_refresh",
        help="Сбрасывает кэш карточек (30 с) и запрашивает статусы видимых строк заново: "
        "`GET /api/tasks/{id}` — запрос на каждую строку (N+1).",
    ):
        state.clear_task_cards()
        st.session_state.pop(KEY_CARDS, None)
        st.session_state.pop(KEY_CARDS_AT, None)
        set_flash("info", "Кэш карточек сброшен: статусы видимых строк запрашиваются заново.")
        st.rerun()
    per_page = int(
        col_page_size.selectbox(
            "Записей на странице", (10, 25, 50, 100), index=1, key="tasks_per_page"
        )
    )

    tasks, _total, error = state.load_tasks(
        task_type=str(task_type),
        status=str(status),
        start_date=start_date,
        end_date=end_date,
        limit=state.LOAD_TASK_CAP,
    )
    if error:
        st.error(error)
        return
    if not tasks:
        st.info("Сервер не вернул задач по заданным фильтрам.")
        return

    moment = datetime.now()
    ordered = sorted(tasks, key=lambda item: (item.created_at, str(item.id)), reverse=True)
    full_values = bool(st.session_state.get(KEY_FULL_VALUES))
    include_fresh = bool(st.session_state.get(KEY_ARCHIVE_FRESH))
    observed = monitor.observation_index(session)
    cards = dict(st.session_state.get(KEY_CARDS) or {})

    snapshot = task_snapshot.load_snapshot()
    before = json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True)
    snapshot.mark_present({str(item.id): (str(item.type), str(item.name)) for item in ordered})
    archived_auto, unarchived_auto = snapshot.refresh_auto(now=moment)
    for task_id in archived_auto:
        log_event(
            "tasks_archived_auto",
            f"Авто-архив: задача {task_id} завершена более суток назад",
            module="tasks",
            payload={
                "task_id": task_id,
                "rule_hours": task_snapshot.ARCHIVE_AFTER.total_seconds() / 3600,
            },
        )
    for task_id in unarchived_auto:
        log_event(
            "tasks_unarchived_auto",
            f"Задача {task_id} вернулась в работу: метка архива снята",
            module="tasks",
            payload={"task_id": task_id},
        )
    if archived_auto:
        set_flash(
            "info",
            f"Авто-архив: перенесено задач — {len(archived_auto)} (завершены более суток назад).",
        )
    if unarchived_auto:
        set_flash(
            "info",
            f"Из архива возвращено задач — {len(unarchived_auto)}: они снова в работе.",
        )
    if json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True) != before:
        task_snapshot.save_snapshot(snapshot)

    def status_view_of(task_id: str) -> TaskStatusView:
        """Статус строки: карточка сервера → снимок → последний опрос сессии."""
        observation = observed.get(task_id)
        return task_status_view(
            card=cards.get(task_id),
            snapshot=snapshot.entries.get(task_id),
            poll_status=observation.status if observation else "",
        )

    active: list[_ListRow] = []
    archive: list[_ListRow] = []
    listed: set[str] = set()
    for task in ordered:
        key = str(task.id)
        listed.add(key)
        entry = snapshot.entries.get(key)
        view = status_view_of(key)
        item = _ListRow(
            task_id=key,
            row=_task_row(task, observed.get(key), view, entry, full_values=full_values),
            archived=bool(entry is not None and entry.archived),
            ended_at=(entry.end_time if entry is not None else "") or view.end_time,
            task=task,
        )
        if entry is None or not entry.archived:
            active.append(item)
        elif include_fresh or snapshot.is_aged(entry, now=moment):
            # «только что перенесённые» скрыты, пока оператор не включит флажок: иначе
            # архив «прыгает» сразу после переноса завершённой задачи вручную
            archive.append(item)

    for record in snapshot.archived_entries(include_fresh=include_fresh, now=moment):
        if record.task_id in listed:
            continue
        archive.append(
            _ListRow(
                task_id=record.task_id,
                row=_snapshot_row(record, full_values=full_values),
                archived=True,
                ended_at=record.end_time,
            )
        )
    archive.sort(key=lambda item: item.ended_at, reverse=True)

    kpi = snapshot.kpi(now=moment)
    st.caption(
        f"На сервере: {len(ordered)} · статус известен: {kpi['known']} "
        f"(без данных: {kpi['unknown']}) · активных: {kpi['active']} · завершено за сутки: "
        f"{kpi['finished_today']} · в архиве: {kpi['archived']} "
        f"(перенесено за сутки: {kpi['archived_fresh']}) · статусы: {status_legend()}"
    )
    _render_archive_panel(snapshot=snapshot, tasks=ordered, cards=cards)

    if view_name == VIEW_ARCHIVE:
        rows = list(archive)
    elif view_name == VIEW_ALL:
        rows = [*active, *archive]
    else:
        rows = list(active)
    if not rows:
        if view_name == VIEW_ARCHIVE:
            st.info(
                "В архиве пусто: сюда попадают задачи, завершённые более суток назад. "
                "«Только что перенесённые» показывает флажок «Показать перенесённые за сутки»."
            )
        else:
            st.info(
                "В этом виде нет задач: активные и архив разделены правилом «завершена ≥ "
                "суток назад» — архив виден в виде «📦 Архив»."
            )
        return

    page_number = int(st.session_state.get(KEY_PAGE) or 1)
    page = paginate(rows, page_number, per_page)
    st.dataframe(
        _styled_table([item.row for item in page.items]),
        hide_index=True,
        use_container_width=True,
        lazy=False,
        column_config={
            "Задача": st.column_config.TextColumn(
                width="small", help="`id` задачи: в таблице — последние 8 символов"
            ),
            "Статус": st.column_config.TextColumn(
                help="Статус FSM-1 последнего прогона: из карточки, снимка или опроса"
            ),
            "Источник": st.column_config.TextColumn(
                help="Откуда взят статус: карточка (свежий запрос), снимок (последний "
                "известный), опрос (текущая сессия) или «нет данных»"
            ),
            "Название": st.column_config.TextColumn(help="Первые 21 символ"),
            "Описание": st.column_config.TextColumn(help="Первые 21 символ"),
            "Архив": st.column_config.TextColumn(
                help="Когда задача перенесена в архив и как: автоматически или оператором"
            ),
        },
    )
    st.caption(
        "«Источник»: `карточка` — статус получен запросом карточки $задачи сейчас, `снимок` — "
        "последний известный из `task_snapshot.json`, `опрос` — из наблюдения текущей сессии. "
        "Колонка «Статус» раскрашена: зелёный — исполняется или завершена, жёлтый — пауза, "
        "красный — ошибка, оранжевый — прервана, серый — нет данных. Статусы строк "
        f"получены: {st.session_state.get(KEY_CARDS_AT) or '—'} (кэш карточек "
        f"{state.STATUS_CACHE_TTL:.0f} с, кнопка «⟳ Обновить статусы»)."
    )
    render_pagination(KEY_PAGE, page, show_size=False)

    keys = tuple(item.task_id for item in page.items if not item.archived)
    _load_status_batch(keys=keys, snapshot=snapshot)

    st.divider()
    options = {
        f"{short_id(item.task_id)} · {item.row['Тип']} · {head_text(item.row['Название'])}": item
        for item in page.items
        if item.task is not None
    }
    if not options:
        st.caption(
            "Полные значения и действия недоступны: в архиве только записи снимка — таких "
            "задач нет в выдаче сервера."
        )
        return
    chosen = st.selectbox(
        "Строка списка: полные значения и действия", list(options), key="tasks_choose"
    )
    item = options[chosen]
    selected = item.task
    with st.expander("🔖 Полные значения строки", expanded=False):
        st.caption(
            "В таблице `id` сокращён до последних 8 символов, название и описание — до 21 "
            "символа. Ниже полные значения, каждое копируется кнопкой справа."
        )
        st.code(str(item.task_id), language=None)
        st.code(str(selected.name) if selected else item.row["Название"], language=None)
        st.code(str(selected.description or "—") if selected else "—", language=None)
        created = selected.created_at.isoformat(timespec="seconds") if selected else ""
        st.caption(
            f"Тип: `{item.row['Тип']}` · создана: `{created or '—'}` · статус: "
            f"`{item.row['Статус']}` (источник: {item.row['Источник']}) · архив: "
            f"{item.row['Архив']}"
        )
    if selected is None:  # строка архива без задачи в выдаче: действий нет
        return
    col_watch, col_card, col_console = st.columns(3)
    if col_watch.button("⏱️ Наблюдать", key="tasks_watch", type="primary"):
        if session is None:
            set_flash("warning", "Сессия не выбрана: наблюдение невозможно.")
        else:
            monitor.register(
                session,
                task_id=str(selected.id),
                task_type=str(selected.type),
                name=str(selected.name),
            )
            state.store_session(session)
            set_flash("success", f"Задача {selected.id} поставлена на наблюдение.")
        st.rerun()
    if col_card.button("🔎 Открыть карточку $задачи", key="tasks_open"):
        st.session_state[KEY_CARD_TASK] = str(selected.id)
        set_flash(
            "info",
            f"$Задача {selected.id}: открывается на вкладке «{glossary.term('task_card')}».",
        )
        st.rerun()
    if col_console.button("📡 Открыть в консоли", key="tasks_open_console"):
        _open_console("get /api/tasks/{task_id}", str(selected.id))


def _render_archive_panel(
    *,
    snapshot: task_snapshot.TaskSnapshot,
    tasks: Sequence[CeleryTask],
    cards: Mapping[str, Any],
) -> None:
    """Панель «Архив задач»: правило, цена действия и перенос завершённых задач.

    Архив — часть рабочих заметок пульта (`acceptance_data/task_snapshot.json`), но
    перенос в него меняет то, что видит оператор, поэтому действие выполняется только
    после подтверждения. Задачи с неизвестным статусом сначала опрашиваются
    карточками (N+1) — об этом сказано до нажатия, вместе с числом запросов.
    """
    with st.expander("📦 Архив задач", expanded=False):
        st.caption(
            "Задачи с терминальным статусом (`completed`, `failed`, `interrupted`, "
            "`not_found`) уходят в архив автоматически, когда завершены более суток назад; "
            "задача, вернувшаяся в работу, из архива возвращается. Архив и последние "
            "известные статусы лежат в `acceptance_data/task_snapshot.json` и переживают "
            "перезапуск пульта: в файл сессии эти данные не пишутся."
        )

        def known_status(key: str) -> bool:
            """True, если статус задачи известен из карточки или снимка."""
            card = cards.get(key)
            if isinstance(card, Mapping) and str(card.get("status") or ""):
                return True
            entry = snapshot.entries.get(key)
            return bool(entry is not None and entry.status)

        unknown = tuple(str(task.id) for task in tasks if not known_status(str(task.id)))
        ready = len(snapshot.archive_candidates())
        st.caption(
            f"Готовы к переносу: {ready} · статус неизвестен у {len(unknown)} задач(и) — их "
            f"нужно опросить карточками (`GET /api/tasks/{{id}}`, запрос на задачу, батчи по "
            f"{state.STATUS_BATCH}, кэш {state.STATUS_CACHE_TTL:.0f} с)."
        )
        if st.session_state.pop(KEY_ARCHIVE_RESET, False):
            # сброс подтверждения до создания виджета: иначе состояние виджета
            # нельзя менять после его отрисовки (Streamlit)
            st.session_state[KEY_ARCHIVE_CONFIRM] = False
        confirmed = st.checkbox(
            "Подтверждаю перенос завершённых задач в архив",
            value=False,
            key=KEY_ARCHIVE_CONFIRM,
        )
        label = f"📦 Перенести завершённые в архив ({ready})"
        if unknown:
            label += f" · опрос карточек: {len(unknown)}"
        if not st.button(label, key="tasks_archive_completed", disabled=not confirmed):
            return

        received = failed = 0
        if unknown:
            received, failed = _fetch_cards(unknown, snapshot=snapshot, progress=True)
        moved = snapshot.archive_completed(by=task_snapshot.ARCHIVED_BY_OPERATOR)
        task_snapshot.save_snapshot(snapshot)
        state.clear_task_cards()
        st.session_state.pop(KEY_CARDS, None)
        summary = task_snapshot.archive_summary(moved, snapshot)
        log_event(
            "tasks_archived",
            f"Архив задач (оператор): {summary}",
            module="tasks",
            payload={
                "moved": moved,
                "fetched": received,
                "errors": failed,
                "by": task_snapshot.ARCHIVED_BY_OPERATOR,
            },
        )
        details = [f"опрос карточек: {received}"] if received else []
        if failed:
            details.append(f"ошибок: {failed}")
        text = f"Архив задач: {summary}" + (f" · {', '.join(details)}" if details else "")
        set_flash("success" if moved else "info", text)
        st.session_state[KEY_ARCHIVE_RESET] = True
        st.rerun()


def _render_observation(*, session: TestSession | None, monitor: TaskMonitor) -> None:
    """Вкладка «Наблюдение»: таблица задач, опрос и управление поллингом."""
    if session is None:
        st.info(
            "Сессия испытаний не выбрана: наблюдение работает, но история переходов не "
            "сохраняется. Создайте сессию на экране «Сессия испытаний»."
        )
        return

    observations = monitor.observed(session)
    if not observations:
        st.info(
            "Ни одна задача не наблюдается. Поставьте задачу на наблюдение на вкладке "
            "«Список задач», запустите `celery-test` на вкладке «Диагностика» или введите "
            "`task_id` внешней задачи на вкладке «Внешняя задача»."
        )
        return

    st.dataframe(
        [
            {
                "Задача": item.task_id,
                "Тип": item.task_type or "—",
                "Происхождение": item.origin,
                "Проверка": item.check_id or "—",
                "Статус": f"{item.status_icon} {item.status_label}",
                "Прошло": _duration_text(item.first_seen),
                "Опросов": item.polls,
                "Интервал, с": item.interval,
                "Поллинг": "активен" if item.active else "пауза",
                "Промежуточный результат": _short_value(item.intermediate_result),
                "Ошибки опроса": item.errors[-1] if item.errors else "—",
            }
            for item in observations
        ],
        hide_index=True,
        use_container_width=True,
    )

    col_poll, col_hint, col_interval = st.columns([1, 2, 2])
    if col_poll.button("⟳ Опросить все сейчас", key="tasks_poll_all"):
        polls = monitor.poll_all(session, active_only=False)
        state.store_session(session)
        set_flash("success", f"Опрошено задач: {len(polls)}.")
        st.rerun()
    col_hint.caption("Автоопрос: к каждой перерисовке опрашиваются задачи с истёкшим интервалом.")
    col_interval.caption(f"Интервал по умолчанию (`PULT_POLL_INTERVAL`): {monitor.interval} с")

    st.divider()
    task_id = _pick_observed(
        session,
        monitor,
        key="tasks_obs_choose",
        label="Задача наблюдения",
        empty_hint="Наблюдаемых задач пока нет.",
    )
    if not task_id:
        return

    observation = monitor.observed(session)
    current = next((item for item in observation if item.task_id == task_id), None)
    if current is None:
        return
    st.caption(
        f"Статус: {current.status_icon} {current.status_label} · происхождение: {current.origin} · "
        f"проверка: {current.check_id or '—'} · доступные команды: "
        f"{', '.join(current.actions) or '—'}"
    )
    _render_task_actions(
        session=session, monitor=monitor, task_id=task_id, observation=current, scope="obs"
    )


def _render_task_actions(
    *,
    session: TestSession,
    monitor: TaskMonitor,
    task_id: str,
    observation: TaskObservation,
    scope: str,
) -> None:
    """Действия над наблюдаемой задачей: поллинг, команды FSM-1, привязки.

    `scope` (`obs` — вкладка наблюдения, `card` — карточка задачи) входит в ключи
    виджетов: обе вкладки могут показывать действия одной задачи за одну перерисовку.
    """
    col_pause, col_poll, col_interval, col_drop = st.columns(4)
    if col_pause.button(
        "▶ Возобновить поллинг" if not observation.active else "⏸ Пауза поллинга",
        key=f"{scope}_tasks_toggle_{task_id}",
    ):
        monitor.set_active(session, task_id, not observation.active)
        state.store_session(session)
        set_flash("success", "Поллинг обновлён.")
        st.rerun()
    if col_poll.button("⟳ Опросить", key=f"{scope}_tasks_poll_{task_id}"):
        poll = monitor.poll_once(session, task_id)
        state.store_session(session)
        set_flash(
            "success" if poll.ok else "warning",
            f"{status_label(poll.status)} за {poll.journal_seq}" if poll.ok else poll.error,
        )
        st.rerun()
    interval = col_interval.number_input(
        "Интервал поллинга, с",
        min_value=0.5,
        max_value=60.0,
        value=float(observation.interval or 2.0),
        step=0.5,
        key=f"{scope}_tasks_interval_{task_id}",
    )
    if interval != observation.interval:
        monitor.set_interval(session, task_id, float(interval))
        state.store_session(session)
    if col_drop.button("✖ Снять с наблюдения", key=f"{scope}_tasks_drop_{task_id}"):
        monitor.unregister(session, task_id)
        state.store_session(session)
        set_flash("warning", f"Задача {task_id} снята с наблюдения (история сохранена в журнале).")
        st.rerun()

    _render_command_panel(
        session=session,
        monitor=monitor,
        task_id=task_id,
        observation=observation,
        scope=scope,
    )
    _render_attachment_panel(session=session, monitor=monitor, task_id=task_id, scope=scope)


def _render_command_panel(
    *,
    session: TestSession,
    monitor: TaskMonitor,
    task_id: str,
    observation: TaskObservation,
    scope: str,
) -> None:
    """Команды FSM-1: только по `available_actions`, кроме осознанной проверки BR-R3."""
    st.markdown("**Команды FSM-1 (UC-28)**")
    allowed = observation.actions
    if not allowed:
        st.caption(
            f"Для статуса «{observation.status_label}» команд нет: `pause`/`resume`/`interrupt` "
            "доступны только из `running`/`pausing`/`paused`."
        )
    force = st.checkbox(
        "Отправить команду вне допустимого состояния (проверка идемпотентности и запрета "
        "из терминального состояния, TC-TASK-06)",
        key=f"{KEY_FORCE}_{scope}_{task_id}",
    )
    options = list(allowed) or list(TASK_COMMANDS)
    action = st.selectbox("Команда", options, key=f"{KEY_ACTION}_{scope}_{task_id}")
    if st.button(
        "▶ Отправить команду",
        key=f"{scope}_tasks_cmd_{task_id}",
        type="primary",
        disabled=not allowed and not force,
    ):
        label = observation.check_id or (SERIES_LABEL if action == "interrupt" else WATCH_LABEL)
        command = monitor.run_command(
            session, task_id, action, force=force or not allowed, label=label
        )
        state.store_session(session)
        st.session_state[f"{scope}_tasks_cmd_result_{task_id}"] = command.as_dict()
        set_flash(
            "success" if command.ok else "warning",
            f"Задача {task_id}: {action} → {command.variant or command.error}",
        )
        st.rerun()

    last = st.session_state.get(f"{scope}_tasks_cmd_result_{task_id}")
    if isinstance(last, dict):
        st.caption(
            f"Последняя команда: `{last.get('action')}` → {last.get('variant') or last.get('error')} "
            f"· запись журнала #{last.get('journal_seq')}"
        )
        with st.expander("Детали последней команды"):
            st.json(last)


def _save_card_artifact(*, session: TestSession, monitor: TaskMonitor, task_id: str) -> None:
    """Сохраняет карточку $задачи (JSON) в артефакты сессии как доказательство."""
    try:
        card = monitor.card(task_id)
    except ClientError as exc:
        set_flash("warning", f"Карточка $задачи не получена: {exc}")
        st.rerun()
        return

    payload = monitor.card_snapshot(card)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_DIR / f"task_{task_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    add_artifact(session, kind="task_card", path=path, note=f"карточка $задачи {task_id}")
    state.store_session(session)
    set_flash("success", f"Карточка $задачи сохранена в артефакты: {path.name}.")
    st.rerun()


def _render_attachment_panel(
    *, session: TestSession, monitor: TaskMonitor, task_id: str, scope: str
) -> None:
    """Привязка задачи к проверке, замечание к API, артефакт, переходы в консоль/журнал."""
    st.divider()
    st.markdown("**Привязки, доказательства и переходы**")
    record = find_task(session, task_id) or {}
    current_check = str(record.get("check_id") or "")

    options = [""] + [f"{spec.check_id} · {spec.title}" for spec in catalog.CHECKS]
    index = next(
        (position for position, option in enumerate(options) if option.startswith(current_check)),
        0,
    )
    chosen = st.selectbox(
        "Проверка чек-листа", options, index=index, key=f"{scope}_tasks_check_{task_id}"
    )
    col_attach, col_note, col_artifact, col_console, col_logs = st.columns(5)
    if col_attach.button("🔗 Прикрепить к проверке", key=f"{scope}_tasks_attach_{task_id}"):
        spec_id = chosen.split(" · ")[0] if chosen else ""
        monitor.attach_to_check(session, task_id, spec_id)
        state.store_session(session)
        set_flash("success", f"Задача привязана к проверке {spec_id or '(снята привязка)'}.")
        st.rerun()
    col_note.caption("Замечание к API — в блоке ниже.")
    if col_artifact.button("💾 Карточка в артефакты", key=f"{scope}_tasks_artifact_{task_id}"):
        _save_card_artifact(session=session, monitor=monitor, task_id=task_id)
    if col_console.button("📡 В консоли", key=f"{scope}_tasks_console_{task_id}"):
        _open_console("get /api/tasks/{task_id}", task_id)
    if col_logs.button("🧾 Обмены в журнале", key=f"{scope}_tasks_logs_{task_id}"):
        _open_journal(current_check or MONITOR_LABEL)

    with st.expander("✍ Создать замечание к API по этой задаче"):
        _task_note_form(session=session, task_id=task_id, scope=scope)


def _task_note_form(*, session: TestSession, task_id: str, scope: str) -> None:
    """Форма замечания к API с предзаполненным фактом по наблюдаемой задаче."""
    record = find_task(session, task_id) or {}
    history = task_history(session, task_id)
    last = history[-1] if history else {}
    default_fact = (
        f"Наблюдение за задачей {task_id} ({record.get('type') or 'тип не определён'}): "
        f"статус «{status_label(record.get('status'))}», опросов {((record.get('poll') or {}).get('polls')) or 0}; "
        f"последний переход: {last.get('previous') or '—'} → {last.get('status') or '—'}"
        f" (запись журнала #{last.get('journal_seq')}); "
        f"ошибки опроса: {record.get('errors') or 'нет'}"
    )
    module_options = list(notes.MODULES)
    col_priority, col_module = st.columns(2)
    priority = col_priority.selectbox(
        "Приоритет",
        list(notes.PRIORITIES),
        index=1,
        key=f"{scope}_tasks_note_priority_{task_id}",
    )
    module = col_module.selectbox(
        "Модуль",
        module_options,
        index=module_options.index("Task service"),
        key=f"{scope}_tasks_note_module_{task_id}",
    )
    title = st.text_input(
        "Заголовок замечания",
        value=f"Задача {task_id}: ",
        key=f"{scope}_tasks_note_title_{task_id}",
    )
    fact = st.text_area(
        "Факт (что произошло)", value=default_fact, key=f"{scope}_tasks_note_fact_{task_id}"
    )
    expected = st.text_area(
        "Ожидание (как должно быть по спецификации)", key=f"{scope}_tasks_note_expected_{task_id}"
    )
    reproduction = st.text_area(
        "Воспроизведение",
        value=f"GET /api/tasks/{task_id}; история наблюдения — в файле сессии",
        key=f"{scope}_tasks_note_repro_{task_id}",
    )
    evidence = st.text_input(
        "Доказательства",
        value=f"запись журнала #{record.get('journal_seq')}; история переходов: {len(history)}",
        key=f"{scope}_tasks_note_evidence_{task_id}",
    )
    if st.button(
        "Добавить замечание в сессию", key=f"{scope}_tasks_note_add_{task_id}", type="primary"
    ):
        note = api_notes.manual_note(
            title.strip() or f"Задача {task_id}: замечание по наблюдению",
            module=module,
            endpoint=f"GET /api/tasks/{task_id}",
            priority=priority,
            fact=fact,
            expected=expected,
            reproduction=reproduction,
            check_id=str(record.get("check_id") or "") or None,
            evidence=evidence,
        )
        api_notes.create_note(note, session=session, module="tasks")


def _render_card(*, session: TestSession | None, monitor: TaskMonitor) -> None:
    """Вкладка «Карточка задачи»: прогоны, история FSM-1, команды и сценарии проверок."""
    st.caption(
        "`GET /api/tasks/{task_id}` — `TaskWithRuntimes` с прогонами `runtimes[]`, история "
        "переходов FSM-1 из сессии и команды строго по `available_actions` (TC-TASK-03…07)."
    )
    selected = str(st.session_state.get(KEY_CARD_TASK) or "")
    if session is not None:
        options = _observed_options(session, monitor)
        if options:
            labels = list(options)
            index = next(
                (position for position, label in enumerate(labels) if options[label] == selected), 0
            )
            chosen = st.selectbox("Наблюдаемая задача", labels, index=index, key="tasks_card_pick")
            selected = options[chosen]
            st.session_state[KEY_CARD_TASK] = selected

    col_manual, col_load = st.columns([3, 1])
    manual = col_manual.text_input(
        "task_id задачи (вручную, например для внешней)", value="", key="tasks_card_manual"
    )
    if col_load.button("⟳ Загрузить карточку $задачи", key="tasks_card_load"):
        st.session_state[KEY_CARD_TASK] = (manual.strip() or selected).strip()
        st.rerun()

    task_id = str(st.session_state.get(KEY_CARD_TASK) or "")
    if not task_id:
        st.info("Выберите наблюдаемую задачу или введите `task_id` вручную.")
        return

    if session is not None:
        poll = monitor.poll_once(session, task_id)
        state.store_session(session)
        if not poll.ok and poll.error:
            st.warning(f"Опрос задачи не выполнен: {poll.error}")

    try:
        card: TaskWithRuntimes = monitor.card(task_id)
    except ClientError as exc:
        st.error(f"Карточка задачи не получена: {exc}")
        return

    columns = st.columns(4)
    columns[0].metric("Тип", str(card.type))
    columns[1].metric("Прогонов", len(card.runtimes))
    columns[2].metric("Создана", card.created_at.strftime("%d.%m.%Y %H:%M"))
    current = card.runtimes[-1].status if card.runtimes else ""
    columns[3].metric("Текущий статус", f"{status_icon(current)} {status_label(current)}")
    st.caption(
        f"`{card.name}` · `{card.id}`" + (f" · {card.description}" if card.description else "")
    )

    if card.runtimes:
        st.dataframe(
            [
                {
                    "Статус": f"{status_icon(str(item.status))} {status_label(str(item.status))}",
                    "Начало": item.start_time.isoformat() if item.start_time else "—",
                    "Окончание": item.end_time.isoformat() if item.end_time else "—",
                    "celery_task_id": item.celery_task_id or "—",
                    "Параметры": _short_value(item.parameters),
                    "Промежуточный результат": _short_value(item.intermediate_result),
                    "Результат": _short_value(item.result),
                }
                for item in card.runtimes
            ],
            hide_index=True,
            use_container_width=True,
        )

    if session is not None:
        history = task_history(session, task_id)
        with st.expander(f"История переходов FSM-1 ({len(history)})", expanded=bool(history)):
            if not history:
                st.caption("Переходы появятся после опросов монитора.")
            else:
                st.dataframe(
                    [
                        {
                            "Время": item.get("at", ""),
                            "Было": status_label(item.get("previous")),
                            "Стало": status_label(item.get("status")),
                            "Журнал": item.get("journal_seq"),
                            "Примечание": item.get("note") or item.get("error") or "",
                        }
                        for item in history
                    ],
                    hide_index=True,
                    use_container_width=True,
                )

        observation = next(
            (item for item in monitor.observed(session) if item.task_id == task_id), None
        )
        if observation is None:
            st.info("Задача не поставлена на наблюдение: команды FSM-1 доступны после наблюдения.")
            if st.button("⏱️ Поставить на наблюдение", key=f"tasks_card_watch_{task_id}"):
                monitor.register(
                    session,
                    task_id=task_id,
                    task_type=str(card.type),
                    name=str(card.name),
                )
                state.store_session(session)
                set_flash("success", f"Задача {task_id} поставлена на наблюдение.")
                st.rerun()
        else:
            _render_task_actions(
                session=session,
                monitor=monitor,
                task_id=task_id,
                observation=observation,
                scope="card",
            )

    if session is not None:
        _render_scenarios(session=session, monitor=monitor, task_id=task_id)


def _render_scenarios(*, session: TestSession, monitor: TaskMonitor, task_id: str) -> None:
    """Прогон сценариев проверок группы `TC-TASK` из карточки задачи."""
    st.divider()
    st.markdown("**Проверки чек-листа по этой задаче (FR-T4)**")
    options = {f"{spec.check_id} · {spec.title}": spec for spec in catalog.by_group("TC-TASK")}
    chosen = st.selectbox("Проверка", list(options), key=f"tasks_scenario_{task_id}")
    spec = options[chosen]
    action = "pause"
    if spec.check_id == "TC-TASK-06":
        action = st.selectbox(
            "Команда для проверки идемпотентности",
            list(TASK_COMMANDS),
            key=f"tasks_scenario_action6_{task_id}",
        )
    else:
        st.caption(
            f"Сценарий: `{spec.automation or 'ручная проверка'}` · шагов: {len(spec.steps)} · "
            f"ожидание: {spec.expected}"
        )
    if st.button("▶ Выполнить сценарий", key=f"tasks_scenario_run_{task_id}", type="primary"):
        _run_scenario(
            session=session,
            monitor=monitor,
            check_id=spec.check_id,
            task_id=task_id,
            extra={"action": action},
        )

    if session.checks:
        st.caption(
            "Зафиксированные результаты сессии: "
            + ", ".join(
                f"{item.get('check_id')} — {item.get('status')}"
                for item in sorted(session.checks, key=lambda item: str(item.get("check_id")))
            )
        )


def _run_scenario(
    *,
    session: TestSession,
    monitor: TaskMonitor,
    check_id: str,
    task_id: str,
    extra: dict[str, Any] | None = None,
    automation: str = "",
) -> None:
    """Прогоняет сценарий проверки через общий помощник (`ui/common/check_run`)."""
    check_run.run_check(
        session=session,
        monitor=monitor,
        check_id=check_id,
        params={"task_id": task_id, **(extra or {})},
        automation=automation,
    )


def _render_diagnostics(*, session: TestSession | None, monitor: TaskMonitor) -> None:
    """Вкладка «Диагностика»: запуск `celery-test` с подтверждением (TC-TASK-01)."""
    st.caption(
        "`POST /api/tasks/test?duration=N` — тестовая задача `celery-test`: дешёвый способ "
        "проверить очередь, FSM-1 и команды управления без расхода ресурсов обучения (UC-26)."
    )
    st.warning(
        "🟠 Операция изменяет состояние стенда: она создаёт задачу на общем сервере. "
        "Выполняется только после подтверждения оператора (NFR-T4)."
    )
    duration = st.number_input(
        "Длительность, с",
        min_value=MIN_DURATION,
        max_value=MAX_DURATION,
        value=DEFAULT_DURATION,
        step=1,
        key=KEY_DURATION,
    )
    confirmed = st.checkbox(
        "Подтверждаю запуск диагностической задачи на испытуемом стенде", key=KEY_DIAG_CONFIRM
    )
    if st.button(
        "▶ Запустить celery-test",
        key="tasks_diag_run",
        type="primary",
        disabled=not confirmed,
    ):
        _run_test_task(session=session, monitor=monitor, duration=int(duration))

    last = st.session_state.get(KEY_DIAG_RESULT)
    if isinstance(last, dict):
        st.success(
            f"Последний запуск: `POST /api/tasks/test?duration={last.get('duration')}` → "
            f"HTTP {last.get('status')} · задача {last.get('task_id') or 'не определена'} · "
            f"запись журнала #{last.get('journal_seq')}"
        )
        with st.expander("Тело ответа сервера"):
            st.json(last.get("body"))


def _run_test_task(*, session: TestSession | None, monitor: TaskMonitor, duration: int) -> None:
    """Запускает диагностическую задачу и ставит её на наблюдение (TC-TASK-01)."""
    runtime = state.get_runtime()
    if runtime is None:
        set_flash("warning", "Адрес испытуемого сервера не задан.")
        st.rerun()
        return

    with checks_engine.check_label(DIAG_LABEL):
        try:
            payload = runtime.apis.tasks.run_test_task(duration)
        except ClientError as exc:
            set_flash("warning", f"Задача не запущена: {exc}")
            st.rerun()
            return

    journal_seq = monitor.last_seq()
    task_id = ""
    if session is not None:
        record = monitor.register_from_response(
            session,
            payload,
            check_id=DIAG_LABEL,
            journal_seq=journal_seq,
            note=f"диагностическая задача celery-test (duration={duration})",
        )
        task_id = str((record or {}).get("task_id") or "")
        state.store_session(session)

    st.session_state[KEY_DIAG_RESULT] = {
        "duration": duration,
        "status": 200,
        "task_id": task_id,
        "journal_seq": journal_seq,
        "body": payload,
    }
    log_event(
        "task_test_started",
        f"Диагностическая задача celery-test (duration={duration})",
        module="tasks",
        check_id=DIAG_LABEL,
        payload={"duration": duration, "task_id": task_id, "journal_seq": journal_seq},
    )
    set_flash(
        "success" if task_id else "warning",
        f"Задача celery-test длительностью {duration} с создана"
        + (
            f" и поставлена на наблюдение: {task_id}"
            if task_id
            else " (в ответе нет task_id — проверьте список задач)"
        ),
    )
    st.rerun()


def _render_external(*, session: TestSession | None, monitor: TaskMonitor) -> None:
    """Вкладка «Внешняя задача»: наблюдение за задачей вне пульта (TC-TASK-08, BR-R5)."""
    st.caption(
        "Задача, запущенная вне пульта (основной UI, `curl`, планировщик), наблюдаётся по "
        "`task_id`: монитор получает её статусы, а задача помечается как **внешняя**."
    )
    if session is None:
        st.info("Для наблюдения за внешней задачей выберите сессию испытаний.")
        return

    task_id = st.text_input(
        "task_id внешней задачи (UUID)",
        key=KEY_EXTERNAL,
        placeholder="9f0a5b7e-0000-4000-8000-000000000001",
    ).strip()
    col_add, col_card = st.columns(2)
    if col_add.button("⏱️ Поставить на наблюдение", key="tasks_ext_add", type="primary"):
        if not task_id:
            set_flash("warning", "Введите task_id внешней задачи.")
        else:
            monitor.register_external(session, task_id, check_id=EXTERNAL_LABEL)
            poll = monitor.poll_once(session, task_id)
            state.store_session(session)
            set_flash(
                "success" if poll.ok else "warning",
                f"Внешняя задача {task_id}: {status_label(poll.status)}"
                if poll.ok
                else f"Внешняя задача {task_id}: {poll.error}",
            )
        st.rerun()
    if col_card.button("🔎 Открыть карточку $задачи", key="tasks_ext_card"):
        if task_id:
            st.session_state[KEY_CARD_TASK] = task_id
            set_flash(
                "info",
                f"Карточка внешней $задачи открывается на вкладке «{glossary.term('task_card')}».",
            )
        st.rerun()

    st.divider()
    external = [item for item in monitor.observed(session) if item.origin == ORIGIN_EXTERNAL]
    if not external:
        st.info("Внешних задач в наблюдении пока нет.")
        return
    st.dataframe(
        [
            {
                "Задача": item.task_id,
                "Тип": item.task_type or "—",
                "Статус": f"{item.status_icon} {item.status_label}",
                "Опросов": item.polls,
                "Прошло": _duration_text(item.first_seen),
                "Проверка": item.check_id or "—",
            }
            for item in external
        ],
        hide_index=True,
        use_container_width=True,
    )
    confirm_id = task_id or external[-1].task_id
    if st.button("✅ Подтвердить наблюдение внешней задачи (TC-TASK-08)", key="tasks_ext_confirm"):
        _run_scenario(
            session=session,
            monitor=monitor,
            check_id=EXTERNAL_LABEL,
            task_id=confirm_id,
            automation="tasks.external_observation",
        )

        del monitor
