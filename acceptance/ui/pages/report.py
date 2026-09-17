"""Экран «Отчёт испытаний» (FR-T8/FR-T9, этапы T3/T9).

Назначение: одна кнопка — и готовый комплект отчётности по сессии:

    * отчёт `*.md` (шапка сессии, программа и объём, снимки стенда, сводка и детализация
      проверок, наблюдение за задачами, артефакты и `__TEST__`-сущности, замечания к API
      по приоритетам, перспективные требования, итог и подписи);
    * отчёт `*.json` (машиночитаемая копия для обработки и архива);
    * приложения: таблица проверок (CSV), замечания (CSV), перечень `__TEST__`-сущностей
      (CSV), выдержка структурного журнала (JSONL).

Отчёт строится **только** по данным сессии и журнала, поэтому воспроизводим: любой шаг
проверки можно повторить по записи `http_request`/`http_response`. Полный комплект
(сравнение сессий, финализация с подписями) — этап T9.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from acceptance import report as report_api
from acceptance.logging_setup import log_event
from acceptance.session import TestSession, add_artifact
from acceptance.ui import state
from acceptance.ui.common.flash import render_flash, set_flash

KEY_SAVE = "report_save_kit"
KEY_PREVIEW = "report_preview"


def render() -> None:
    """Отрисовывает экран «Отчёт испытаний» (комплект md/json + приложения)."""
    st.title("Отчёт испытаний")
    st.caption(
        "Комплект отчётности сессии: `md` + `json` и приложения (проверки, замечания, "
        "`__TEST__`-сущности, выдержка журнала). Отчёт строится только по данным сессии "
        "и структурного журнала — сервер при формировании не опрашивается."
    )
    render_flash()

    session = state.current_session()
    if session is None:
        st.warning(
            "Сессия испытаний не выбрана: отчёт формируется по данным сессии — "
            "выберите сессию на экране «Сессия испытаний»."
        )
        return

    ready = report_api.readiness(session)
    _render_kpi(ready)
    _render_warnings(ready)

    runtime = state.get_runtime()
    bundle = report_api.build(
        session,
        journal_records=runtime.journal.records if runtime else None,
        journal_text=_session_journal_text(session),
        meta=_meta(session),
    )

    st.divider()
    _render_preview(bundle)
    st.divider()
    _render_downloads(bundle, session)


def _render_kpi(ready: dict) -> None:
    """KPI готовности отчёта: проверки, замечания, задачи, артефакты, сущности."""
    checks = ready["checks"]
    columns = st.columns(6)
    columns[0].metric(
        "Проверки",
        f"{checks['done']} / {checks['implemented']}",
        delta=f"план {checks['program_total']}",
    )
    columns[1].metric("Успех / отказ", f"{checks['passed']} / {checks['failed']}")
    columns[2].metric("Блокировано API", checks["blocked"])
    columns[3].metric("Замечания", ready["notes"]["total"], delta=f"P0 — {ready['notes']['p0']}")
    columns[4].metric(
        "Задачи / артефакты",
        f"{ready['tasks']['observed']} / {ready['artifacts']['total']}",
    )
    columns[5].metric(
        "`__TEST__` не удалено",
        ready["test_entities"]["pending"],
        delta=f"всего {ready['test_entities']['total']}",
    )
    percent = (checks["done"] / checks["implemented"] * 100) if checks["implemented"] else 0.0
    st.progress(
        min(1.0, max(0.0, percent / 100)),
        text=f"Готовность отчёта: {checks['done']} из {checks['implemented']} проверок ({percent:.0f}%)",
    )


def _render_warnings(ready: dict) -> None:
    """Предупреждения «чего не хватает» для подписания отчёта."""
    if not ready["warnings"]:
        st.success("Отчёт полон: предупреждений о полноте нет.")
        return
    with st.expander(f"⚠️ Отчёт неполон: {len(ready['warnings'])} замечание(й)", expanded=True):
        for warning in ready["warnings"]:
            st.markdown(f"* {warning}")


def _render_preview(bundle: report_api.ReportBundle) -> None:
    """Предпросмотр markdown-отчёта (рендер и исходный текст)."""
    st.markdown("**Предпросмотр отчёта**")
    show_source = st.checkbox("Показать исходный markdown", key=KEY_PREVIEW)
    if show_source:
        st.code(bundle.markdown, language="markdown")
    else:
        st.markdown(bundle.markdown)


def _render_downloads(bundle: report_api.ReportBundle, session: TestSession) -> None:
    """Выгрузка комплекта и сохранение его в артефакты сессии."""
    st.markdown("**Комплект отчётности**")
    columns = st.columns(2 + len(bundle.annexes))
    columns[0].download_button(
        "⬇ Отчёт (MD)",
        data=bundle.markdown,
        file_name=f"{bundle.file_stem}.md",
        mime="text/markdown",
        use_container_width=True,
        key="report_download_md",
    )
    columns[1].download_button(
        "⬇ Отчёт (JSON)",
        data=bundle.json_text(),
        file_name=f"{bundle.file_stem}.json",
        mime="application/json",
        use_container_width=True,
        key="report_download_json",
    )
    for index, (name, content) in enumerate(bundle.annexes.items(), start=2):
        columns[index].download_button(
            f"⬇ {name}",
            data=content,
            file_name=f"{bundle.file_stem}_{name}",
            mime="text/csv" if name.endswith(".csv") else "application/x-ndjson",
            use_container_width=True,
            key=f"report_download_{name}",
        )

    if st.button(
        "💾 Сформировать и сохранить комплект в артефакты сессии",
        key=KEY_SAVE,
        type="primary",
        help=(
            "Комплект сохраняется в `acceptance_data/reports` и регистрируется в артефактах "
            "сессии — файлы попадают в отчёт и в комплект передачи."
        ),
    ):
        _save_kit(session)


def _save_kit(session: TestSession) -> None:
    """Сохраняет комплект отчёта на диск и регистрирует файлы в артефактах сессии."""
    runtime = state.get_runtime()
    paths = report_api.save_report(
        session,
        journal_records=runtime.journal.records if runtime else None,
        journal_text=_session_journal_text(session),
        meta=_meta(session),
    )
    for role, path in paths.items():
        add_artifact(
            session,
            kind=f"report:{role}",
            path=path,
            note=f"комплект отчётности сессии {session.session_id}",
        )
    state.store_session(session)
    log_event(
        "report_saved",
        f"Комплект отчёта сохранён: {len(paths)} файл(ов)",
        module="report",
        payload={role: path.as_posix() for role, path in paths.items()},
    )
    set_flash(
        "success",
        "Комплект отчёта сохранён: " + ", ".join(path.name for path in paths.values()),
    )
    st.rerun()


def _session_journal_text(session: TestSession) -> str | None:
    """Текст структурного журнала сессии (JSONL), если он уже создан (FR-T6)."""
    raw = str(session.logs.get("session_log") or "")
    if not raw:
        return None
    try:
        return Path(raw).read_text(encoding="utf-8")
    except OSError:
        return None


def _meta(session: TestSession) -> dict[str, str]:
    """Шапка комплекта: кто, где и когда сформировал отчёт."""
    return {
        "Сессия": session.session_id,
        "Стенд": session.base_url or "—",
        "Сборка сервера": session.server_build,
        "Оператор": session.info.operator_fio or "—",
        "Объект испытаний": session.info.object_of_test or "—",
    }
