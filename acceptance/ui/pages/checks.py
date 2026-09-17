"""Экран «Чек-лист проверок» (FR-T4; минимальная версия — этап T4).

Назначение: программа испытаний в виде проверок `TC-<модуль>-<NN>` с трассировкой
на требования (`UC`/`FR`/`BR`/`FSM`), классами (`tech`/`live`/`heavy`/`manual`),
ожидаемым результатом, вердиктом, заключением оператора и доказательствами.

Этап T4 реализует экран для **первой группы — `TC-TASK`** (8 проверок модуля
«Task service»): инфраструктура чек-листа появляется вместе с монитором задач,
потому что все асинхронные проверки (`dataset-fill`, `training`, `model-testing`,
`inference`) опираются на него. Остальные группы (TC-SYS…TC-CLEAN) добавляются в T3
и далее — каталог (`acceptance.checks.catalog`) уже рассчитан на все 69 проверок.

Ресурсоёмкие (`heavy`) проверки — полноправная часть программы: перед запуском
пульт требует цель проверки, используемые данные, ответственного и подтверждение
расхода ресурсов (FR-T10), а по завершении фиксирует `task_id`, время, статусы
FSM-1, метрики и решение о судьбе созданных артефактов.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import streamlit as st

from acceptance import endpoints as ep
from acceptance import notes
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks.registry import STATUS_ICONS, CheckSpec, CheckStatus
from acceptance.session import TestSession
from acceptance.tasks_monitor import TASK_COMMANDS, TaskMonitor
from acceptance.ui import state
from acceptance.ui.common import api_notes, check_run
from acceptance.ui.common.flash import render_flash, set_flash
from acceptance.ui.common.run_card import render_run_card

#: Группы, доступные на экране (T4 — только TC-TASK; расширение групп — этап T3).
AVAILABLE_GROUPS: tuple[str, ...] = ("TC-TASK",)

#: Ручные отметки оператора: статус → подпись кнопки.
MANUAL_MARKS: tuple[tuple[CheckStatus, str], ...] = (
    (CheckStatus.MANUAL_OK, "🖐️ Выполнена вручную"),
    (CheckStatus.SKIPPED, "⏭️ Пропущена"),
    (CheckStatus.BLOCKED, "🚫 Блокировано API"),
    (CheckStatus.INTERRUPTED, "⛔ Прервана"),
)

KEY_GROUP = "checks_group"
KEY_CHECK = "checks_selected"
KEY_TASK = "checks_task"
KEY_ACTION = "checks_action"
KEY_VERDICT = "checks_verdict"
KEY_NOTE = "checks_note"


def render() -> None:
    """Отрисовывает экран «Чек-лист проверок» (группа `TC-TASK`)."""
    st.title("Чек-лист проверок")
    st.caption(
        "Программа испытаний: проверки `TC-<модуль>-<NN>` с трассировкой на требования, "
        "классом, ожидаемым результатом, вердиктом и диапазоном журнала обмена. "
        "На этапе T4 доступна группа `TC-TASK` (модуль «Task service»)."
    )
    render_flash()

    summary = catalog.catalog_summary()
    st.caption(
        f"Каталог: групп — {summary['groups_implemented']} из {summary['groups_total']}, "
        f"проверок — {summary['checks_implemented']} из {summary['checks_total']} "
        "(остальные группы добавляются на этапах T3 и далее)."
    )

    session = state.current_session()
    if session is None:
        st.warning(
            "Сессия испытаний не выбрана: проверки выполняются, но результаты в отчёт не войдут."
        )

    group_key = str(
        st.selectbox(
            "Группа проверок",
            [f"{item.key} · {item.title}" for item in catalog.groups(implemented_only=True)],
            key=KEY_GROUP,
        )
    ).split(" · ")[0]
    if group_key not in AVAILABLE_GROUPS:
        st.info(f"Группа {group_key} наполняется на следующих этапах (`docs/02`).")
        return

    _render_kpi(session, group_key)
    st.divider()
    _render_table(session, group_key)
    st.divider()
    _render_card(session, group_key)


def _render_kpi(session: TestSession | None, group_key: str) -> None:
    """KPI группы: всего, выполнено, успех, отказ, блокировано, не выполнено."""
    ids = [spec.check_id for spec in catalog.by_group(group_key)]
    stats: dict[str, Any] = (
        checks_engine.group_stats(session, ids)
        if session is not None
        else {
            "total": len(ids),
            "done": 0,
            "passed": 0,
            "failed": 0,
            "blocked": 0,
            "not_run": len(ids),
        }
    )
    columns = st.columns(6)
    columns[0].metric("Проверок в группе", stats["total"])
    columns[1].metric("Выполнено", stats["done"])
    columns[2].metric("Успех", stats["passed"])
    columns[3].metric("Отказ", stats["failed"])
    columns[4].metric("Блокировано API", stats["blocked"])
    columns[5].metric("Не выполнено", stats["not_run"])


def _render_table(session: TestSession | None, group_key: str) -> None:
    """Таблица проверок группы с текущими статусами, вердиктами и диапазоном журнала."""
    results = checks_engine.results_map(session) if session is not None else {}
    rows: list[dict[str, Any]] = []
    for spec in catalog.by_group(group_key):
        result = results.get(spec.check_id)
        status = str(result.status) if result is not None else str(CheckStatus.NOT_RUN)
        rows.append(
            {
                "ID": spec.check_id,
                "Проверка": spec.title,
                "Требования": spec.requirement,
                "Класс": str(spec.check_class),
                "Статус": f"{STATUS_ICONS.get(status, '⚪')} {status}",
                "Вердикт": (result.verdict if result is not None else "") or "—",
                "Журнал": _journal_range(
                    result.journal_from if result is not None else None,
                    result.journal_to if result is not None else None,
                ),
            }
        )
    st.dataframe(rows, hide_index=True, use_container_width=True)


def _journal_range(first: int | None, last: int | None) -> str:
    """Диапазон номеров записей журнала проверки (`—`, если записей нет)."""
    if first is None and last is None:
        return "—"
    if first == last or last is None:
        return f"#{first}"
    return f"#{first}–#{last}"


def _render_card(session: TestSession | None, group_key: str) -> None:
    """Карточка проверки: шаги, ожидание, результат, запуск и ручные отметки."""
    options = {f"{spec.check_id} · {spec.title}": spec for spec in catalog.by_group(group_key)}
    chosen = st.selectbox("Проверка", list(options), key=KEY_CHECK)
    spec = options[chosen]

    st.markdown(f"### `{spec.check_id}` {spec.title}")
    st.caption(
        f"Требования: {spec.requirement} · класс: {spec.class_label} "
        f"({'подтверждение обязательно' if spec.is_confirmation_required else 'без подтверждения'}) · "
        f"эндпоинты: {', '.join(spec.endpoints) or '—'}"
    )
    col_steps, col_expected = st.columns(2)
    with col_steps:
        st.markdown("**Шаги проверки**")
        for index, step in enumerate(spec.steps, start=1):
            st.markdown(f"{index}. {step}")
    with col_expected:
        st.markdown("**Ожидаемый результат**")
        st.info(spec.expected)
        st.caption(
            f"Автоматический сценарий: `{spec.automation}`"
            if spec.automation
            else "Автоматического сценария нет: проверка выполняется вручную."
        )

    if session is None:
        return

    result = checks_engine.result_of(session, spec.check_id)
    stat_columns = st.columns(4)
    stat_columns[0].metric("Статус", f"{result.icon} {result.status}")
    stat_columns[1].metric(
        "Диапазон журнала", _journal_range(result.journal_from, result.journal_to)
    )
    stat_columns[2].metric(
        "Длительность, мс", f"{result.duration_ms:.0f}" if result.duration_ms else "—"
    )
    stat_columns[3].metric("Завершена", result.ended_at or "—")
    if result.verdict:
        st.success(f"Вердикт: {result.verdict}")
    if result.operator_note:
        st.caption(f"Заключение оператора: {result.operator_note}")
    if result.evidence:
        with st.expander("Доказательства"):
            st.json(result.evidence)

    _render_run(session, spec)
    st.divider()
    _render_manual_marks(session, spec, result)
    st.divider()
    _render_note_block(session, spec, result)


def _render_run(session: TestSession, spec: CheckSpec) -> None:
    """Запуск проверки: автоматический сценарий с параметрами задачи."""
    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес испытуемого сервера не задан: запуск проверки невозможен.")
        return

    monitor = state.task_monitor()
    task_id = ""
    action = "pause"
    if spec.check_id.startswith("TC-TASK-"):
        task_id = _render_task_picker(session, monitor, spec)
    if spec.check_id == "TC-TASK-06":
        action = str(
            st.selectbox(
                "Команда для проверки идемпотентности",
                list(TASK_COMMANDS),
                key=f"{KEY_ACTION}{spec.check_id}",
            )
        )

    run_card = None
    if spec.is_confirmation_required:
        st.warning(
            f"🟡 Проверка класса «{spec.class_label}» выполняется только после подтверждения "
            "оператора (NFR-T4, FR-T10)."
        )
        run_card = render_run_card(
            _run_card_spec(spec),
            key_prefix=f"checks_card_{spec.check_id}",
            default_responsible=session.info.operator_fio,
        )

    automation = "tasks.external_observation" if spec.check_id == "TC-TASK-08" else ""
    if st.button("▶ Запустить проверку", key=f"checks_run_{spec.check_id}", type="primary"):
        ready, reason, evidence = checks_engine.check_confirmation(spec, run_card)
        if not ready:
            set_flash("warning", reason)
            st.rerun()
        check_run.run_check(
            session=session,
            monitor=monitor,
            check_id=spec.check_id,
            params={"task_id": task_id, "action": action, "confirmation": evidence},
            automation=automation,
        )


def _render_task_picker(session: TestSession, monitor: TaskMonitor | None, spec: CheckSpec) -> str:
    """Выбор задачи для сценария проверки (из наблюдаемых задач сессии)."""
    if monitor is None:
        st.info("Монитор задач недоступен: параметры задачи задаются на экране «Задачи».")
        return ""
    observations = monitor.observed(session)
    if not observations and spec.check_id not in ("TC-TASK-02", "TC-TASK-07"):
        st.info(
            "Наблюдаемых задач нет: поставьте задачу на наблюдение на экране «Задачи» "
            "(или запустите `celery-test` на вкладке «Диагностика»)."
        )
        return ""
    options = {
        f"{item.task_id} · {item.task_type or '—'} · {item.status_label}": item.task_id
        for item in observations
    }
    chosen = st.selectbox(
        "Задача для сценария проверки",
        ["", *options],
        key=f"{KEY_TASK}{spec.check_id}",
        format_func=lambda value: value or "— задача не нужна —",
    )
    return options.get(str(chosen), "")


def _render_manual_marks(session: TestSession, spec: CheckSpec, result: Any) -> None:
    """Ручные отметки оператора: «вручную», «пропущена», «блокировано API», «прервана»."""
    st.markdown("**Отметка оператора**")
    verdict = st.text_input(
        "Вердикт / факт", value=str(result.verdict or ""), key=f"{KEY_VERDICT}{spec.check_id}"
    )
    operator_note = st.text_area(
        "Заключение оператора (обязательно для ручных отметок)",
        value=str(result.operator_note or ""),
        key=f"{KEY_NOTE}{spec.check_id}",
    )
    columns = st.columns(len(MANUAL_MARKS) + 1)
    for column, (status, label) in zip(columns, MANUAL_MARKS, strict=False):
        if column.button(label, key=f"checks_mark_{spec.check_id}_{status}"):
            if not operator_note.strip():
                set_flash("warning", "Ручная отметка требует заключения оператора (NFR-T4).")
                st.rerun()
            checks_engine.mark(
                session,
                spec,
                status,
                verdict=verdict,
                operator_note=operator_note,
                evidence={"operator": session.info.operator_fio or "оператор"},
            )
            state.store_session(session)
            set_flash("success", f"{spec.check_id}: {status}.")
            st.rerun()

    if columns[-1].button("↩ Сбросить результат", key=f"checks_reset_{spec.check_id}"):
        session.checks = [
            item for item in session.checks if str(item.get("check_id")) != spec.check_id
        ]
        session.add_history("check_reset", f"Результат проверки {spec.check_id} сброшен оператором")
        state.store_session(session)
        set_flash("warning", f"Результат {spec.check_id} сброшен.")
        st.rerun()


def _render_note_block(session: TestSession, spec: CheckSpec, result: Any) -> None:
    """Замечание к API по проверке: для статуса «блокировано API» обязательно (FR-T7)."""
    with st.expander("✍ Создать замечание к API по этой проверке"):
        st.caption(
            "Замечание — результат испытаний: несоответствие спецификации и фактического "
            "поведения сервера. Для статуса «блокировано API» замечание обязательно."
        )
        module_options = list(notes.MODULES)
        endpoint = spec.endpoints[0] if spec.endpoints else ""
        default_module = _module_for_endpoint(endpoint, module_options)
        col_priority, col_module = st.columns(2)
        priority = col_priority.selectbox(
            "Приоритет",
            list(notes.PRIORITIES),
            index=0 if str(result.status) == str(CheckStatus.BLOCKED) else 1,
            key=f"checks_note_priority_{spec.check_id}",
            help=" · ".join(f"{item} — {notes.PRIORITY_HINTS[item]}" for item in notes.PRIORITIES),
        )
        module = col_module.selectbox(
            "Модуль",
            module_options,
            index=module_options.index(default_module),
            key=f"checks_note_module_{spec.check_id}",
        )
        title = st.text_input(
            "Заголовок замечания",
            key=f"checks_note_title_{spec.check_id}",
            placeholder=f"{spec.check_id}: ",
        )
        fact = st.text_area(
            "Факт (что произошло)",
            value=str(result.verdict or ""),
            key=f"checks_note_fact_{spec.check_id}",
        )
        expected = st.text_area(
            "Ожидание (как должно быть по спецификации)",
            value=spec.expected,
            key=f"checks_note_expected_{spec.check_id}",
        )
        reproduction = st.text_area(
            "Воспроизведение",
            value="; ".join(spec.steps) if spec.steps else "",
            key=f"checks_note_repro_{spec.check_id}",
        )
        evidence = st.text_input(
            "Доказательства",
            value=(
                f"проверка {spec.check_id}; записи журнала "
                f"{_journal_range(result.journal_from, result.journal_to)}"
            ),
            key=f"checks_note_evidence_{spec.check_id}",
        )
        if st.button(
            "Добавить замечание в сессию", key=f"checks_note_add_{spec.check_id}", type="primary"
        ):
            note = api_notes.manual_note(
                title.strip() or f"{spec.check_id}: замечание по результату проверки",
                module=module,
                endpoint=endpoint,
                priority=priority,
                fact=fact,
                expected=expected,
                reproduction=reproduction,
                check_id=spec.check_id,
                evidence=evidence,
            )
            api_notes.create_note(note, session=session, module="checks")


def _module_for_endpoint(endpoint: str, module_options: list[str]) -> str:
    """Модуль замечания по эндпоинту проверки (реестр операций консоли — источник истины)."""
    found = ep.find(endpoint) if endpoint else None
    if found is not None and found.module in module_options:
        return found.module
    return "Task service" if "Task service" in module_options else module_options[-1]


@dataclass(frozen=True)
class _CheckEndpoint:
    """Обёртка проверки под карточку запуска (когда у проверки нет одного эндпоинта)."""

    spec: CheckSpec

    @property
    def title(self) -> str:
        """Подпись операции для карточки запуска."""
        return f"{self.spec.check_id} {self.spec.title}"

    @property
    def key(self) -> str:
        """Ключ операции (идентификатор проверки)."""
        return self.spec.check_id

    @property
    def note(self) -> str:
        """Ожидаемый результат проверки как «особенность операции»."""
        return self.spec.expected


def _run_card_spec(spec: CheckSpec) -> Any:
    """Описание операции для карточки запуска (`render_run_card`).

    Проверка чек-листа может опираться на несколько эндпоинтов, а карточка запуска
    работает с одной операцией реестра: берётся первый эндпоинт проверки, а если его
    нет — обёртка самой проверки.
    """
    if spec.endpoints:
        found = ep.find(spec.endpoints[0])
        if found is not None:
            return found
    return _CheckEndpoint(spec)
