"""`SCR-301` Прогон (единое рабочее место) — Испытания (Ф1–Ф2).

Макет экрана — `docs/16`, раздел «SCR-301. Прогон»; требования ТЗ: `FR-P-13…FR-P-23`,
`FR-P-27`, `FR-P-29`, `FR-P-41`, `IR-P-3`, `IR-P-4`, `IR-P-13`, `IR-P-20`.

Экран исполняет программу: показывает очередь утверждённой ревизии с маркерами
«следующая/выполняется/последняя» (`IR-P-3`), даёт **одну** команду запуска
«▶ Следующая: `TC-…`» (`IR-P-4`) и рядом — ленту обмена и вердикт, без переходов.

Чего экран **не делает** (`IR-P-18`): не редактирует состав программы (для этого ссылка на
`SCR-102`) и не хранит второй статус проверки — статус читается из `results.py` (`DR-P-5`).

Разделение работы с состоянием: сессия, программа и очередь читаются с диска на каждой
перерисовке, а после прогона сохраняются **тем же объектом** сессии, в который движок записал
результат (`state.store_session`) — иначе результат прогона остался бы только в памяти.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import streamlit as st

from acceptance import endpoints as ep
from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance import runner as runner_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme, ProgrammeItem
from acceptance.queue import MODE_CALL, MODE_LABELS, Queue
from acceptance.session import TestSession
from acceptance.test_payloads import payload_previews, preview_lines
from acceptance.ui import state
from acceptance.ui.components import flash, journal, layout, run_card, status
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr301_run"

#: Ключ состояния сессии, где ждёт итог последнего шага прогона (панель «Вердикт»).
KEY_OUTCOME = "pult_last_outcome"

#: Ключ состояния сессии: идёт ли авто-прогон (`A3`).
KEY_AUTO = "pult_auto_run"

#: Экран карточки проверки (переходы «открыть пункт»).
CHECK_SCREEN = "scr302_check"

#: Экран протокола проверок (переход «к протоколу»).
PROTOCOL_SCREEN = "scr401_protocol"

#: Сколько пунктов очереди показывать в таблице (остальные — счётчиком).
QUEUE_TABLE_LIMIT = 200

#: Варианты фильтра ленты: что именно показывать в обмене.
FEED_ALL = "вся лента"
FEED_LAST = "последняя проверка"
FEED_NEXT = "следующая проверка"

CONTENT = ScreenContent(
    purpose="Единое рабочее место: очередь, одна команда «Следующая», лента обмена, вердикт.",
    blocks=(
        "Очередь программы (только чтение) с маркерами «следующая/выполняется/последняя»",
        "Одна команда запуска «▶ Следующая: `TC-…`» (`IR-P-4`)",
        "Режимы: «сценарием»/«по вызовам», «Пачка tech», «Авто с паузой», «Стоп»",
        "Карточка запуска для live/heavy и предпросмотр payload (`FR-P-19`, `FR-P-20`)",
        "Лента обмена: запрос и ответ по раздельности, curl, повтор, замечание",
        "Вердикт, статус и диапазон журнала по выполненной проверке",
    ),
    states=(
        "Нет сессии: «Выберите сессию (SCR-203), иначе результаты не попадут в протокол»",
        "Программа не утверждена: прогон по черновику с пометкой в протоколе",
        "Нет стенда: запуск недоступен, показана причина",
        "Очередь пройдена: доступны протокол и отчёт",
    ),
    transitions=(
        ("SCR-102", "Программа сессии"),
        ("SCR-302", "Карточка проверки"),
        ("SCR-401", "Протокол проверок"),
        ("SCR-402", "Журнал обмена"),
    ),
    requirements=(
        "FR-P-13…FR-P-23",
        "FR-P-27",
        "FR-P-29",
        "IR-P-3",
        "IR-P-4",
        "IR-P-13",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def queue_table_rows(
    queue: Queue,
    programme: Programme,
    results: Mapping[str, Any] | None = None,
    *,
    limit: int = QUEUE_TABLE_LIMIT,
) -> list[dict[str, Any]]:
    """Строки очереди для таблицы: маркер, состояние, проверка и её результат.

    Показываются первые `limit` пунктов: остальные видны счётчиком в KPI — таблица
    на сотни строк нечитаема, а очередь растёт вместе с программой.
    """
    rows: list[dict[str, Any]] = []
    for row in queue.rows(results)[: max(0, int(limit))]:
        item = programme.find(str(row["check_id"]))
        rows.append(
            {
                "order": row["order"],
                "marker": status.marker_text(str(row["marker"])),
                "state": f"{row['state_icon']} {status.queue_state_label(str(row['state']))}",
                "check_id": row["check_id"],
                "title": item.title if item is not None else "",
                "check_class": status.class_label(item.check_class) if item is not None else "",
                "result": (
                    status.result_text(row) if row["status"] != results_api.STATUS_NOT_RUN else ""
                ),
                "journal": str(row["journal_range"]),
                "reason": str(row["reason"]),
            }
        )
    return rows


def progress_caption(queue: Queue, results: Mapping[str, Any] | None = None) -> str:
    """KPI прогона: сколько пройдено, чем закончилось и сколько осталось."""
    summary = queue.summary(results)
    done = int(summary["done"]) + int(summary["off"])
    return layout.join_parts(
        f"выполнено {layout.percent(done, int(summary['total']))}",
        f"успех {summary['passed']}",
        f"отказ {summary['failed']}",
        f"блокировано {summary['blocked']}",
        f"снято {summary['off']}",
        f"осталось {summary['pending']}",
    )


def next_note(queue: Queue, programme: Programme) -> str:
    """Что запустит «Следующая» и что для этого нужно (`IR-P-4`, `FR-P-19`)."""
    following = queue.next_item()
    if following is None:
        return "Очередь пройдена: осталось оформить протокол и отчёт"
    item = programme.find(following.check_id)
    if item is None:
        return f"Следующая: {following.check_id} — пункта нет в программе: прогон остановится"
    need, note = runner_api.needs_confirmation(item)
    manual, manual_note = runner_api.requires_operator(item)
    return layout.join_parts(
        f"Следующая: {item.check_id} ({item.check_class}) «{item.title}»",
        note if need else "",
        manual_note if manual else "",
    )


def feed_label(choice: str, queue: Queue) -> str:
    """Метка проверки для ленты обмена: пусто — показывать всю ленту."""
    if choice == FEED_LAST:
        return str(queue.last_check_id or "")
    if choice == FEED_NEXT:
        following = queue.next_item()
        return "" if following is None else following.check_id
    return ""


def mode_index(queue: Queue) -> int:
    """Индекс текущего режима исполнения в списке `queue.MODE_LABELS`."""
    modes = list(MODE_LABELS)
    return modes.index(queue.mode) if queue.mode in modes else 0


def payload_lines(
    session: TestSession | None,
    check_id: str,
    params: Mapping[str, Any],
) -> list[str]:
    """«Что будет отправлено»: строки предпросмотра `__TEST__`-данных (`FR-P-20`)."""
    spec = catalog.find(check_id)
    if spec is None:
        return [f"Проверка `{check_id}` отсутствует в каталоге: предпросмотр невозможен."]
    previews = payload_previews(spec, session, params=dict(params))
    return preview_lines(previews)


def call_step_note(check_id: str, call_index: int) -> str:
    """Подпись следующего вызова в режиме «по вызовам»: «вызов 2 из 5: GET …»."""
    spec = catalog.find(check_id)
    if spec is None:
        return ""
    steps = runner_api.call_steps(spec)
    if not steps:
        return "у проверки нет операций для пошагового прогона: выполните её сценарием"
    index = min(max(int(call_index), 0), len(steps) - 1)
    step = steps[index]
    return layout.join_parts(f"вызов {index + 1} из {len(steps)}: {step.operation}", step.expected)


def outcome_lines(outcome: Mapping[str, Any] | None) -> list[str]:
    """Строки вердикта последнего шага: что сделано и с каким итогом."""
    data = dict(outcome or {})
    if not data:
        return ["Прогон ещё не выполнялся."]
    return [
        layout.join_parts(
            str(data.get("check_id") or ""),
            str(data.get("status") or ""),
            str(data.get("level") or ""),
        ),
        str(data.get("verdict") or data.get("detail") or ""),
        layout.join_parts(
            f"журнал: {data.get('journal_range') or '—'}",
            f"повторов: {data.get('repeats') or 0}",
            f"записей в шаге: {data.get('steps') or 0}",
        ),
    ]


def summary_feedback(outcomes: Sequence[Mapping[str, Any]]) -> str:
    """Сообщение после шага прогона: одна проверка или итог «пачки tech»."""
    if not outcomes:
        return "Шаг прогона не выполнен"
    last = dict(outcomes[-1])
    single = layout.join_parts(
        str(last.get("check_id") or ""),
        str(last.get("status") or ""),
        str(last.get("verdict") or last.get("detail") or ""),
    )
    if len(outcomes) == 1:
        return single
    passed = sum(1 for item in outcomes if str(item.get("status")) == str(CheckStatus.PASSED))
    return layout.join_parts(
        f"пачка tech: шагов {len(outcomes)}",
        f"успех {passed}",
        f"последний: {single}",
    )


# ---------------------------------------------------------------------------
# Экран: очередь, управление, лента обмена и панель вердикта
# ---------------------------------------------------------------------------
def render() -> None:
    """Рисует экран прогона: очередь, одна команда запуска, лента и вердикт."""
    session = state.current_session()
    if session is None:
        layout.render_header(KEY, "сессия не выбрана")
        flash.render()
        layout.render_no_session(what="Прогон пишет результаты в сессию (`DR-P-1`)")
        return

    programme = state.current_programme()
    queue = state.current_queue()
    results = state.results_state(queue.check_ids)

    subtitle = layout.join_parts(
        f"ревизия {queue.revision}" if queue.size else "очередь не собрана",
        queue.mode_label,
        "прогон по черновику программы" if queue.draft else "по утверждённой программе",
    )
    layout.render_header(KEY, subtitle)
    flash.render()

    if queue.is_empty:
        st.warning(runner_api.programme_notice(queue))
        if st.button(
            "→ Собрать программу и очередь (SCR-102)", key=f"{KEY}_goto_programme", type="primary"
        ):
            state.go_to("scr102_programme")
        return

    workspace, context = layout.zones()
    with workspace:
        _render_queue(session, programme, queue, results)
        _render_controls(session, programme, queue, results)
        _render_feed(queue)
    with context:
        _render_context(programme, queue)


def _render_queue(
    session: TestSession,
    programme: Programme,
    queue: Queue,
    results: Mapping[str, Any],
) -> None:
    """Очередь утверждённой программы: маркеры, состав и снятие пункта с причиной."""
    st.subheader(f"Очередь программы ({queue.size})")
    st.caption(runner_api.programme_notice(queue))
    st.caption(progress_caption(queue, results))
    st.caption(
        layout.join_parts(
            next_note(queue, programme),
            f"последняя: {queue.last_check_id}" if queue.last_check_id else "",
        )
    )
    if not queue.is_current(programme):
        st.error(
            f"Очередь собрана по ревизии {queue.revision}, а программа — ревизия "
            f"{programme.revision}: пересоберите очередь на `SCR-102`."
        )

    layout.rows_table(
        queue_table_rows(queue, programme, results),
        key=f"{KEY}_queue",
        columns={
            "order": "№",
            "marker": "",
            "state": "Состояние",
            "check_id": "Проверка",
            "title": "Название",
            "check_class": "Класс",
            "result": "Результат",
            "journal": "Журнал",
            "reason": "Причина снятия",
        },
        height=320,
    )
    if queue.size > QUEUE_TABLE_LIMIT:
        st.caption(f"показаны первые {QUEUE_TABLE_LIMIT} пунктов из {queue.size}")

    with st.expander("Снять пункт с причиной (`FR-P-15`)", expanded=False):
        st.caption(
            "Снятие — результат прогона, а не правка программы: состав очереди не меняется, "
            "а причина попадает в протокол."
        )
        chosen = st.selectbox(
            "Пункт",
            queue.check_ids,
            key=f"{KEY}_off_pick",
            format_func=lambda check_id: layout.join_parts(
                check_id, _title_of(programme, check_id)
            ),
        )
        reason = st.text_input("Причина снятия", key=f"{KEY}_off_reason")
        if st.button("Снять пункт", key=f"{KEY}_off_apply"):
            _take_off(session, queue, chosen, reason)

    if st.button("✎ Изменить программу (SCR-102)", key=f"{KEY}_edit_programme"):
        state.go_to("scr102_programme")


def _title_of(programme: Programme, check_id: str) -> str:
    """Название проверки программы (пусто, если пункта в программе нет)."""
    item = programme.find(check_id)
    return "" if item is None else item.title


def _take_off(session: TestSession, queue: Queue, check_id: str, reason: str) -> None:
    """Снимает пункт с причиной: результат пишется в сессию (`FR-P-15`)."""
    try:
        queue.take_off(
            check_id,
            reason,
            session=session,
            author=session.info.operator_fio,
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    _save_step(session, queue, event="queue_item_taken_off", message=f"Пункт {check_id} снят")
    flash.warning(f"Пункт {check_id} снят с причиной: {reason}")
    st.rerun()


def _render_controls(
    session: TestSession,
    programme: Programme,
    queue: Queue,
    results: Mapping[str, Any],
) -> None:
    """Управление прогоном: режим, одна команда запуска, «Пачка tech» и авто (`IR-P-4`)."""
    st.divider()
    st.subheader("Управление")
    modes = list(MODE_LABELS)
    choice = st.radio(
        "Режим исполнения",
        modes,
        index=mode_index(queue),
        format_func=lambda mode: MODE_LABELS.get(mode, mode),
        horizontal=True,
        key=f"{KEY}_mode",
    )
    if choice != queue.mode:
        queue.mode = choice
        _save_step(session, queue, event="queue_mode", message=f"Режим прогона: {choice}")
        flash.info(f"Режим прогона: {choice}")
        st.rerun()

    runtime = state.get_runtime()
    ready, reason = runner_api.can_run(queue, programme)
    if runtime is None:
        st.error("Адрес стенда не задан: прогон невозможен (настройки — `SCR-501`).")
    elif not ready:
        st.warning(reason)

    following = queue.next_item()
    item = programme.find(following.check_id) if following is not None else None
    outcome = st.session_state.get(KEY_OUTCOME) or {}
    card_reason = (
        str(outcome.get("stop_reason") or "") if outcome.get("confirmation_required") else ""
    )
    params = (
        _render_call_params(item, queue) if (item is not None and queue.mode == MODE_CALL) else {}
    )
    evidence = _render_run_card(session, item, card_reason)

    if st.button(
        runner_api.next_label(queue),
        key=f"{KEY}_next",
        type="primary",
        width="stretch",
        disabled=not ready or runtime is None,
        help="Единственная команда запуска: отправляет ровно один следующий пункт (IR-P-4)",
    ):
        _execute(session, programme, queue, action="next", params=params, run_evidence=evidence)

    batch_ids = runner_api.batch_tech_ids(queue, programme)
    if st.button(
        f"▶▶ Пачка tech ({len(batch_ids)})",
        key=f"{KEY}_batch",
        width="stretch",
        disabled=not ready or runtime is None or not batch_ids,
        help="Подряд безопасные проверки класса tech — без карточек запуска (решение A4)",
    ):
        _execute(session, programme, queue, action="batch", params=params, run_evidence=evidence)

    _render_auto(session, programme, queue, params, evidence)
    _render_payload(session, queue, params)


def _render_run_card(
    session: TestSession,
    item: ProgrammeItem | None,
    reason: str,
) -> dict[str, Any] | None:
    """Карточка запуска: обязательна для изменяющих и ресурсоёмких проверок (`FR-P-19`)."""
    if item is None:
        return None
    need, _note = runner_api.needs_confirmation(item)
    if not need:
        return None
    with st.expander(f"Карточка запуска: {item.check_id} (обязательна)", expanded=True):
        return run_card.render(
            item,
            key=f"{KEY}_card_{item.check_id}",
            session_id=session.session_id,
            author=session.info.operator_fio,
            reason=reason,
        )


def _render_auto(
    session: TestSession,
    programme: Programme,
    queue: Queue,
    params: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
) -> None:
    """Авто-прогон с паузой: шаг за шагом, с остановом на карточке запуска (`A3`)."""
    pause = st.number_input(
        "Пауза авто-прогона, с",
        min_value=queue_api.MIN_PAUSE_SECONDS,
        max_value=queue_api.MAX_PAUSE_SECONDS,
        value=float(queue.pause_seconds),
        step=0.5,
        key=f"{KEY}_pause",
    )
    if abs(float(pause) - float(queue.pause_seconds)) > 1e-9:
        queue.pause_seconds = queue_api.clamp_pause(pause)
        _save_step(
            session, queue, event="queue_pause", message=f"Пауза авто: {queue.pause_seconds} с"
        )

    auto_on = bool(st.session_state.get(KEY_AUTO))
    start, stop = st.columns(2)
    if start.button("⏯ Авто", key=f"{KEY}_auto", width="stretch", disabled=auto_on):
        st.session_state[KEY_AUTO] = True
        st.rerun()
    if stop.button("⏸ Стоп", key=f"{KEY}_stop", width="stretch", disabled=not auto_on):
        st.session_state[KEY_AUTO] = False
        flash.info("Авто-прогон остановлен оператором")
        st.rerun()
    if not auto_on:
        return

    reason = runner_api.auto_stop_reason(queue, programme)
    if reason:
        st.session_state[KEY_AUTO] = False
        flash.info(f"Авто-прогон остановлен: {reason}")
        st.rerun()
    time.sleep(float(queue.pause_seconds))
    _execute(session, programme, queue, action="auto", params=params, run_evidence=evidence)


def _execute(
    session: TestSession,
    programme: Programme,
    queue: Queue,
    *,
    action: str,
    params: Mapping[str, Any] | None = None,
    run_evidence: Mapping[str, Any] | None = None,
) -> None:
    """Выполняет шаг прогона и сохраняет итог в сессию (`FR-P-16`, `FR-P-19`)."""
    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес стенда не задан: прогон невозможен (настройки — `SCR-501`).")
        return
    ready, reason = runner_api.can_run(queue, programme)
    if not ready:
        st.error(reason)
        return

    common: dict[str, Any] = {
        "monitor": state.task_monitor(),
        "client": state.console_client(),
        "params": dict(params or {}),
        "run_evidence": dict(run_evidence) if run_evidence else None,
        "author": session.info.operator_fio,
    }
    if action == "batch":
        outcomes = runner_api.run_batch_tech(queue, programme, session, runtime, **common)
    elif action == "auto":
        step = runner_api.auto_step(queue, programme, session, runtime, **common)
        outcomes = [] if step is None else [step]
    else:
        outcomes = [runner_api.execute_next(queue, programme, session, runtime, **common)]
    _record_outcomes(session, queue, outcomes)


def _record_outcomes(
    session: TestSession,
    queue: Queue,
    outcomes: Sequence[runner_api.RunOutcome],
) -> None:
    """Сохраняет шаг прогона и сообщает оператору, чем он закончился."""
    summaries = [outcome.summary() for outcome in outcomes]
    last = outcomes[-1] if outcomes else None
    if summaries:
        st.session_state[KEY_OUTCOME] = {
            **summaries[-1],
            "confirmation_required": bool(last.confirmation_required) if last else False,
            "operator_required": bool(last.operator_required) if last else False,
            "stopped": bool(last.stopped) if last else False,
        }
    _save_step(
        session,
        queue,
        event="queue_step",
        message=f"Шаг прогона: {summaries[-1]['check_id'] if summaries else '—'}",
    )
    text = summary_feedback(summaries)
    if last is not None and last.confirmation_required:
        flash.warning(f"{text} — заполните карточку запуска")
    elif last is not None and last.operator_required:
        flash.warning(f"{text} — проверка ручная: отметьте её в карточке проверки (`SCR-302`)")
    elif last is not None and last.ok:
        flash.success(text)
    else:
        flash.warning(text)
    st.rerun()


def _save_step(
    session: TestSession,
    queue: Queue,
    *,
    event: str = "",
    message: str = "",
) -> None:
    """Пишет очередь в **тот же** объект сессии, в который движок записал результат.

    Отдельный путь сохранения нужен потому, что `state.store_queue` перечитывает сессию
    с диска: результат прогона, записанный движком в память, при этом потерялся бы.
    """
    queue_api.save_queue(session, queue, event=event, message=message)
    state.store_session(session)


def _render_call_params(item: ProgrammeItem | None, queue: Queue) -> dict[str, Any]:
    """Параметры следующего вызова в режиме «по вызовам»: путь, query и тело."""
    if item is None:
        return {}
    spec = catalog.find(item.check_id)
    if spec is None:
        return {}
    steps = runner_api.call_steps(spec)
    if not steps:
        st.caption("У проверки нет операций для пошагового прогона: выполните её сценарием.")
        return {}
    found = queue.find(item.check_id)
    index = min(max(found.call_index if found is not None else 0, 0), len(steps) - 1)
    step = steps[index]
    st.caption(call_step_note(item.check_id, index))

    values: dict[str, Any] = {}
    names = [*runner_api.path_params(step), *runner_api.query_params(step)]
    if names:
        cells = st.columns(min(3, len(names)))
        for position, name in enumerate(names):
            values[name] = cells[position % len(cells)].text_input(
                name,
                value=str(step.params.get(name, "")),
                key=f"{KEY}_param_{item.check_id}_{name}",
                help="path-параметры подставляются в путь, query — в строку запроса",
            )
    if step.body_kind == ep.BODY_JSON:
        values["body"] = st.text_area(
            "тело запроса (JSON)",
            value=step.body_sample,
            key=f"{KEY}_body_{item.check_id}",
            height=100,
        )
    return values


def _render_payload(
    session: TestSession,
    queue: Queue,
    params: Mapping[str, Any],
) -> None:
    """Предпросмотр payload: что именно уйдёт на стенд (`FR-P-20`)."""
    following = queue.next_item()
    if following is None:
        return
    with st.expander(f"Что будет отправлено: {following.check_id}", expanded=False):
        for line in payload_lines(session, following.check_id, params):
            st.markdown(line)
        if queue.mode == MODE_CALL:
            st.caption(call_step_note(following.check_id, following.call_index))


def _render_feed(queue: Queue) -> None:
    """Лента обмена: запрос и ответ раздельно, `curl` и фильтры (`DR-P-4`, `FR-T6`)."""
    st.divider()
    st.subheader("Лента обмена")
    choice = st.radio(
        "Что показывать",
        [FEED_ALL, FEED_LAST, FEED_NEXT],
        horizontal=True,
        key=f"{KEY}_feed_choice",
    )
    label = feed_label(choice, queue)
    if label:
        st.caption(f"фильтр по метке проверки: `{label}`")
    runtime = state.get_runtime()
    journal.render_feed(
        state.journal_records(),
        key=f"{KEY}_feed",
        label=label,
        base_url=runtime.settings.base_url if runtime is not None else "",
    )


def _render_context(programme: Programme, queue: Queue) -> None:
    """Панель контекста: вердикт последнего шага, след прогона и переходы."""
    st.subheader("Вердикт и след")
    st.caption(next_note(queue, programme))
    for line in outcome_lines(st.session_state.get(KEY_OUTCOME)):
        st.markdown(line)
    if queue.stop_reason:
        st.warning(queue.stop_reason)
    last = queue.last()
    if last is not None:
        st.caption(
            layout.join_parts(
                f"последняя: {last.check_id}",
                last.journal_range or "без журнала",
                last.note,
            )
        )
    st.divider()
    if queue.check_ids:
        chosen = st.selectbox("Открыть карточку проверки", queue.check_ids, key=f"{KEY}_open_pick")
        if st.button("→ Карточка проверки (SCR-302)", key=f"{KEY}_open", width="stretch"):
            st.session_state[state.KEY_CHECK] = chosen
            state.go_to(CHECK_SCREEN)
    if st.button("→ Протокол проверок (SCR-401)", key=f"{KEY}_protocol", width="stretch"):
        state.go_to(PROTOCOL_SCREEN)
    if st.button("→ Журнал обмена (SCR-402)", key=f"{KEY}_journal", width="stretch"):
        state.go_to("scr402_journal")
