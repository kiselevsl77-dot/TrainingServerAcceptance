"""Экран «Чек-лист проверок» (FR-T4; минимальная версия — этап T4).

Назначение: программа испытаний в виде проверок `TC-<модуль>-<NN>` с трассировкой
на требования (`UC`/`FR`/`BR`/`FSM`), классами (`tech`/`live`/`heavy`/`manual`),
ожидаемым результатом, вердиктом, заключением оператора и доказательствами.

Этап T4 реализует экран для **первой группы — `TC-TASK`** (8 проверок модуля
«Task service»): инфраструктура чек-листа появляется вместе с монитором задач,
потому что все асинхронные проверки (`dataset-fill`, `training`, `model-testing`,
`inference`) опираются на него. Дальше группы приходят по этапам: T3 — `TC-SYS`,
`TC-FILE`, `TC-REC`, `TC-LOAD`, T5 — `TC-DS` (датасеты); каталог
(`acceptance.checks.catalog`) рассчитан на все 69 проверок, наполнено 48.

Проверки модуля «Datasets» получают в карточке запуска пикер `dataset_id`, режим
чтения состава (провайдер `acceptance.dataset_composition`) и окна ожидания задачи
наполнения — состав датасета сервер не отдаёт (замечание P1), поэтому пульт ведёт
его учёт у себя и сверяет с серверным, когда тот появится.

Ресурсоёмкие (`heavy`) проверки — полноправная часть программы: перед запуском
пульт требует цель проверки, используемые данные, ответственного и подтверждение
расхода ресурсов (FR-T10), а по завершении фиксирует `task_id`, время, статусы
FSM-1, метрики и решение о судьбе созданных артефактов.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from acceptance import endpoints as ep
from acceptance import notes, test_payloads
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks.registry import CheckClass, CheckSpec, CheckStatus
from acceptance.dataset_composition import COMPOSITION_MODES, SOURCE_LABELS
from acceptance.logging_setup import log_event
from acceptance.paths import ARTIFACT_DIR, ensure_dirs
from acceptance.session import TestSession, add_artifact
from acceptance.tasks_monitor import TASK_COMMANDS, TaskMonitor
from acceptance.ui import state
from acceptance.ui.common import api_notes, check_run
from acceptance.ui.common.flash import render_flash, set_flash
from acceptance.ui.common.run_card import render_run_card

#: Колонки выгрузки чек-листа (приложение к отчёту, совпадает с `report.py`).
CHECK_COLUMNS: tuple[str, ...] = (
    "check_id",
    "group",
    "class",
    "module",
    "title",
    "requirement",
    "status",
    "verdict",
    "operator_note",
    "journal_from",
    "journal_to",
    "started_at",
    "ended_at",
    "duration_ms",
)

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
KEY_STATUS = "checks_filter_status"
KEY_CLASS = "checks_filter_class"
KEY_TEXT = "checks_filter_text"
KEY_PENDING = "checks_filter_pending"
KEY_BULK = "checks_bulk_run"
KEY_EXPORT = "checks_export_csv"
KEY_ARTIFACT = "checks_save_artifact"
KEY_FILE = "checks_file"
KEY_LOAD = "checks_load"
KEY_DATASET = "checks_dataset"
KEY_DATASET_PICK = "checks_dataset_pick"
KEY_COMPOSITION = "checks_composition_mode"
KEY_FILL_PAIRS = "checks_fill_pairs"
KEY_FILL_WAIT = "checks_fill_task_wait"
KEY_FILL_TIMEOUT = "checks_fill_task_timeout"

#: Подписи режимов чтения состава датасета (карточка запуска `TC-DS-03`).
COMPOSITION_MODE_LABELS: dict[str, str] = {
    "auto": "авто (сервер → факт пульта → гипотеза)",
    "server": "только серверный состав",
    "local": "только локальный учёт пульта",
    "heuristic": "только предположение по реестру файлов",
}

#: Режимы чтения состава, доступные оператору (порядок — от «авто» к частным).
COMPOSITION_MODE_OPTIONS: tuple[str, ...] = tuple(
    mode for mode in COMPOSITION_MODES if mode in COMPOSITION_MODE_LABELS
)

#: Сколько задач сервера предлагать в пикере, когда наблюдений в сессии ещё нет
#: (находка прогона 18.09.2026: без этого проверки FSM-1 остаются «пропущены»).
TASK_PICKER_LIMIT = 50


def render() -> None:
    """Отрисовывает экран «Чек-лист проверок» (все наполненные группы `TC-*`)."""
    st.title("Чек-лист проверок")
    st.caption(
        "Программа испытаний: проверки `TC-<модуль>-<NN>` с трассировкой на требования, "
        "классом, ожидаемым результатом, вердиктом и диапазоном журнала обмена. "
        "Этап T3 выполняется по группам `TC-SYS`, `TC-FILE`, `TC-REC`, `TC-LOAD` и `TC-TASK`."
    )
    render_flash()

    summary = catalog.catalog_summary()
    st.caption(
        f"Каталог: групп — {summary['groups_implemented']} из {summary['groups_total']}, "
        f"проверок — {summary['checks_implemented']} из {summary['checks_total']} "
        "(группы `TC-MOD`…`TC-CLEAN` добавляются на этапах T6–T10)."
    )

    session = state.current_session()
    if session is None:
        st.warning(
            "Сессия испытаний не выбрана: проверки выполняются, но результаты в отчёт не войдут."
        )

    filters = _render_filters(catalog.groups(implemented_only=True))
    rows = (
        checks_engine.checklist_rows(
            session,
            groups=filters["groups"],
            statuses=filters["statuses"],
            classes=filters["classes"],
            text=filters["text"],
            only_pending=filters["only_pending"],
        )
        if session is not None
        else []
    )

    _render_kpi(session, rows)
    st.divider()
    _render_table(rows)
    st.divider()
    _render_actions(session, filters, rows)
    st.divider()
    _render_card(session, _selected_row(rows))


def _render_filters(implemented: tuple[Any, ...]) -> dict[str, Any]:
    """Фильтры чек-листа: группа, статус, класс, поиск, «только невыполненные».

    Returns:
        Разобранные значения фильтров — по ним строятся таблица, KPI и групповой запуск.
    """
    options = {"ALL": "Все группы (сводка по чек-листу)"}
    options.update({item.key: f"{item.key} · {item.title}" for item in implemented})

    col_group, col_status, col_class, col_text = st.columns([2, 2, 1, 2])
    group_label = str(col_group.selectbox("Группа проверок", list(options.values()), key=KEY_GROUP))
    group_key = next(key for key, label in options.items() if label == group_label)

    selected_statuses = col_status.multiselect(
        "Статус",
        [str(status) for status in CheckStatus],
        key=KEY_STATUS,
        help="Пусто — показать проверки с любым статусом.",
    )
    selected_classes = col_class.multiselect(
        "Класс",
        [str(item) for item in CheckClass],
        key=KEY_CLASS,
        help="`tech` — безопасные, `live` — боевые, `heavy` — ресурсоёмкие, `manual` — ручные.",
    )
    text = col_text.text_input("Поиск по id / названию / требованиям", key=KEY_TEXT)
    only_pending = st.checkbox("Показать только невыполненные проверки", key=KEY_PENDING)

    return {
        "groups": () if group_key == "ALL" else (group_key,),
        "group_key": group_key,
        "statuses": tuple(selected_statuses),
        "classes": tuple(selected_classes),
        "text": text,
        "only_pending": only_pending,
    }


def _selected_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Выбор проверки для карточки (из текущей выборки)."""
    if not rows:
        return None
    labels = {f"{row['check_id']} · {row['title']}": row for row in rows}
    chosen = st.selectbox("Проверка для карточки", list(labels), key=KEY_CHECK)
    return labels[str(chosen)]


def _render_kpi(session: TestSession | None, rows: list[dict[str, Any]]) -> None:
    """KPI чек-листа: каталог и программа, состав видимой выборки и прогресс."""
    overall: dict[str, Any] = (
        checks_engine.overall_stats(session)
        if session is not None
        else {
            "implemented": len(catalog.CHECKS),
            "program_total": catalog.PLANNED_CHECKS_TOTAL,
        }
    )
    stats = _rows_stats(rows)

    columns = st.columns(7)
    columns[0].metric(
        "Проверок в каталоге", f"{overall['implemented']} / {overall['program_total']}"
    )
    columns[1].metric("В выборке", stats["total"])
    columns[2].metric("Выполнено", stats["done"])
    columns[3].metric("Успех", stats["passed"])
    columns[4].metric("Отказ", stats["failed"])
    columns[5].metric("Блокировано API", stats["blocked"])
    columns[6].metric("Не выполнено", stats["not_run"])

    progress = (stats["done"] / stats["total"]) if stats["total"] else 0.0
    st.progress(
        min(1.0, max(0.0, progress)),
        text=(
            f"Готовность выборки: {stats['done']} из {stats['total']} ({progress * 100:.0f}%) · "
            f"строк в таблице: {len(rows)}"
        ),
    )


def _rows_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Состав выборки по статусам: отобрано, выполнено, успех, отказ, блокировано.

    Считается по строкам таблицы, то есть по всем активным фильтрам (группа, класс,
    поиск, статус, «только невыполненные»), поэтому KPI всегда описывает то, что
    оператор видит на экране.
    """
    stats = {"total": len(rows), "done": 0, "passed": 0, "failed": 0, "blocked": 0, "not_run": 0}
    for row in rows:
        status = str(row["status"])
        if row["is_done"]:
            stats["done"] += 1
        if status == str(CheckStatus.PASSED):
            stats["passed"] += 1
        elif status == str(CheckStatus.FAILED):
            stats["failed"] += 1
        elif status == str(CheckStatus.BLOCKED):
            stats["blocked"] += 1
        elif status == str(CheckStatus.NOT_RUN):
            stats["not_run"] += 1
    return stats


def _render_table(rows: list[dict[str, Any]]) -> None:
    """Таблица проверок выборки: класс, статус, вердикт, диапазон журнала, время."""
    if not rows:
        st.info("По заданным фильтрам проверок нет.")
        return
    st.dataframe(
        [
            {
                "ID": row["check_id"],
                "Группа": row["group"],
                "Проверка": row["title"],
                "Требования": row["requirement"],
                "Класс": row["check_class"],
                "Статус": f"{row['icon']} {row['status']}",
                "Вердикт": row["verdict"] or "—",
                "Журнал": row["journal_range"],
                "Завершена": row["ended_at"] or "—",
            }
            for row in rows
        ],
        hide_index=True,
        use_container_width=True,
    )


def _render_actions(
    session: TestSession | None, filters: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    """Действия чек-листа: групповой прогон дешёвых проверок, экспорт, артефакты."""
    st.markdown("**Действия по выборке**")
    group_key = str(filters["group_key"])
    specs = catalog.CHECKS if group_key == "ALL" else catalog.by_group(group_key)
    cheap = [spec for spec in specs if spec.check_class == CheckClass.TECH and spec.automation]

    col_run, col_export, col_artifact = st.columns([2, 1, 1])
    if col_run.button(
        f"▶ Выполнить дешёвые проверки ({len(cheap)})",
        key=KEY_BULK,
        type="primary",
        help=(
            "Выполняются только проверки класса `tech` — безопасные и без подтверждения. "
            "Боевые (`live`) и ресурсоёмкие (`heavy`) запускаются по одной из карточки."
        ),
    ):
        _run_group(session, cheap)

    export_rows = checks_engine.checklist_rows(session) if session is not None else rows
    col_export.download_button(
        "⬇ Чек-лист (CSV)",
        data=_checks_csv(export_rows),
        file_name=f"checklist_{session.session_id if session else 'no-session'}.csv",
        mime="text/csv",
        use_container_width=True,
        key=KEY_EXPORT,
    )

    if session is None:
        col_artifact.button(
            "💾 Сохранить в артефакты",
            disabled=True,
            use_container_width=True,
            key=f"{KEY_ARTIFACT}_disabled",
            help="Нужна сессия испытаний: её артефакты попадают в отчёт.",
        )
        return
    if col_artifact.button(
        "💾 Сохранить в артефакты",
        use_container_width=True,
        key=KEY_ARTIFACT,
        help="Чек-лист (CSV + MD) сохраняется в `acceptance_data/artifacts` и входит в отчёт.",
    ):
        _save_checklist_artifact(session, export_rows)


def _run_group(session: TestSession | None, specs: list[CheckSpec]) -> None:
    """Групповой прогон дешёвых проверок с итоговой сводкой (кнопка на экране)."""
    if session is None:
        set_flash("warning", "Сессия испытаний не выбрана: результаты некуда сохранять.")
        st.rerun()
    if not specs:
        set_flash("info", "В выборке нет автоматических проверок класса `tech`.")
        st.rerun()

    runtime = state.get_runtime()
    if runtime is None:
        set_flash("error", "Адрес испытуемого сервера не задан: проверки не запустить.")
        st.rerun()

    monitor = state.task_monitor()
    client = state.console_client()
    results = checks_engine.run_checks(
        session,
        specs,
        context_factory=lambda spec: check_run.build_context(
            session=session,
            spec=spec,
            runtime=runtime,
            monitor=monitor,
            client=client,
        ),
    )
    state.store_session(session)
    tally: dict[str, int] = {}
    for result in results:
        tally[str(result.status)] = tally.get(str(result.status), 0) + 1
    summary = ", ".join(f"{status} — {count}" for status, count in sorted(tally.items()))
    set_flash(
        "success" if results else "info",
        f"Выполнено проверок: {len(results)}. Итоги: {summary or 'нет результатов'}.",
    )
    st.rerun()


def _checks_csv(rows: list[dict[str, Any]]) -> str:
    """Чек-лист в CSV: id, группа, класс, статус, вердикт, журнал (приложение отчёта)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CHECK_COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "check_id": row["check_id"],
                "group": row["group"],
                "class": row["check_class"],
                "module": row["module"],
                "title": row["title"],
                "requirement": row["requirement"],
                "status": row["status"],
                "verdict": row["verdict"],
                "operator_note": row["operator_note"],
                "journal_from": row["journal_from"] if row["journal_from"] is not None else "",
                "journal_to": row["journal_to"] if row["journal_to"] is not None else "",
                "started_at": row["started_at"] or "",
                "ended_at": row["ended_at"] or "",
                "duration_ms": row["duration_ms"] if row["duration_ms"] is not None else "",
            }
        )
    return buffer.getvalue()


def _save_checklist_artifact(session: TestSession, rows: list[dict[str, Any]]) -> None:
    """Сохраняет чек-лист (CSV + Markdown) в артефакты сессии — приложение отчёта."""
    ensure_dirs()
    csv_path = ARTIFACT_DIR / f"checklist_{session.session_id}.csv"
    md_path = ARTIFACT_DIR / f"checklist_{session.session_id}.md"
    csv_path.write_text(_checks_csv(rows), encoding="utf-8-sig")
    md_path.write_text(
        "# Чек-лист проверок\n\n"
        + "\n".join(
            f"| {row['check_id']} | {row['check_class']} | {row['status']} | "
            f"{row['verdict'] or '—'} | {row['journal_range']} |"
            for row in rows
        )
        + "\n",
        encoding="utf-8",
    )
    for path in (csv_path, md_path):
        _register_artifact(session, path, rows)
    state.store_session(session)
    log_event(
        "checklist_exported",
        f"Чек-лист сохранён в артефакты: {len(rows)} строк",
        module="checks",
        payload={"csv": csv_path.as_posix(), "md": md_path.as_posix()},
    )
    set_flash("success", f"Чек-лист сохранён в артефакты сессии: {csv_path.name}, {md_path.name}")
    st.rerun()


def _register_artifact(session: TestSession, path: Path, rows: list[dict[str, Any]]) -> None:
    """Регистрирует файл чек-листа в артефактах сессии (приложение отчёта)."""
    add_artifact(
        session,
        kind="checklist",
        path=path,
        note=f"чек-лист испытаний: {len(rows)} строк (сессия {session.session_id})",
    )


def _journal_range(first: int | None, last: int | None) -> str:
    """Диапазон номеров записей журнала проверки (`—`, если записей нет)."""
    if first is None and last is None:
        return "—"
    if first == last or last is None:
        return f"#{first}"
    return f"#{first}–#{last}"


def _render_card(session: TestSession | None, row: dict[str, Any] | None) -> None:
    """Карточка проверки: шаги, ожидание, результат, запуск и ручные отметки."""
    if row is None:
        st.info("Выберите проверку в таблице — откроется её карточка.")
        return
    spec = catalog.find(str(row["check_id"]))
    if spec is None:
        st.warning(f"Проверка {row['check_id']} отсутствует в каталоге.")
        return

    st.markdown(f"### `{spec.check_id}` {spec.title}")
    st.caption(
        f"Модуль: {spec.module} · требования: {spec.requirement} · класс: {spec.class_label} "
        f"({'подтверждение обязательно' if spec.is_confirmation_required else 'без подтверждения'})"
    )
    col_steps, col_expected = st.columns(2)
    with col_steps:
        st.markdown("**Шаги проверки**")
        for index, step in enumerate(spec.steps, start=1):
            st.markdown(f"{index}. {step}")
    with col_expected:
        st.markdown("**Ожидаемый результат**")
        st.info(spec.expected)
        targets = ", ".join(spec.endpoints) or "—"
        st.caption(f"Эндпоинты: {targets}")
        if spec.probe_paths:
            st.caption(
                "Негативные пробы (маршрутов нет в спецификации): " + ", ".join(spec.probe_paths)
            )
        st.caption(
            f"Автоматический сценарий: `{spec.automation}`"
            if spec.automation
            else "Автоматического сценария нет: проверка выполняется вручную."
        )
        if spec.blocked_by_api:
            st.caption(f"Ожидаемо блокировано API: {spec.blocked_by_api}")

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

    _render_payload_block(session, spec)

    _render_run(session, spec)
    st.divider()
    _render_manual_marks(session, spec, result)
    st.divider()
    _render_note_block(session, spec, result)


def _render_payload_block(session: TestSession | None, spec: CheckSpec) -> None:
    """Блок «что будет отправлено»: `__TEST__`-данные, которые создаст проверка.

    Пожелание заказчика (21.09.2026, п. 5): тела и имена `__TEST__`-сущностей
    генерирует код сценария, поэтому оператор должен видеть их **до** запуска, а не
    искать в журнале после. Правила берутся из `acceptance.test_payloads` (единый
    источник с константами сценариев).
    """
    previews = test_payloads.payload_previews(spec, session)
    title = f"📤 Что будет отправлено (`{test_payloads.TEST_PREFIX}`-данные)"
    with st.expander(f"{title} — {test_payloads.summary(previews)}", expanded=False):
        for line in test_payloads.preview_lines(previews):
            st.markdown(line)
        st.caption(
            "Имена и идентификаторы с пометкой «генерируется при выполнении» создаются "
            "в момент запуска; всё созданное пульт убирает за собой и учитывает в сессии."
        )


def _render_run(session: TestSession, spec: CheckSpec) -> None:
    """Запуск проверки: автоматический сценарий с параметрами цели (задача/файл/нагрузка)."""
    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес испытуемого сервера не задан: запуск проверки невозможен.")
        return

    monitor = state.task_monitor()
    params: dict[str, Any] = {}
    if spec.check_id.startswith("TC-TASK-"):
        task_id = _render_task_picker(session, monitor, spec)
        if task_id:
            params["task_id"] = task_id
    if spec.check_id.startswith(("TC-FILE-", "TC-REC-")):
        file_id = _render_file_picker(spec)
        if file_id:
            params["file_id"] = file_id
    if spec.check_id.startswith("TC-LOAD-"):
        load_id = _render_load_picker(spec)
        if load_id:
            params["load_id"] = load_id
    if spec.check_id.startswith("TC-DS-"):
        params.update(_render_dataset_params(spec))
    if spec.check_id == "TC-TASK-06":
        params["action"] = str(
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
        _ensure_task_observed(session, monitor, spec, params.get("task_id"))
        result = check_run.run_check(
            session=session,
            monitor=monitor,
            check_id=spec.check_id,
            params=params,
            automation=automation,
            runtime=runtime,
            run_evidence=evidence,
            rerun=False,
        )
        if result is not None and result.status == CheckStatus.BLOCKED:
            note = checks_engine.ensure_defect_note(session, spec, result)
            state.store_session(session)
            if note is not None:
                set_flash("info", f"Замечание к API создано автоматически: {note['title']}")
        st.rerun()


def _render_file_picker(spec: CheckSpec) -> str:
    """Живой список файлов для подстановки `file_id` (TC-FILE, TC-REC)."""
    files, error = state.load_files()
    if error:
        st.caption(f"Список файлов недоступен: {error}")
        return ""
    if not files:
        st.caption("Реестр файлов пуст: `file_id` можно не выбирать — сценарий подберёт сам.")
        return ""
    labels = {
        f"{file.file_name} · {file.file_type} · {file.size} Б": str(file.id) for file in files
    }
    chosen = st.selectbox(
        "Файл (file_id)",
        ["— подобрать автоматически —", *labels],
        key=f"{KEY_FILE}{spec.check_id}",
    )
    return labels.get(str(chosen), "")


def _render_load_picker(spec: CheckSpec) -> str:
    """Живой список нагрузок для подстановки `load_id` (TC-LOAD)."""
    loads, error = state.load_loads()
    if error:
        st.caption(f"Список нагрузок недоступен: {error}")
        return ""
    if not loads:
        st.caption("Реестр нагрузок пуст: `load_id` можно не выбирать — сценарий подберёт сам.")
        return ""
    labels = {f"{item.load_id} · {item.category}": str(item.load_id) for item in loads}
    chosen = st.selectbox(
        "Нагрузка (load_id)",
        ["— подобрать автоматически —", *labels],
        key=f"{KEY_LOAD}{spec.check_id}",
    )
    return labels.get(str(chosen), "")


def _render_dataset_picker(spec: CheckSpec) -> str:
    """Пикер `dataset_id` для сценариев `TC-DS`: живой реестр датасетов и ручной ввод."""
    datasets, error = state.load_datasets()
    if error:
        st.caption(f"Реестр датасетов недоступен: {error}")
    options: dict[str, str] = {}
    for item in datasets:
        created = (
            item.creation_date.strftime("%d.%m.%Y") if item.creation_date else "дата неизвестна"
        )
        options[f"{item.name} · {item.type} · {created}"] = str(item.id)
    manual = st.text_input(
        "Идентификатор датасета (UUID), если нужного нет в списке",
        key=f"{KEY_DATASET}{spec.check_id}",
        placeholder="например, 5f0a5b7e-…",
    )
    if manual.strip():
        return manual.strip()
    if not options:
        st.caption("Реестр датасетов пуст: будет использован первый датасет реестра.")
        return ""
    chosen = st.selectbox(
        "Датасет для сценария проверки",
        ["", *options],
        key=f"{KEY_DATASET_PICK}{spec.check_id}",
        format_func=lambda value: value or "— первый в реестре —",
    )
    return options.get(str(chosen), "")


def _render_dataset_params(spec: CheckSpec) -> dict[str, Any]:
    """Параметры сценариев `TC-DS`: датасет, режим состава и окна ожидания наполнения."""
    params: dict[str, Any] = {}
    if spec.check_id in ("TC-DS-02", "TC-DS-03"):
        dataset_id = _render_dataset_picker(spec)
        if dataset_id:
            params["dataset_id"] = dataset_id
    if spec.check_id == "TC-DS-03":
        sources = "; ".join(
            SOURCE_LABELS[mode] for mode in COMPOSITION_MODE_OPTIONS if mode in SOURCE_LABELS
        )
        params["composition_mode"] = str(
            st.selectbox(
                "Режим чтения состава датасета",
                list(COMPOSITION_MODE_OPTIONS),
                key=f"{KEY_COMPOSITION}{spec.check_id}",
                format_func=lambda value: COMPOSITION_MODE_LABELS.get(value, value),
                help=(
                    "Источники состава: " + sources + ". Серверный состав появится, когда "
                    "замечание P1 устранят: проверка перейдёт в строгий режим сама."
                ),
            )
        )
    if spec.check_id == "TC-DS-05":
        st.caption(
            "Проверка создаёт собственный `__TEST__`-датасет: состав датасета сервер не "
            "отдаёт (P1), а удалить файлы из датасета нельзя — наполнение боевого "
            "датасета было бы необратимым изменением стенда."
        )
        col_pairs, col_wait, col_timeout = st.columns(3)
        params["fill_pairs"] = str(
            col_pairs.number_input(
                "Пар RAW+markup для наполнения",
                min_value=1,
                max_value=10,
                value=1,
                step=1,
                key=f"{KEY_FILL_PAIRS}{spec.check_id}",
            )
        )
        params["task_wait"] = str(
            col_wait.number_input(
                "Поиск задачи `dataset-fill`, с",
                min_value=0,
                max_value=300,
                value=30,
                step=5,
                key=f"{KEY_FILL_WAIT}{spec.check_id}",
            )
        )
        params["task_timeout"] = str(
            col_timeout.number_input(
                "Ожидание терминального статуса, с",
                min_value=60,
                max_value=3600,
                value=600,
                step=60,
                key=f"{KEY_FILL_TIMEOUT}{spec.check_id}",
            )
        )
    return params


def _server_task_options() -> dict[str, str]:
    """Живой список задач сервера для подстановки `task_id` (пикер чек-листа).

    Нужен там, где в сессии ещё нет наблюдений: проверки `TC-TASK-03/04/05/06`
    требуют `task_id`, а до 18.09.2026 пикер показывал только наблюдаемые задачи,
    поэтому при живом реестре задач проверки оставались «пропущены».
    """
    tasks, _total, error = state.load_tasks(limit=TASK_PICKER_LIMIT)
    if error:
        st.caption(f"Список задач недоступен ({error}): выберите задачу на экране «Задачи».")
        return {}
    return {f"{item.id} · {item.type} · {item.name}": str(item.id) for item in tasks}


def _ensure_task_observed(
    session: TestSession,
    monitor: TaskMonitor | None,
    spec: CheckSpec,
    task_id: Any,
) -> None:
    """Ставит на наблюдение задачу, выбранную из серверного списка (BR-R5).

    История переходов FSM-1 собирается поллингом, поэтому проверка задачи,
    выбранной в пикере из списка сервера, должна попасть в наблюдение **до**
    запуска сценария — иначе цепочка статусов окажется пустой.
    """
    if monitor is None or not task_id:
        return
    if any(item.task_id == str(task_id) for item in monitor.observed(session)):
        return

    tasks, _total, _error = state.load_tasks(limit=TASK_PICKER_LIMIT)
    known = next((item for item in tasks if str(item.id) == str(task_id)), None)
    if spec.check_id == "TC-TASK-08" or known is None:
        monitor.register_external(session, str(task_id), check_id=spec.check_id)
    else:
        monitor.register_task(session, known, check_id=spec.check_id)
    monitor.poll_once(session, str(task_id), label=spec.check_id)
    state.store_session(session)


def _render_task_picker(session: TestSession, monitor: TaskMonitor | None, spec: CheckSpec) -> str:
    """Выбор задачи для сценария: наблюдаемые задачи сессии, иначе список сервера."""
    if monitor is None:
        st.info("Монитор задач недоступен: параметры задачи задаются на экране «Задачи».")
        return ""

    observations = monitor.observed(session)
    options = {
        f"{item.task_id} · {item.task_type or '—'} · {item.status_label}": item.task_id
        for item in observations
    }
    if not options:
        options = _server_task_options()
        if not options:
            if spec.check_id not in ("TC-TASK-02", "TC-TASK-07"):
                st.info(
                    "Наблюдаемых задач нет: поставьте задачу на наблюдение на экране «Задачи» "
                    "(или запустите `celery-test` на вкладке «Диагностика»)."
                )
            return ""
        st.caption(
            "Наблюдаемых задач в сессии нет: показан список задач сервера. Выбранная задача "
            "будет поставлена на наблюдение перед запуском (BR-R5)."
        )

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
