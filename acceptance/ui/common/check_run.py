"""Запуск сценариев проверок из интерфейса (общий помощник экранов «Задачи» и «Чек-лист»).

Проверка чек-листа выполняется по одному правилу, откуда бы её ни запустили:

    * контекст собирает `build_context` — API модулей, монитор задач, журнал и «сырой»
      клиент консоли для негативных проб (`TC-SYS-03/05`, `TC-FILE-14`, `TC-LOAD-07`);
    * сценарий берётся из общего реестра (`acceptance.checks.runner`), прогон идёт через
      движок (`acceptance.checks.engine`) — с меткой `TC-…`, диапазоном записей журнала,
      длительностью, записью результата в `session.checks` и автоматическим замечанием
      к API при статусе «блокировано API» (FR-T7);
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
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckResult, CheckStatus
from acceptance.checks.runner import AutomationContext, RawProbe
from acceptance.session import TestSession
from acceptance.tasks_monitor import TaskMonitor
from acceptance.ui import state
from acceptance.ui.common.flash import set_flash


def build_context(
    *,
    session: TestSession,
    spec: Any,
    runtime: state.Runtime,
    monitor: TaskMonitor | None = None,
    client: Any | None = None,
    params: dict[str, Any] | None = None,
) -> AutomationContext:
    """Собирает контекст сценария для прогона из интерфейса.

    Args:
        session: сессия испытаний (в неё попадёт результат).
        spec: описание проверки из каталога.
        runtime: текущий рантайм пульта (API, журнал, настройки).
        monitor: монитор задач (нужен сценариям FSM-1).
        client: «сырой» httpx-клиент консоли для негативных проб (`state.console_client`).
        params: параметры запуска (`task_id`, `file_id`, `load_id`, `action`).
    """
    probe = (
        RawProbe(
            client=client,
            journal=runtime.journal,
            body_limit=runtime.config.body_limit,
        )
        if client is not None
        else None
    )
    return AutomationContext(
        session=session,
        spec=spec,
        tasks=runtime.apis.tasks,
        journal=runtime.journal,
        monitor=monitor,
        params=dict(params or {}),
        apis=runtime.apis,
        probe=probe,
    )


def run_check(
    *,
    session: TestSession,
    monitor: TaskMonitor | None,
    check_id: str,
    params: dict[str, Any] | None = None,
    automation: str = "",
    runtime: state.Runtime | None = None,
    run_evidence: dict[str, Any] | None = None,
    rerun: bool = True,
) -> CheckResult | None:
    """Выполняет сценарий проверки и сохраняет результат в сессии.

    Args:
        session: сессия испытаний (результат проверки попадает в `session.checks`).
        monitor: монитор задач для сценариев FSM-1 (может быть None для чтения).
        check_id: идентификатор проверки (`TC-FILE-01`).
        params: параметры прогона (задача, файл, нагрузка, команда FSM-1).
        automation: явный ключ сценария для ручных проверок.
        runtime: рантайм пульта (если не задан — берётся из состояния экрана).
        run_evidence: доказательства подтверждения (карточка запуска `live`/`heavy`).
        rerun: перерисовать экран после прогона (False — вернуть результат вызывающему).
    """
    spec = catalog.find(check_id)
    resolved = runtime or state.get_runtime()
    if spec is None or resolved is None:
        set_flash("warning", f"Проверка {check_id} недоступна: нет каталога или адреса стенда.")
        if rerun:
            st.rerun()
        return None

    context = build_context(
        session=session,
        spec=spec,
        runtime=resolved,
        monitor=monitor,
        client=state.console_client(),
        params=params,
    )

    result = check_runner.automate(context)
    if result is None:
        outcome = check_runner.evaluate(context, automation=automation)
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

    if run_evidence:
        result.evidence["run_card"] = dict(run_evidence)
        checks_engine.record_result(session, result)

    if result.status == CheckStatus.BLOCKED:
        checks_engine.ensure_defect_note(session, spec, result)

    state.store_session(session)
    set_flash(
        "success" if result.status == CheckStatus.PASSED else "warning",
        f"{check_id}: {result.status} — {result.verdict or 'без вердикта'}",
    )
    if rerun:
        st.rerun()
    return result
