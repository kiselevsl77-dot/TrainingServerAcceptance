"""Экран «Замечания к API» (FR-T7, этап T2).

Реестр замечаний испытаний: ручные замечания оператора и автоматические по
известным дефектам API, с приоритетом (`P0`/`P1`/`P2`), модулем, эндпоинтом,
фактом, ожиданием, воспроизведением и привязкой к проверке (`check_id`).

Замечания хранятся в сессии испытаний (`TestSession.notes`) и попадают в отчёт
разделом «Замечания к API» с группировкой по приоритету. Отдельный раздел —
«Перспективные требования» (не замечания приёмки, а бэклог к API), в первую
очередь субдатасеты.
"""

from __future__ import annotations

import streamlit as st

from acceptance import notes as notes_api
from acceptance.logging_setup import log_event
from acceptance.session import TestSession, add_artifact
from acceptance.ui import state
from acceptance.ui.common import api_notes
from acceptance.ui.common.flash import render_flash, set_flash

#: Ключи состояния экрана.
KEY_DELETE_CONFIRM = "notes_delete_confirm"
KEY_TEMPLATE = "notes_template"
KEY_TEMPLATE_LABEL = "notes_template_label"
KEY_TEMPLATE_EVIDENCE = "notes_template_evidence"

FILTER_ALL = "все"
SOURCE_OPTIONS = (FILTER_ALL, notes_api.SOURCE_AUTO, notes_api.SOURCE_OPERATOR)
LINK_OPTIONS = (FILTER_ALL, "с привязкой к проверке", "без привязки")


def render() -> None:
    """Отрисовывает экран «Замечания к API»."""
    st.title("Реестр замечаний к API")
    st.caption(
        "Замечания формируются оператором из любого места пульта (консоль запросов, записи, "
        "проверки) и идут в отчёт разделом «Замечания к API» (P0/P1/P2). Перспективные "
        "требования к API ведутся отдельно — как бэклог."
    )
    render_flash()

    session = state.current_session()
    if session is None:
        st.warning(
            "Сессия испытаний не выбрана: замечания нельзя зарегистрировать в отчёте — "
            "выберите или создайте сессию на экране «Сессия испытаний»."
        )

    notes = api_notes.notes_of(session)
    _render_kpi(notes)

    tabs = st.tabs(
        [
            "📋 Реестр замечаний",
            "➕ Добавить замечание",
            "🧭 Перспективные требования",
            "⬇ Выгрузка",
        ]
    )
    with tabs[0]:
        _render_registry(notes, session=session)
    with tabs[1]:
        _render_add(session=session)
    with tabs[2]:
        _render_prospective(notes, session=session)
    with tabs[3]:
        _render_export(notes, session=session)


def _render_kpi(notes: list[dict]) -> None:
    """KPI реестра: всего, по приоритетам, источники, привязка к проверкам."""
    summary = api_notes.notes_summary(notes)
    col_total, col_p0, col_p1, col_p2 = st.columns(4)
    col_total.metric("Замечаний всего", summary["total"])
    col_p0.metric("P0 (блокирующие)", summary["by_priority"].get("P0", 0))
    col_p1.metric("P1 (с обходным путём)", summary["by_priority"].get("P1", 0))
    col_p2.metric("P2 (улучшения)", summary["by_priority"].get("P2", 0))

    col_auto, col_operator, col_linked = st.columns(3)
    col_auto.metric("Из шаблонов (авто)", summary["by_source"].get(notes_api.SOURCE_AUTO, 0))
    col_operator.metric("Ручных (оператор)", summary["by_source"].get(notes_api.SOURCE_OPERATOR, 0))
    col_linked.metric("Привязано к проверкам", summary["linked_to_checks"])


def _render_registry(notes: list[dict], *, session: TestSession | None) -> None:
    """Реестр замечаний: фильтры, таблица, карточка выбранного, удаление."""
    if not notes:
        st.info(
            "Замечаний пока нет. Их можно создать вручную или добавить готовые шаблоны "
            "известных дефектов на вкладке «➕ Добавить замечание»."
        )
        return

    col_priority, col_module, col_source, col_link = st.columns(4)
    priority = col_priority.selectbox(
        "Приоритет", (FILTER_ALL, *notes_api.PRIORITIES), key="notes_f_priority"
    )
    modules = sorted({str(note.get("module") or "Прочее") for note in notes})
    module = col_module.selectbox("Модуль", (FILTER_ALL, *modules), key="notes_f_module")
    source = col_source.selectbox("Источник", SOURCE_OPTIONS, key="notes_f_source")
    linked = col_link.selectbox("Привязка", LINK_OPTIONS, key="notes_f_linked")
    search = st.text_input(
        "Поиск по тексту замечания",
        key="notes_f_search",
        placeholder="например: latin-1, phase_connection",
    )

    filtered = api_notes.filter_notes(
        notes,
        priority=None if priority == FILTER_ALL else priority,
        module=None if module == FILTER_ALL else module,
        source=None if source == FILTER_ALL else source,
        linked=None if linked == FILTER_ALL else linked,
        search=search,
    )
    st.caption(f"Показано {len(filtered)} из {len(notes)} замечаний.")
    st.dataframe(api_notes.notes_rows(filtered), hide_index=True, use_container_width=True)

    if not filtered:
        return

    options = {
        f"{note.get('priority')} · {note.get('module')} · {note.get('title')}": note
        for note in filtered
    }
    # ключ зависит от состава списка: после удаления замечания выбор не «залипает»
    detail_key = f"notes_detail_{len(filtered)}_{list(options)[0][:40]}"
    chosen = st.selectbox("Карточка замечания", list(options), key=detail_key)
    note = options[chosen]

    st.markdown(f"#### {note.get('priority')} · {note.get('title')}")
    st.caption(
        f"модуль: {note.get('module')} · эндпоинт: {note.get('endpoint') or '—'} · "
        f"проверка: {note.get('check_id') or '—'} · источник: {note.get('source')} · "
        f"создано: {note.get('created_at')}"
    )
    st.markdown(f"**Приоритет:** {api_notes.priority_hint(str(note.get('priority')))}")
    for label, key in (
        ("Факт", "fact"),
        ("Ожидание", "expected"),
        ("Воспроизведение", "reproduction"),
        ("Доказательства", "evidence"),
    ):
        value = str(note.get(key) or "").strip()
        if value:
            st.markdown(f"**{label}:** {value}")

    with st.expander("✏️ Уточнить формулировку (правит замечание в сессии)"):
        _render_edit_form(note, session=session)

    confirm = st.checkbox("Подтверждаю удаление замечания из сессии", key=KEY_DELETE_CONFIRM)
    if st.button(
        "🗑 Удалить замечание",
        key="notes_delete",
        disabled=not (confirm and session is not None),
        help="Удаление ошибочно созданного замечания; действие фиксируется в истории сессии.",
    ):
        if session is None or not api_notes.remove_note(session, str(note.get("note_id"))):
            set_flash(
                "warning", "Замечание не удалено: сессия не выбрана или замечание уже удалено."
            )
            st.rerun()
        set_flash("success", f"Замечание удалено из сессии: {note.get('title')}")
        st.rerun()


def _render_edit_form(note: dict, *, session: TestSession | None) -> None:
    """Правка формулировки замечания (факт, ожидание, доказательства, приоритет)."""
    suffix = str(note.get("note_id") or "note")
    col_priority, col_module = st.columns(2)
    priority = col_priority.selectbox(
        "Приоритет",
        list(notes_api.PRIORITIES),
        index=list(notes_api.PRIORITIES).index(str(note.get("priority") or "P1"))
        if str(note.get("priority")) in notes_api.PRIORITIES
        else 1,
        key=f"notes_edit_priority_{suffix}",
    )
    modules = list(notes_api.MODULES)
    current_module = str(note.get("module") or "Прочее")
    module = col_module.selectbox(
        "Модуль",
        modules,
        index=modules.index(current_module) if current_module in modules else len(modules) - 1,
        key=f"notes_edit_module_{suffix}",
    )
    fact = st.text_area("Факт", value=str(note.get("fact") or ""), key=f"notes_edit_fact_{suffix}")
    expected = st.text_area(
        "Ожидание", value=str(note.get("expected") or ""), key=f"notes_edit_expected_{suffix}"
    )
    reproduction = st.text_area(
        "Воспроизведение",
        value=str(note.get("reproduction") or ""),
        key=f"notes_edit_repro_{suffix}",
    )
    evidence = st.text_input(
        "Доказательства", value=str(note.get("evidence") or ""), key=f"notes_edit_evidence_{suffix}"
    )
    if st.button("Сохранить изменения", key=f"notes_edit_save_{suffix}", disabled=session is None):
        if session is None:
            set_flash("warning", "Сессия не выбрана: изменения сохранить некуда.")
            st.rerun()
        updated = notes_api.ApiNote.from_dict(
            {
                **note,
                "priority": priority,
                "module": module,
                "fact": fact,
                "expected": expected,
                "reproduction": reproduction,
                "evidence": evidence,
            }
        )
        api_notes.update_note(session, updated)
        set_flash("success", f"Замечание обновлено: {updated.title}")
        st.rerun()


def _render_add(*, session: TestSession | None) -> None:
    """Добавление замечания: из шаблона известного дефекта или вручную."""
    st.markdown("### Готовые шаблоны известных дефектов API")
    st.caption(
        "11 замечаний, выявленных при работе с сервером (latin-1, отсутствие Range, "
        "phase_connection, task_id в fill и др.). Шаблон можно дополнить доказательствами."
    )
    titles = api_notes.template_titles()
    chosen = st.selectbox("Шаблон", titles, key=KEY_TEMPLATE)
    template = api_notes.find_template(chosen)
    if template is not None:
        st.markdown(
            f"**{template.priority} · {template.module} · {template.endpoint}**\n\n"
            f"* Факт: {template.fact}\n"
            f"* Ожидание: {template.expected}\n"
            f"* Воспроизведение: {template.reproduction}"
        )
    col_label, col_evidence = st.columns(2)
    check_id = col_label.text_input(
        "Метка проверки (TC-…)",
        key=KEY_TEMPLATE_LABEL,
        placeholder="TC-FILE-01",
        help="Привязка замечания к проверке чек-листа: попадает в отчёт.",
    )
    evidence = col_evidence.text_input(
        "Доказательства (записи журнала, имена файлов, размеры)",
        key=KEY_TEMPLATE_EVIDENCE,
        placeholder="например: Antminer_S19.raw.csv → 404, запись журнала #37",
    )
    if st.button(
        "➕ Добавить шаблон в сессию",
        key="notes_add_template",
        type="primary",
        disabled=session is None,
    ):
        if session is None:
            set_flash("warning", "Сессия не выбрана: замечание не будет зарегистрировано в отчёте.")
            st.rerun()
        added = api_notes.create_note_from_template(
            chosen,
            session=session,
            check_id=check_id.strip(),
            evidence=evidence.strip(),
            module="notes",
            rerun=False,
        )
        set_flash(
            "success" if added else "info",
            f"Шаблон добавлен в сессию: {chosen}"
            if added
            else f"Такое замечание уже есть: {chosen}",
        )
        st.rerun()

    st.divider()
    st.markdown("### Своё замечание (вручную)")
    _render_manual_form(session=session)


def _render_manual_form(*, session: TestSession | None) -> None:
    """Форма ручного замечания к API (FR-T7: создание из любого места пульта)."""
    module_options = list(notes_api.MODULES)
    col_module, col_priority = st.columns(2)
    module = col_module.selectbox("Модуль", module_options, key="notes_new_module")
    priority = col_priority.selectbox(
        "Приоритет", list(notes_api.PRIORITIES), index=1, key="notes_new_priority"
    )
    title = st.text_input("Заголовок замечания *", key="notes_new_title")
    endpoint = st.text_input(
        "Эндпоинт", key="notes_new_endpoint", placeholder="GET /api/data/files"
    )
    col_check, col_evidence = st.columns(2)
    check_id = col_check.text_input("Метка проверки (TC-…)", key="notes_new_check")
    evidence = col_evidence.text_input("Доказательства", key="notes_new_evidence")
    fact = st.text_area("Факт (что произошло) *", key="notes_new_fact")
    expected = st.text_area("Ожидание (как должно быть)", key="notes_new_expected")
    reproduction = st.text_area("Воспроизведение (шаги/запрос)", key="notes_new_repro")
    if st.button(
        "➕ Добавить замечание",
        key="notes_add_manual",
        type="primary",
        disabled=session is None or not title.strip(),
    ):
        if session is None:
            set_flash("warning", "Сессия не выбрана: замечание не будет зарегистрировано в отчёте.")
            st.rerun()
        note = api_notes.manual_note(
            title,
            module=module,
            endpoint=endpoint,
            priority=priority,
            fact=fact,
            expected=expected,
            reproduction=reproduction,
            check_id=check_id.strip() or None,
            evidence=evidence,
        )
        added = api_notes.create_note(note, session=session, module="notes", rerun=False)
        set_flash(
            "success" if added else "info",
            f"Замечание добавлено в сессию: {note.title}"
            if added
            else f"Такое замечание уже есть в сессии: {note.title}",
        )
        st.rerun()


def _render_prospective(notes: list[dict], *, session: TestSession | None) -> None:
    """Перспективные требования к API (бэклог, а не замечания текущей приёмки)."""
    st.caption(
        "Перспективные требования — это предложения к развитию API (в первую очередь "
        "субдатасеты). В отчёт они идут отдельным разделом как бэклог и не влияют на "
        "решение о приёмке текущей сборки."
    )
    rows = api_notes.prospective_rows()
    st.dataframe(
        [
            {
                "Требование": row["title"],
                "Модуль": row["module"],
                "Приоритет": row["priority"],
                "Содержание": row["detail"],
            }
            for row in rows
        ],
        hide_index=True,
        use_container_width=True,
    )

    st.divider()
    st.markdown("**Создать замечание из перспективного требования**")
    options = {f"{row['priority']} · {row['title']}": row for row in rows}
    chosen = st.selectbox("Требование", list(options), key="notes_prospective_pick")
    row = options[chosen]
    if st.button(
        "➕ Добавить как замечание к API",
        key="notes_prospective_add",
        disabled=session is None,
        help="Требование попадёт в реестр замечаний с указанным приоритетом и модулем.",
    ):
        if session is None:
            set_flash("warning", "Сессия не выбрана: замечание не будет зарегистрировано в отчёте.")
            st.rerun()
        note = api_notes.manual_note(
            str(row["title"]),
            module=str(row["module"]),
            priority=str(row["priority"]),
            fact=str(row["detail"]),
            expected="Требование включено в план развития API (бэклог приёмки).",
            reproduction="Обсуждение с разработчиком API по итогам испытаний.",
        )
        added = api_notes.create_note(note, session=session, module="notes", rerun=False)
        set_flash(
            "success" if added else "info",
            f"Требование добавлено в реестр замечаний: {row['title']}"
            if added
            else f"Такое замечание уже есть: {row['title']}",
        )
        st.rerun()


def _render_export(notes: list[dict], *, session: TestSession | None) -> None:
    """Выгрузка реестра замечаний: JSON, CSV (Excel) и Markdown-раздел отчёта."""
    if not notes:
        st.info("Выгружать нечего: реестр замечаний пуст.")
        return

    meta = {
        "session_id": session.session_id if session else None,
        "server_build": session.server_build if session else "",
        "base_url": session.base_url if session else "",
        "operator": session.info.operator_fio if session else "",
    }
    st.markdown("**Раздел отчёта (Markdown)**")
    st.code(api_notes.notes_to_markdown(notes, meta=meta), language="markdown")

    col_json, col_csv, col_save = st.columns(3)
    col_json.download_button(
        "⬇ JSON",
        data=api_notes.notes_to_json(notes, meta).encode("utf-8"),
        file_name="api_notes.json",
        mime="application/json",
        key="notes_download_json",
        use_container_width=True,
    )
    col_csv.download_button(
        "⬇ CSV (Excel, utf-8-sig)",
        data=api_notes.notes_to_csv(notes).encode("utf-8-sig"),
        file_name="api_notes.csv",
        mime="text/csv",
        key="notes_download_csv",
        use_container_width=True,
    )
    if col_save.button(
        "💾 Сохранить в артефакты сессии",
        key="notes_save_artifacts",
        use_container_width=True,
        disabled=session is None,
        help="Комплект (md, csv, json) сохраняется в acceptance_data/artifacts и в сессии.",
    ):
        if session is None:
            set_flash("warning", "Сессия не выбрана: артефакты не будут зарегистрированы в отчёте.")
            st.rerun()
        paths = api_notes.save_notes_export(notes, meta=meta)
        for kind, path in paths.items():
            add_artifact(
                session,
                kind=f"замечания к API ({kind})",
                path=path,
                note=f"реестр замечаний: {len(notes)} шт.",
            )
        state.store_session(session)
        log_event(
            "notes_export_saved",
            f"Реестр замечаний сохранён: {', '.join(path.name for path in paths.values())}",
            module="notes",
            payload={"notes": len(notes), "paths": [path.as_posix() for path in paths.values()]},
        )
        set_flash(
            "success",
            "Комплект замечаний сохранён и зарегистрирован в сессии: "
            + ", ".join(path.name for path in paths.values()),
        )
        st.rerun()
