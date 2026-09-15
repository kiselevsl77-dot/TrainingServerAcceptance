"""Экран «Сессия испытаний» (FR-T5).

Сессия фиксирует: кто (оператор, организация, комиссия), когда (начало/окончание
и длительность), что (наименование, объект испытаний — сборка сервера, программа-
методика, цель, объём, условия, критерии) и с каким результатом (проверки,
замечания к API, итоговое решение). Все поля попадают в отчёт об испытаниях.

Сессия хранится файлом `acceptance_data/sessions/<id>.json` и переживает
перезапуск пульта; файл можно выгрузить и передать другому испытателю.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from acceptance.logging_setup import log_event
from acceptance.session import (
    CONCLUSIONS,
    STATUS_CLOSED,
    STATUS_DRAFT,
    STATUS_RUNNING,
    TestSession,
    close_session,
    delete_session,
    list_sessions,
    load_session,
    load_session_from_text,
    new_session,
    now_iso,
    reopen_session,
    session_json,
    start_session,
)
from acceptance.ui import state
from acceptance.ui.common.labels import build_label

SHORT_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "Наименование испытаний*"),
    ("program_doc", "Программа/методика испытаний (реквизиты)"),
    ("object_of_test", "Объект испытаний (сборка, ревизия)*"),
    ("customer", "Заказчик"),
    ("lab", "Испытательная организация / подразделение"),
    ("operator_fio", "Оператор (ФИО)"),
    ("operator_position", "Должность оператора"),
    ("criteria", "Критерии оценки (годен / годен с замечаниями / не годен)"),
)

LONG_FIELDS: tuple[tuple[str, str], ...] = (
    ("commission", "Состав комиссии (ФИО, должности)"),
    ("goal", "Цель испытаний"),
    ("scope", "Объём испытаний (перечень проверок)"),
    ("conditions", "Условия испытаний (стенд, сеть, окружение)"),
    ("limitations", "Ограничения и допущения"),
    ("notes", "Дополнительные сведения"),
    ("signatures", "Подписи (ФИО, дата)"),
)

REQUIRED_FIELDS = ("title", "object_of_test")
RECENT_SESSIONS = 10


def render() -> None:
    """Отрисовывает экран «Сессия испытаний»."""
    st.title("Сессия испытаний")
    st.caption(
        "FR-T5 · фиксация начала, хода и результатов испытаний: кто, когда, на какой сборке, "
        "что проверялось и с каким результатом. Сессия сохраняется в `acceptance_data/sessions/`."
    )

    session = state.current_session()
    if session is None:
        _render_new_session()
    else:
        _render_current_session(session)

    st.divider()
    _render_sessions_list(session)


# ---------------------------------------------------------------------------
# Новая сессия
# ---------------------------------------------------------------------------
def _render_new_session() -> None:
    """Форма создания сессии испытаний."""
    runtime = state.get_runtime()
    st.subheader("Новая сессия")

    if runtime is None:
        st.error(
            "Base URL испытуемого сервера не задан — сессию создать нельзя. "
            "Укажите `TRAINING_SERVER_BASE_URL` в `.env` и перезапустите пульт."
        )
        return

    stand = state.stand_status() or {}
    server_version = stand.get("version") if isinstance(stand.get("version"), dict) else {}
    build = build_label(server_version)
    st.caption(f"Сервер: {runtime.settings.base_url} · сборка: {build} · время пульта: {now_iso()}")

    with st.form("session_new"):
        title = st.text_input("Наименование испытаний*", key="new_session_title")
        object_of_test = st.text_input("Объект испытаний (сборка, ревизия)*", value=build)
        operator_fio = st.text_input("Оператор (ФИО)", key="new_session_operator")
        lab = st.text_input("Испытательная организация / подразделение", key="new_session_lab")
        goal = st.text_area("Цель испытаний", height=80, key="new_session_goal")
        submitted = st.form_submit_button("▶ Начать сессию", type="primary")

    if not submitted:
        st.info(
            "Перед созданием сессии проверьте стенд на экране «Стенд»: снимок реестров "
            "на начало сессии попадёт в отчёт."
        )
        return

    if not title.strip() or not object_of_test.strip():
        st.error("Заполните обязательные поля: «Наименование испытаний» и «Объект испытаний».")
        return

    session = new_session(base_url=runtime.settings.base_url, server_version=server_version)
    session.info.title = title.strip()
    session.info.object_of_test = object_of_test.strip()
    session.info.operator_fio = operator_fio.strip()
    session.info.lab = lab.strip()
    session.info.goal = goal.strip()
    start_session(session)

    logs = state.ensure_logging(state.log_level(), session.session_id)
    session.logs = {"app_log": logs.app_log.as_posix(), "session_log": logs.session_log.as_posix()}
    state.store_session(session)

    log_event(
        "session_created",
        "Сессия испытаний создана",
        module="session",
        payload={
            "session_id": session.session_id,
            "base_url": session.base_url,
            "server_build": session.server_build,
            "operator": session.info.operator_fio,
            "session_log": session.logs.get("session_log"),
        },
    )
    st.rerun()


# ---------------------------------------------------------------------------
# Текущая сессия
# ---------------------------------------------------------------------------
def _render_current_session(session: TestSession) -> None:
    """Панель текущей сессии: состояние, поля, действия, история."""
    icons = {STATUS_DRAFT: "⚪", STATUS_RUNNING: "🟢", STATUS_CLOSED: "✅"}
    st.subheader(
        f"{icons.get(session.status, '⚪')} Сессия {session.session_id} · {session.status}"
    )

    col_build, col_start, col_end, col_duration = st.columns(4)
    col_build.metric("Сборка сервера", session.server_build)
    col_start.metric("Начало", session.started_at or "—")
    col_end.metric("Окончание", session.ended_at or "—")
    duration = session.duration_seconds
    col_duration.metric("Длительность, с", f"{duration:.0f}" if duration is not None else "—")

    st.caption(
        f"Base URL: {session.base_url} · проверок в сессии: {len(session.checks)} · "
        f"замечаний к API: {len(session.notes)} · событий истории: {len(session.history)} · "
        f"структурный журнал: {session.logs.get('session_log', '—')}"
    )
    if not session.info.is_filled:
        st.warning(
            "Не заполнено обязательное ядро сессии (наименование и объект испытаний) — "
            "отчёт об испытаниях будет неполным."
        )

    _render_info_form(session)
    _render_actions(session)
    _render_history(session)


def _render_info_form(session: TestSession) -> None:
    """Поля сессии, заполняемые испытателем (входят в шапку отчёта)."""
    with st.form("session_info"):
        st.markdown("**Поля сессии (входят в отчёт)**")
        col_left, col_right = st.columns(2)
        values: dict[str, str] = {}
        for index, (field, label) in enumerate(SHORT_FIELDS):
            column = col_left if index % 2 == 0 else col_right
            values[field] = column.text_input(
                label,
                value=getattr(session.info, field),
                key=f"info_{session.session_id}_{field}",
            )
        for field, label in LONG_FIELDS:
            values[field] = st.text_area(
                label,
                value=getattr(session.info, field),
                height=80,
                key=f"info_{session.session_id}_{field}",
            )
        submitted = st.form_submit_button("💾 Сохранить поля сессии")

    if not submitted:
        return

    for field, value in values.items():
        setattr(session.info, field, value.strip())
    state.store_session(session)
    log_event(
        "session_info_saved",
        "Поля сессии сохранены",
        module="session",
        payload={
            "session_id": session.session_id,
            "title": session.info.title,
            "object_of_test": session.info.object_of_test,
        },
    )
    st.success("Поля сессии сохранены.")


def _render_actions(session: TestSession) -> None:
    """Действия сессии: старт, завершение, переоткрытие, выгрузка, удаление."""
    col_start, col_close, col_download = st.columns(3)

    if session.status == STATUS_DRAFT and col_start.button(
        "▶ Стартовать сессию", use_container_width=True, key=f"start_{session.session_id}"
    ):
        start_session(session)
        state.store_session(session)
        log_event(
            "session_started",
            "Сессия начата",
            module="session",
            payload={"session_id": session.session_id},
        )
        st.rerun()

    if session.status == STATUS_CLOSED and col_start.button(
        "↩ Переоткрыть сессию", use_container_width=True, key=f"reopen_{session.session_id}"
    ):
        reopen_session(session)
        state.store_session(session)
        log_event(
            "session_reopened",
            "Сессия переоткрыта",
            module="session",
            payload={"session_id": session.session_id},
        )
        st.rerun()

    if session.is_open:
        with col_close.popover("⏹ Завершить сессию", use_container_width=True):
            conclusion = st.selectbox(
                "Итоговое решение",
                CONCLUSIONS,
                key=f"close_{session.session_id}",
            )
            note = st.text_area(
                "Дополнительно к решению",
                height=60,
                key=f"close_note_{session.session_id}",
            )
            if st.button("Завершить", type="primary", key=f"close_btn_{session.session_id}"):
                close_session(session, conclusion=conclusion)
                if note.strip():
                    session.info.notes = f"{session.info.notes}\n{note.strip()}".strip()
                state.store_session(session)
                log_event(
                    "session_closed",
                    "Сессия завершена",
                    module="session",
                    payload={
                        "session_id": session.session_id,
                        "conclusion": session.info.conclusion,
                        "duration_seconds": session.duration_seconds,
                    },
                )
                st.rerun()

    col_download.download_button(
        "⬇ Файл сессии (JSON)",
        data=session_json(session),
        file_name=f"{session.session_id}.json",
        mime="application/json",
        use_container_width=True,
        key=f"download_{session.session_id}",
    )

    with st.expander("Удалить сессию из пульта"):
        confirm = st.checkbox(
            "Подтверждаю удаление файла сессии",
            key=f"delete_confirm_{session.session_id}",
        )
        if st.button(
            "🗑 Удалить сессию",
            disabled=not confirm,
            key=f"delete_btn_{session.session_id}",
        ):
            deleted = delete_session(session.session_id)
            state.clear_current_session()
            log_event(
                "session_deleted",
                "Файл сессии удалён",
                level="WARNING",
                module="session",
                payload={"session_id": session.session_id, "deleted": deleted},
            )
            st.rerun()


def _render_history(session: TestSession) -> None:
    """История событий сессии (основа раздела отчёта «Ход испытаний»)."""
    with st.expander(f"История сессии ({len(session.history)} событий)"):
        if not session.history:
            st.info("Событий пока нет.")
            return
        rows: list[dict[str, Any]] = [
            {"Время": item.get("at"), "Событие": item.get("event"), "Описание": item.get("message")}
            for item in reversed(session.history[-50:])
        ]
        st.table(rows)
        st.caption("Показаны последние 50 событий; полный список — в файле сессии (JSON).")


# ---------------------------------------------------------------------------
# Список сессий и перенос сессии между пультами
# ---------------------------------------------------------------------------
def _render_sessions_list(current: TestSession | None) -> None:
    """Список сохранённых сессий: открытие, снятие с просмотра, импорт файла."""
    st.subheader("Сохранённые сессии")
    metas = list_sessions()
    if not metas:
        st.info(
            "Сохранённых сессий нет. Созданная сессия автоматически сохраняется "
            "в `acceptance_data/sessions/`."
        )
        return

    st.table(
        [
            {
                "Сессия": meta["session_id"],
                "Статус": meta["status"],
                "Наименование": meta["title"],
                "Объект испытаний": meta["object_of_test"],
                "Сборка": meta["server_build"],
                "Начало": meta["started_at"] or "—",
                "Окончание": meta["ended_at"] or "—",
                "Проверок": meta["checks_total"],
                "Замечаний": meta["notes_total"],
                "Решение": meta["conclusion"],
            }
            for meta in metas[:RECENT_SESSIONS]
        ]
    )
    if len(metas) > RECENT_SESSIONS:
        st.caption(f"Показаны {RECENT_SESSIONS} последних сессий из {len(metas)}.")

    options = {f"{meta['session_id']} · {meta['title']} · {meta['status']}": meta for meta in metas}
    chosen = st.selectbox("Сессия", list(options), key="session_open_choice")

    col_open, col_detach, col_upload, col_import = st.columns([1, 1, 2, 1])
    if col_open.button("📂 Открыть", use_container_width=True, key="session_open_btn"):
        _open_session(str(options[chosen]["session_id"]))

    if current is not None and col_detach.button(
        "⏏ Снять с просмотра", use_container_width=True, key="session_detach_btn"
    ):
        log_event(
            "session_detached",
            "Сессия снята с просмотра",
            module="session",
            payload={"session_id": current.session_id},
        )
        state.clear_current_session()
        st.rerun()

    uploaded = col_upload.file_uploader(
        "Файл сессии (JSON) — передача между пультами",
        type=["json"],
        key="session_upload",
    )
    if col_import.button("📥 Загрузить", use_container_width=True, key="session_import_btn"):
        _import_session(uploaded)


def _open_session(session_id: str) -> None:
    """Открывает сохранённую сессию как текущую и переводит журнал на её файл."""
    try:
        session = load_session(session_id)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        st.error(f"Не удалось открыть сессию {session_id}: {exc}")
        return

    state.set_current_session(session)
    state.ensure_logging(state.log_level(), session.session_id)
    log_event(
        "session_opened",
        "Сессия открыта",
        module="session",
        payload={
            "session_id": session.session_id,
            "status": session.status,
            "checks": len(session.checks),
            "notes": len(session.notes),
        },
    )
    st.rerun()


def _import_session(uploaded: Any) -> None:
    """Загружает сессию из JSON-файла (перенос испытаний между пультами)."""
    if uploaded is None:
        st.warning("Выберите файл сессии (JSON).")
        return

    try:
        session = load_session_from_text(uploaded.getvalue().decode("utf-8"))
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        st.error(f"Не удалось прочитать файл сессии: {exc}")
        return

    if not session.session_id:
        st.error("В файле нет идентификатора сессии — это не файл сессии пульта.")
        return

    state.store_session(session)
    state.ensure_logging(state.log_level(), session.session_id)
    log_event(
        "session_imported",
        "Сессия загружена из файла",
        module="session",
        payload={
            "session_id": session.session_id,
            "status": session.status,
            "checks": len(session.checks),
        },
    )
    st.success(f"Сессия {session.session_id} загружена и сохранена в пульте.")
    st.rerun()
