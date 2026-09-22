"""Панель «Программа испытаний»: план вызовов и контроль исполнения (пожелания п. 2–3).

Панель ставится рядом с лентой монитора обмена (экран «Монитор обмена») и отвечает
на вопросы комиссии:

    * **что будет вызвано следующим** — список запланированных вызовов с галочками
      (снять галочку можно и точечно, и «по выборке» через фильтры);
    * **чем закончился вызов** — статус пункта и вердикт «соответствует ожиданию»;
    * **как выполнять** — «▶ Выполнить следующую» (по одной команде оператора),
      «⏯ Авто с паузой» (шаг за шагом с заданным интервалом) и «⏸ Стоп».

Авто-прогон реализован фрагментом Streamlit с тиком в секунду: он не блокирует
интерфейс, работает в том же потоке скрипта (поэтому безопасно пишет в сессию) и
останавливается сам, когда нужен оператор — например, для проверки класса `live`,
которая запускается только с карточкой запуска (NFR-T4, FR-T10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st

from acceptance import plan as plan_api
from acceptance import plan_runner, test_payloads
from acceptance.checks.registry import CheckClass
from acceptance.session import TestSession
from acceptance.ui import state
from acceptance.ui.common.flash import set_flash

#: Ключ рабочего плана в `session_state` (план переживает перерисовки, в сессию пишется сразу).
KEY_PLAN = "pult_plan"
KEY_MODE = "plan_mode"
KEY_PAUSE = "plan_pause_seconds"
KEY_CONFIRM = "plan_bulk_confirm"
AUTO_TICK = "1s"

STATUS_OPTIONS: tuple[str, ...] = (
    plan_api.STATUS_PENDING,
    plan_api.STATUS_PASSED,
    plan_api.STATUS_FAILED,
    plan_api.STATUS_BLOCKED,
    plan_api.STATUS_SKIPPED,
)


def plan_state() -> plan_api.PlanState:
    """Рабочее состояние плана: из сессии, иначе — собранное по каталогу проверок."""
    session = state.current_session()
    session_id = session.session_id if session is not None else ""
    holder = st.session_state.get(KEY_PLAN)
    if (
        isinstance(holder, dict)
        and holder.get("session_id") == session_id
        and isinstance(holder.get("plan"), plan_api.PlanState)
    ):
        return holder["plan"]

    loaded = plan_api.load_plan(session) if session is not None else plan_api.build_plan()
    st.session_state[KEY_PLAN] = {"session_id": session_id, "plan": loaded}
    return loaded


def save(state_: plan_api.PlanState, *, history: bool = False) -> None:
    """Сохраняет план в сессию (схема v6) и в `session_state`."""
    session = state.current_session()
    st.session_state[KEY_PLAN] = {
        "session_id": session.session_id if session is not None else "",
        "plan": state_,
    }
    if session is not None:
        plan_api.save_plan(session, state_, history=history)
        state.store_session(session)


def render(session: TestSession | None, runtime: Any) -> plan_api.PlanState:
    """Отрисовывает панель программы испытаний и возвращает её состояние."""
    plan = plan_state()
    st.subheader("Программа испытаний")
    st.caption(
        "План вызовов: галочки определяют, что пойдёт на стенд; «Выполнить» отправляет "
        "следующий включённый пункт, «Авто с паузой» идёт по плану с заданным интервалом."
    )
    _render_summary(plan)
    mode = _render_mode_controls(plan)
    _render_buttons(plan, session=session, runtime=runtime, mode=mode)
    st.divider()
    visible = _render_filters(plan)
    st.divider()
    _render_tree(plan, visible=visible)
    _render_item_editor(plan, session=session, runtime=runtime)
    return plan


def _render_summary(plan: plan_api.PlanState) -> None:
    """KPI программы испытаний: включено, выполнено, успех/отказ, режим."""
    summary = plan.summary()
    col_enabled, col_done, col_passed, col_failed = st.columns(4)
    col_enabled.metric("Включено", f"{summary['enabled']} / {summary['total']}")
    col_done.metric("Выполнено", summary["done"])
    col_passed.metric("Успех", summary["passed"])
    col_failed.metric("Отказ", summary["failed"], delta=f"пропущено {summary['skipped']}")
    next_item = plan_runner.next_item(plan)
    if next_item is None:
        st.caption("Следующий пункт: — (включённые пункты закончились).")
    else:
        st.caption(
            f"Следующий пункт: `{next_item.item_id}` · {next_item.title}"
            + (f" · {next_item.operation}" if next_item.operation else "")
        )
    if plan.stop_reason:
        st.warning(f"Авто-прогон остановлен: {plan.stop_reason}")


def _render_mode_controls(plan: plan_api.PlanState) -> str:
    """Режим исполнения и пауза между вызовами."""
    labels = list(plan_api.MODE_LABELS.values())
    keys = list(plan_api.MODE_LABELS)
    current = keys.index(plan.mode) if plan.mode in keys else 0
    col_mode, col_pause = st.columns([3, 2])
    label = col_mode.radio(
        "Что выполняет кнопка «Выполнить»",
        labels,
        index=current,
        key=KEY_MODE,
        help="Проверка целиком выполняет её сценарий; «по вызовам» отправляет ровно один "
        "запрос по шаблону пункта — так удобно вести испытания «по одной команде».",
    )
    mode = keys[labels.index(label)]
    pause = col_pause.number_input(
        "Пауза между вызовами, с",
        min_value=plan_api.MIN_PAUSE_SECONDS,
        max_value=plan_api.MAX_PAUSE_SECONDS,
        value=float(plan.pause_seconds),
        step=0.5,
        key=KEY_PAUSE,
        help="Интервал авто-прогона: между двумя пунктами плана выдерживается эта пауза.",
    )
    changed = mode != plan.mode or float(pause) != float(plan.pause_seconds)
    plan.mode = mode
    plan.pause_seconds = plan_api.clamp_pause(pause)
    if changed:
        save(plan)
    return mode


def _render_buttons(
    plan: plan_api.PlanState,
    *,
    session: TestSession | None,
    runtime: Any,
    mode: str,
) -> None:
    """Кнопки контроля исполнения: по одной, авто с паузой, стоп, сброс."""
    confirmed = st.checkbox(
        "Подтверждаю изменяющие и ресурсоёмкие вызовы плана (§ NFR-T4)",
        value=bool(st.session_state.get(KEY_CONFIRM, False)),
        key=KEY_CONFIRM,
        help="Проверки классов `live`/`heavy` всё равно требуют карточку запуска на экране "
        "«Чек-лист»: план останавливается и подсказывает, что нужно подтвердить.",
    )
    col_next, col_auto, col_stop, col_reset = st.columns(4)
    if col_next.button("▶ Выполнить следующую", type="primary", key="plan_next"):
        _run_next(plan, session=session, runtime=runtime, bulk_confirmed=confirmed)
    if col_auto.button("⏯ Авто с паузой", key="plan_auto"):
        item = plan_runner.next_item(plan)
        if item is None:
            set_flash("info", "Включённых пунктов не осталось: выполнять нечего.")
        else:
            plan.running = True
            plan.stop_reason = ""
            save(plan, history=True)
        st.rerun()
    if col_stop.button("⏸ Стоп", key="plan_stop"):
        plan.running = False
        plan.stop_reason = "остановлено оператором"
        save(plan)
        st.rerun()
    if col_reset.button("↺ Сброс", key="plan_reset"):
        plan.reset()
        save(plan)
        set_flash("info", "Программа испытаний сброшена: галочки сохранены, результаты очищены.")
        st.rerun()
    st.caption(
        f"Режим: {plan_api.MODE_LABELS.get(mode, mode)} · пауза {plan.pause_seconds:g} с · "
        + ("авто-прогон идёт" if plan.running else "авто-прогон остановлен")
    )
    # Тикающий фрагмент регистрируется только во время авто-прогона: пока он не нужен,
    # интерфейс не перерисовывается сам (и смоук-тесты AppTest не ждут таймер).
    if plan.running:
        _auto_runner(plan, session=session, runtime=runtime, bulk_confirmed=confirmed)


def _run_next(
    plan: plan_api.PlanState,
    *,
    session: TestSession | None,
    runtime: Any,
    bulk_confirmed: bool,
    item: plan_api.PlanItem | None = None,
) -> None:
    """Выполняет следующий (или указанный) пункт плана и сообщает результат."""
    target = item or plan_runner.next_item(plan)
    if target is None:
        set_flash("info", "Включённых пунктов не осталось: выполнять нечего.")
        st.rerun()

    need, reason = plan_runner.item_needs_confirmation(target)
    if need and (target.is_check or not bulk_confirmed):
        plan.running = False
        plan.stop_reason = reason
        save(plan)
        set_flash(
            "warning",
            f"Пункт `{target.item_id}` не отправлен: {reason}. "
            + (
                "Заполните карточку запуска на экране «Чек-лист проверок»."
                if target.is_check
                else "Поставьте подтверждение в панели программы испытаний."
            ),
        )
        st.rerun()
    if session is None:
        set_flash(
            "warning",
            "Сессия испытаний не выбрана: результаты программы не попадут в отчёт.",
        )
        st.rerun()

    with st.spinner(f"Выполняется `{target.item_id}`…"):
        run = plan_runner.execute_item(
            plan,
            item=target,
            session=session,
            runtime=runtime,
            monitor=state.task_monitor(),
            client=state.console_client(),
        )
    save(plan)
    icon = "success" if run.ok else "warning"
    set_flash(
        icon,
        f"`{run.item.item_id}`: {run.item.status} — {run.verdict or run.detail or 'без вердикта'}"
        + (f" · запись журнала #{run.journal_to}" if run.journal_to else ""),
    )
    st.rerun()


@st.fragment(run_every=AUTO_TICK)
def _auto_runner(
    plan: plan_api.PlanState,
    *,
    session: TestSession | None,
    runtime: Any,
    bulk_confirmed: bool,
) -> None:
    """Авто-прогон с паузой: один пункт за тик, когда пауза выдержана.

    Тик фрагмента — 1 с, а фактический интервал задаётся `plan.pause_seconds`:
    между отправками выдерживается пауза оператора (пожелание п. 2). Прогон
    останавливается, как только нужен оператор (подтверждение, сессия).
    """
    if not plan.running:
        return
    if session is None:
        plan.running = False
        plan.stop_reason = "сессия испытаний не выбрана"
        save(plan)
        return
    if not _pause_elapsed(plan):
        return

    target = plan_runner.next_item(plan)
    if target is None:
        plan.running = False
        plan.stop_reason = "план выполнен"
        save(plan, history=True)
        st.rerun()
        return

    need, reason = plan_runner.item_needs_confirmation(target)
    if need and (target.is_check or not bulk_confirmed):
        plan.running = False
        plan.stop_reason = reason
        save(plan)
        st.rerun()
        return

    run = plan_runner.execute_item(
        plan,
        item=target,
        session=session,
        runtime=runtime,
        monitor=state.task_monitor(),
        client=state.console_client(),
    )
    if plan.running:
        save(plan, history=True)
    if run.stop:
        plan.running = False
        plan.stop_reason = f"отказ пункта `{run.item.item_id}`"
        save(plan)
    st.rerun()


def _pause_elapsed(plan: plan_api.PlanState) -> bool:
    """True, если после предыдущего пункта прошла заданная пауза."""
    if not plan.last_at:
        return True
    try:
        last = datetime.fromisoformat(plan.last_at)
    except ValueError:
        return True
    return (datetime.now() - last).total_seconds() >= plan.pause_seconds


def _render_filters(plan: plan_api.PlanState) -> list[plan_api.PlanItem]:
    """Фильтры программы испытаний и массовые действия с галочками по выборке."""
    groups = sorted({item.group for item in plan.items if item.group})
    col_group, col_class, col_status = st.columns([2, 2, 2])
    group_label = col_group.selectbox(
        "Группа", ["— все —", *groups], key="plan_filter_group", help="Группа проверок (TC-…)."
    )
    classes = col_class.multiselect(
        "Класс",
        [str(item) for item in CheckClass],
        key="plan_filter_class",
        help="`tech` — безопасные, `live` — боевые, `heavy` — ресурсоёмкие, `manual` — ручные.",
    )
    statuses = col_status.multiselect(
        "Статус",
        list(STATUS_OPTIONS),
        key="plan_filter_status",
        help="Пусто — показать пункты с любым статусом.",
    )
    col_text, col_only = st.columns([3, 2])
    text = col_text.text_input(
        "Поиск по id, названию и операции",
        key="plan_filter_text",
        placeholder="TC-DS-01, datasets, /api/loads",
    )
    only = col_only.radio(
        "Галочки",
        ("все пункты", "только включённые", "только снятые"),
        key="plan_filter_only",
        help="Снятые галочкой пункты не уходят на стенд — это видно комиссии.",
    )
    level = st.radio(
        "Уровень пунктов",
        (plan_api.LEVEL_CHECK, plan_api.LEVEL_CALL),
        horizontal=True,
        key="plan_filter_level",
        help="Проверка целиком или отдельные вызовы проверок.",
    )

    visible = [
        item
        for item in plan.items
        if (group_label.startswith("—") or item.group == group_label)
        and (not classes or item.check_class in classes)
        and (not statuses or item.status in statuses)
        and (not text.strip() or _matches_text(item, text))
        and (only != "только включённые" or item.enabled)
        and (only != "только снятые" or not item.enabled)
        and item.level == level
    ]

    col_on, col_off, col_hint = st.columns([1, 1, 3])
    if col_on.button("✔ Включить всё в выборке", key="plan_bulk_on"):
        plan.set_enabled_many([item.item_id for item in visible], True)
        for item in plan.items:
            sync_checkbox(item, item.enabled)
        save(plan)
        st.rerun()
    if col_off.button("✖ Снять всё в выборке", key="plan_bulk_off"):
        plan.set_enabled_many([item.item_id for item in visible], False)
        for item in plan.items:
            sync_checkbox(item, item.enabled)
        save(plan)
        st.rerun()
    col_hint.caption(
        f"В выборке {len(visible)} пунктов из {len(plan.items)}; снято галочек — "
        f"{plan.summary()['disabled']}."
    )
    return visible


def _matches_text(item: plan_api.PlanItem, text: str) -> bool:
    """True, если подстрока встречается в идентификаторе, названии или операции."""
    needle = text.strip().lower()
    haystack = " ".join((item.item_id, item.title, item.operation, item.group)).lower()
    return needle in haystack


def _widget_key(item: plan_api.PlanItem) -> str:
    """Ключ галочки пункта в `session_state` (по нему же — виджет в дереве)."""
    return f"plan_chk_{item.plan_key}"


def _checkbox_value(item: plan_api.PlanItem) -> bool:
    """Текущее значение галочки пункта (значение виджета важнее модели).

    Галочки живут в `session_state` и меняются и оператором, и программно (снятие
    галочки проверки снимает её вызовы, включение вызова включает проверку). Поэтому
    читать нужно значение виджета, а не поле пункта, иначе интерфейс и модель будут
    «перетягивать» друг друга (найдено смоук-тестом: бесконечная перерисовка).
    """
    return bool(st.session_state.get(_widget_key(item), item.enabled))


def sync_checkbox(item: plan_api.PlanItem, value: bool) -> None:
    """Приводит галочку пункта в `session_state` к значению модели.

    Нужна после массовых действий («включить/снять всё в выборке», сброс плана):
    виджеты уже созданы и без синхронизации сохранили бы прежние значения.
    """
    key = _widget_key(item)
    if key in st.session_state:
        st.session_state[key] = value


def _on_check_toggle(plan: plan_api.PlanState, item: plan_api.PlanItem) -> None:
    """Галочка проверки: снимает/ставит её вызовы и сохраняет план."""
    enabled = _checkbox_value(item)
    plan.set_enabled(item.item_id, enabled)
    for call in plan.calls_of(item.item_id):
        sync_checkbox(call, enabled)
    save(plan)


def _on_call_toggle(plan: plan_api.PlanState, item: plan_api.PlanItem) -> None:
    """Галочка вызова: поднимает галочку проверки-родителя и сохраняет план."""
    enabled = _checkbox_value(item)
    plan.set_enabled(item.item_id, enabled)
    parent = plan.find(item.parent_id) if item.parent_id else None
    if enabled and parent is not None:
        sync_checkbox(parent, True)
    save(plan)


def _render_tree(plan: plan_api.PlanState, *, visible: list[plan_api.PlanItem]) -> None:
    """Список запланированных пунктов с галочками и результатами.

    Проверки показываются строками, их вызовы — раскрывающимся списком внутри той же
    проверки: оператор видит и план целиком, и детализацию до отдельного вызова.
    Изменения галочек применяются колбэками (`_on_check_toggle`/`_on_call_toggle`),
    поэтому «выполнить следующую» сразу видит актуальный состав плана.
    """
    if not visible:
        st.info("В выборке нет пунктов: измените фильтры программы испытаний.")
        return

    for item in visible:
        if item.is_check:
            _check_row(plan, item)
        else:
            _call_row(plan, item)


def _check_row(plan: plan_api.PlanState, item: plan_api.PlanItem) -> None:
    """Строка проверки: галочка, статус, вердикт и её вызовы."""
    st.checkbox(
        _check_label(item),
        value=item.enabled,
        key=_widget_key(item),
        on_change=_on_check_toggle,
        args=(plan, item),
        help=(
            f"{item.title} · требования и ожидание — на экране «Чек-лист проверок». "
            "Снятая галочка исключает проверку и все её вызовы."
        ),
    )
    calls = plan.calls_of(item.item_id)
    if calls:
        with st.expander(f"Вызовы проверки ({len(calls)})", expanded=False):
            for call in calls:
                _call_row(plan, call)
    else:
        st.caption("В описании проверки эндпоинты не указаны: выполняется сценарий целиком.")


def _check_label(item: plan_api.PlanItem) -> str:
    """Подпись проверки в списке плана: статус, id, класс, название."""
    return f"{item.icon} `{item.item_id}` · {item.check_class} · {item.title}"


def _call_row(plan: plan_api.PlanState, item: plan_api.PlanItem) -> None:
    """Строка вызова: галочка, операция, ожидание и вердикт."""
    col_check, col_text = st.columns([1, 5])
    col_check.checkbox(
        "Включить",
        value=item.enabled,
        key=_widget_key(item),
        on_change=_on_call_toggle,
        args=(plan, item),
        label_visibility="collapsed",
        help="Снятая галочка исключает вызов из прогона.",
    )
    suffix = f" · 🚫 {item.verdict}" if item.verdict else ""
    col_text.markdown(
        f"{item.icon} `{item.item_id}` · **{item.operation or item.title}** · "
        f"{item.safety or '—'}{suffix}"
    )
    col_text.caption(f"Ожидание: {item.expected or '—'}" + _verdict_hint(item))


def _verdict_hint(item: plan_api.PlanItem) -> str:
    """Дополнение к ожиданию: что именно произошло в прогоне."""
    parts: list[str] = []
    if item.duration_ms is not None:
        parts.append(f"{item.duration_ms:.0f} мс")
    if item.journal_from is not None:
        parts.append(
            f"журнал #{item.journal_from}"
            if item.journal_to in (None, item.journal_from)
            else f"журнал #{item.journal_from}–#{item.journal_to}"
        )
    return (" · " + " · ".join(parts)) if parts else ""


def _render_item_editor(
    plan: plan_api.PlanState,
    *,
    session: TestSession | None,
    runtime: Any,
) -> None:
    """Правка параметров выбранного вызова и его одиночное исполнение.

    Оператор настраивает то, что уйдёт на стенд (path-параметры, query, JSON-тело,
    идентификаторы сущностей), видит команду `curl` и может выполнить ровно этот
    вызов — «по одной команде», как требует пожелание п. 2.
    """
    calls = [item for item in plan.items if item.is_call]
    if not calls:
        return
    st.divider()
    st.markdown("**Параметры вызова (`$`-сущности и query)**")
    options = {
        f"{item.icon} {item.item_id} · {item.operation or item.title}": item for item in calls
    }
    chosen = st.selectbox("Пункт для правки", list(options), key="plan_edit_pick")
    item = options[chosen]
    st.caption(f"Ожидание: {item.expected or '—'} · класс безопасности: `{item.safety or '—'}`")

    if item.probe:
        st.info(
            "Негативная проба (маршрута нет в спецификации) выполняется сценарием проверки: "
            "в режиме «по вызовам» она пропускается."
        )
        return

    values = dict(item.params)
    endpoint = plan_runner.plan_endpoint(item)
    path_params: list[str] = (
        [param.name for param in endpoint.path_params()] if endpoint is not None else []
    ) or plan_runner.path_params(item)
    for name in path_params:
        values[name] = st.text_input(
            f"`{name}` (path)",
            value=str(values.get(name, "")),
            key=f"plan_param_{item.plan_key}_{name}",
            help="Идентификатор $сущности на стенде; для тестовых — с префиксом `__TEST__`.",
        )
    query_params = plan_runner.query_params(item)
    if query_params:
        cols = st.columns(min(3, len(query_params)))
        for index, name in enumerate(query_params):
            values[name] = cols[index % len(cols)].text_input(
                f"`{name}` (query)",
                value=str(values.get(name, "")),
                key=f"plan_query_{item.plan_key}_{name}",
            )
    if item.body_kind == "json":
        values["body"] = st.text_area(
            "JSON-тело",
            value=str(values.get("body") or item.body_sample or ""),
            height=160,
            key=f"plan_body_{item.plan_key}",
            help="Тело запроса — то, что уйдёт на стенд; заготовка взята из спецификации.",
        )

    if values != item.params:
        item.params = values
        save(plan)

    st.code(
        _curl_preview(item, values, runtime),
        language="bash",
    )
    _render_payload_hint(item, session)
    col_run, col_next = st.columns(2)
    if col_run.button("▶ Выполнить этот вызов", key=f"plan_run_{item.plan_key}", type="primary"):
        plan.mode = plan_api.MODE_CALL
        _run_next(plan, session=session, runtime=runtime, bulk_confirmed=True, item=item)
    col_next.button(
        "▶ Выполнить следующую",
        key=f"plan_next_{item.plan_key}",
        on_click=_run_next,
        args=(plan,),
        kwargs={"session": session, "runtime": runtime, "bulk_confirmed": True},
    )


def _render_payload_hint(item: plan_api.PlanItem, session: TestSession | None) -> None:
    """Показывает, какие `__TEST__`-данные создаст проверка выбранного вызова.

    Пожелание заказчика (21.09.2026, п. 5): в режиме «по вызовам» видно, что уйдёт
    на стенд, включая тела и имена, которые генерирует сценарий проверки
    (`acceptance/test_payloads.py` — единый источник с кодом сценариев).
    """
    spec = plan_spec(item)
    if spec is None:
        return
    previews = test_payloads.payload_previews(spec, session, params=item.params)
    with st.expander(
        f"📤 Что отправляет проверка `{spec.check_id}` — {test_payloads.summary(previews)}",
        expanded=False,
    ):
        for line in test_payloads.preview_lines(previews):
            st.markdown(line)


def plan_spec(item: plan_api.PlanItem) -> Any:
    """Описание проверки-родителя пункта плана (или None)."""
    from acceptance.checks import catalog

    check_id = item.parent_id or (item.item_id if item.is_check else "")
    return catalog.find(check_id) if check_id else None


def _curl_preview(item: plan_api.PlanItem, values: dict[str, Any], runtime: Any) -> str:
    """Команда `curl` для настроенного вызова (копирование и воспроизведение вне пульта)."""
    from acceptance.exchange import curl_command

    path = item.path
    for name in plan_runner.path_params(item):
        path = path.replace(f"{{{name}}}", str(values.get(name, "")).strip() or f"{{{name}}}")
    query = "&".join(
        f"{name}={values.get(name)}"
        for name in plan_runner.query_params(item)
        if str(values.get(name, "")).strip()
    )
    return curl_command(
        method=item.method or "GET",
        path=path,
        query=query,
        body=str(values.get("body") or ""),
        base_url=getattr(runtime.settings, "base_url", ""),
        content_type="application/json",
    )
