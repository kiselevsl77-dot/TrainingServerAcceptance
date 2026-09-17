"""Запуск сценариев проверок из интерфейса (общий помощник экранов «Задачи» и «Чек-лист»).

Проверка чек-листа выполняется по одному правилу, откуда бы её ни запустили:

    * сценарий берётся из каталога (`acceptance.checks.catalog`) по описанию проверки;
    * прогон идёт через движок (`acceptance.checks.engine`) — с меткой `TC-…`, диапазоном
      записей журнала, длительностью и записью результата в `session.checks`;
    * ручные проверки (`manual`) не прогоняются, а подтверждаются оператором: для
      `TC-TASK-08` вердикт строит сценарий `tasks.external_observation`.

Помощник выделен, чтобы «Задачи» и «Чек-лист» не расходились в правилах: результат
всегда сохраняется в сессии и подписывается тем же набором статусов (`registry.py`).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks import tasks as check_tasks
from acceptance.checks.registry import CheckResult, CheckStatus
from acceptance.session import TestSession
from acceptance.tasks_monitor import TaskMonitor
from acceptance.ui import state
from acceptance.ui.common.flash import set_flash


def run_check(
    *,
    session: TestSession,
    monitor: TaskMonitor | None,
    check_id: str,
    params: dict[str, Any] | None = None,
    automation: str = "",
    rerun: bool = True,
) -> CheckResult | None:
    """Выполняет сценарий проверки и сохраняет результат в сессии.

    Args:
        session: сессия испытаний (результат проверки попадает в `session.checks`).
        monitor: монитор задач для сценариев FSM-1 (может быть None для чтения).
        check_id: идентификатор проверки (`TC-TASK-03`).
        params: параметры прогона (обычно `task_id` и `action` для BR-R3).
        automation: явный ключ сценария для ручных проверок.
        rerun: перерисовать экран после прогона (False — вернуть результат вызывающему).
    """
    spec = catalog.find(check_id)
    runtime = state.get_runtime()
    if spec is None or runtime is None:
        set_flash("warning", f"Проверка {check_id} недоступна: нет каталога или адреса стенда.")
        if rerun:
            st.rerun()
        return None

    context = check_tasks.AutomationContext(
        session=session,
        spec=spec,
        tasks=runtime.apis.tasks,
        journal=runtime.journal,
        monitor=monitor,
        params=dict(params or {}),
    )

    result = check_tasks.automate(context)
    if result is None:
        outcome = check_tasks.evaluate(context, automation=automation)
        if outcome is None:
            set_flash(
                "warning",
                f"У проверки {check_id} нет автоматического сценария: отметьте её вручную.",
            )
            if rerun:
                st.rerun()
            return None
        result = checks_engine.mark(
            session,
            spec,
            outcome.status,
            verdict=outcome.verdict,
            evidence=outcome.evidence,
            params=context.params,
        )

    state.store_session(session)
    set_flash(
        "success" if result.status == CheckStatus.PASSED else "warning",
        f"{check_id}: {result.status} — {result.verdict or 'без вердикта'}",
    )
    if rerun:
        st.rerun()
    return result
