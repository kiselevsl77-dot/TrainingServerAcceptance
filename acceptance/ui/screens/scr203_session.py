"""`SCR-203` Сессия испытаний — Подготовка (Ф0, Ф4).

Макет экрана — `docs/16`, раздел «SCR-203. Сессия испытаний»; требования ТЗ: `FR-P-2…FR-P-5`,
`FR-P-52`, `FR-P-55`, `DR-P-1`.

Экран открывает и ведёт сессию: реквизиты (они попадают в шапку отчёта), состояние работы,
история событий и передача сессии на другую машину файлом (`FR-P-52`). Экран **не управляет
прогоном** и не хранит результаты: он даёт контекст, в котором работают остальные экраны.

`CONTENT` ниже — описание экрана по макету (блоки, состояния, переходы, требования): после
наполнения оно остаётся объявленным контрактом экрана (трассировка — этап 4), а состояния
реализованы в `render` и его помощниках.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import httpx
import streamlit as st

from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance import session as session_api
from acceptance.labels import build_label
from acceptance.session import SessionInfo, TestSession
from acceptance.ui import state
from acceptance.ui.components import flash, layout, snapshots, status
from acceptance.ui.components.screen import ScreenContent
from client.errors import ClientError

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr203_session"

#: Поля реквизитов, без которых сессия считается незаполненной (в макете помечены «*»).
REQUIRED_INFO: tuple[str, ...] = ("title", "object_of_test", "program_doc")

#: Подписи полей реквизитов (порядок — как в макете).
INFO_LABELS: dict[str, str] = {
    "title": "Наименование",
    "object_of_test": "Объект испытаний",
    "program_doc": "Программа-методика",
    "customer": "Заказчик",
    "lab": "Организация",
    "commission": "Комиссия",
    "goal": "Цель испытаний",
    "scope": "Объём испытаний",
    "conditions": "Условия",
    "limitations": "Ограничения",
    "criteria": "Критерии приёмки",
    "conclusion": "Заключение",
    "signatures": "Подписи",
    "notes": "Примечания",
}

#: Состояния сессии → что оператор может сделать дальше (жизненный цикл, `FR-P-2…FR-P-5`).
LIFECYCLE_BY_STATUS: dict[str, tuple[str, ...]] = {
    session_api.STATUS_DRAFT: ("start", "close"),
    session_api.STATUS_RUNNING: ("close",),
    session_api.STATUS_CLOSED: ("reopen",),
}

#: Подписи действий жизненного цикла.
ACTION_LABELS: dict[str, str] = {
    "start": "▶ Начать сессию",
    "close": "⏹ Завершить",
    "reopen": "↗ Переоткрыть",
}

#: Сколько событий истории показывать без раскрытия «всей истории».
HISTORY_LIMIT = 20

#: Подпись состояния «нет сессии» (макет `docs/16`, `SCR-203`).
NO_SESSION_HINT = (
    "Сессия не выбрана: создайте сессию, иначе прогон не с чем связать — "
    "протокол ведётся в сессии (`DR-P-1`)"
)


#: Содержимое скелета: что известно об экране из макета до реализации.
CONTENT = ScreenContent(
    purpose="Открыть и вести сессию: реквизиты для шапки отчёта, ход работы, завершение.",
    blocks=(
        "Реквизиты испытаний: объект, сборка, этап, исполнители, программа-методика",
        "Состояние сессии: статус, начало, передача и завершение",
        "История работы сессии: что и когда делалось",
        "Снимок «Начало» при открытии сессии",
    ),
    states=(
        "Нет сессии: создание новой сессии",
        "Черновик: реквизиты заполнены не полностью (готовность в процентах)",
        "Завершена: правки результатов запрещены",
    ),
    transitions=(
        ("SCR-201", "Обзор испытаний"),
        ("SCR-202", "Стенд"),
    ),
    requirements=(
        "FR-P-2…FR-P-5",
        "FR-P-52",
        "FR-P-55",
        "DR-P-1",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def readiness(info: SessionInfo) -> dict[str, Any]:
    """Готовность реквизитов: заполненные и не хватающие поля и процент заполнения.

    «Реквизиты сессии: 90 %» из макета и предупреждение шапки считаются по этому
    словарю, поэтому на экране и в панели контекста одно и то же число.
    """
    filled = [key for key, value in info.to_dict().items() if str(value or "").strip()]
    missing = [key for key in REQUIRED_INFO if not str(getattr(info, key, "") or "").strip()]
    total = len(INFO_LABELS)
    return {
        "filled": filled,
        "missing": missing,
        "percent": round(len(filled) * 100 / total) if total else 0,
        "complete": not missing,
    }


def actions_for(status: str) -> tuple[str, ...]:
    """Действия, возможные в этом состоянии сессии (черновик → «идёт» → «завершена»)."""
    return LIFECYCLE_BY_STATUS.get(str(status), ("start", "close"))


def build_warning(session: TestSession, current_build: str = "") -> str:
    """Сборка в сессии и сборка стенда: расхождение показывается предупреждением.

    Помощник оставлен на экране как понятное имя для вызова `status.build_warning`
    (проверка живёт в компоненте: её использует и `SCR-202`).
    """
    return status.build_warning(session, current_build)


def history_rows(
    session: TestSession,
    *,
    needle: str = "",
    limit: int = HISTORY_LIMIT,
) -> list[dict[str, str]]:
    """История событий сессии строками: свежие сверху, с фильтром по подстроке."""
    text = str(needle or "").strip().lower()
    rows: list[dict[str, str]] = []
    for record in reversed(session.history):
        event = str(record.get("event") or "")
        message = str(record.get("message") or event)
        if text and text not in f"{event} {message}".lower():
            continue
        rows.append({"at": str(record.get("at") or ""), "event": event, "message": message})
    return rows[: max(0, int(limit))]


def snapshot_labels(session: TestSession) -> list[tuple[str, str]]:
    """Снимки стенда сессии: подпись фазы → время (см. `components.snapshots`)."""
    return snapshots.labels(session)


def session_picker_rows(metas: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Строки списка сессий для выбора: идентификатор, статус, наименование, проверки."""
    return [
        {
            "session_id": str(meta.get("session_id") or ""),
            "status": str(meta.get("status") or ""),
            "title": str(meta.get("title") or ""),
            "object_of_test": str(meta.get("object_of_test") or ""),
            "checks_total": int(meta.get("checks_total") or 0),
            "created_at": str(meta.get("created_at") or ""),
        }
        for meta in metas
    ]


def render() -> None:
    """Рисует экран: создание сессии или ведение уже выбранной сессии."""
    session = state.current_session()
    subtitle = f"сессия {session.session_id}" if session is not None else "сессия не выбрана"
    layout.render_header(KEY, subtitle)
    flash.render()

    if session is None:
        _render_start()
        return

    workspace, context = layout.zones()
    with workspace:
        _render_info(session)
        _render_history(session)
        _render_transfer(session)
    with context:
        _render_state(session)


# ---------------------------------------------------------------------------
# Состояние «нет сессии»: создать новую или открыть существующую
# ---------------------------------------------------------------------------
def _render_start() -> None:
    """Состояние «нет сессии» (`IR-P-8`): создать новую или открыть существующую."""
    st.info(NO_SESSION_HINT)
    left, right = st.columns(2)

    with left, st.container(border=True):
        st.subheader("Новая сессия")
        st.caption(
            "Сессия зафиксирует адрес и сборку стенда, реквизиты и весь прогон "
            "(`DR-P-1`); после создания нажмите «▶ Начать сессию»."
        )
        if st.button("Создать сессию", type="primary", key=f"{KEY}_create"):
            _create_session()

    with right, st.container(border=True):
        st.subheader("Открыть существующую")
        metas = session_picker_rows(session_api.list_sessions())
        if not metas:
            st.caption("Файлов сессий нет: в `acceptance_data/sessions/` ничего не найдено.")
            return
        options = {
            f"{row['session_id']} · {row['status']} · {row['title']}": row["session_id"]
            for row in metas
        }
        chosen = st.selectbox("Сессия", list(options), key=f"{KEY}_open_pick")
        st.caption(f"Всего сессий в каталоге: {len(metas)}")
        if st.button("Открыть", key=f"{KEY}_open"):
            _open_session(options[chosen])


def _create_session() -> None:
    """Создаёт черновик сессии: адрес стенда, версия сервера, пути журналов (`FR-T5`)."""
    runtime = state.get_runtime()
    session = session_api.new_session(base_url=runtime.settings.base_url if runtime else "")
    artifacts = state.ensure_logging(state.log_level(), session.session_id)
    session.logs = {
        "app_log": artifacts.app_log.as_posix(),
        "session_log": artifacts.session_log.as_posix(),
    }
    if runtime is not None:
        try:
            session.server_version = runtime.apis.system.version()
        except (ClientError, httpx.HTTPError) as exc:
            st.warning(f"Версия стенда не получена: {exc}")
    state.store_session(session)
    flash.success(
        f"Сессия {session.session_id} создана: заполните реквизиты и нажмите «▶ Начать сессию»"
    )
    st.rerun()


def _open_session(session_id: str) -> None:
    """Открывает существующую сессию и переключает журналирование на неё."""
    try:
        session = session_api.load_session(session_id)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        st.error(f"Сессию {session_id} прочитать не удалось: {exc}")
        return
    state.set_current_session(session)
    state.ensure_logging(state.log_level(), session.session_id)
    flash.success(f"Сессия {session.session_id} открыта · {session.status}")
    st.rerun()


# ---------------------------------------------------------------------------
# Реквизиты, история и передача сессии (рабочая область)
# ---------------------------------------------------------------------------
def _render_info(session: TestSession) -> None:
    """Реквизиты испытаний: они попадают в шапку отчёта (`DR-P-1`)."""
    ready = readiness(session.info)
    st.subheader("Реквизиты испытаний")
    missing = ", ".join(INFO_LABELS[key] for key in ready["missing"])
    st.caption(
        layout.join_parts(
            f"готовность {ready['percent']} %",
            f"не хватает: {missing}" if missing else "обязательные поля заполнены",
            f"снимков стенда: {len(session.snapshots)}",
        )
    )

    with st.form(key=f"{KEY}_info_form", clear_on_submit=False):
        values: dict[str, str] = {}
        first, second = st.columns(2)
        with first:
            values["title"] = st.text_input(INFO_LABELS["title"] + " *", value=session.info.title)
            values["object_of_test"] = st.text_input(
                INFO_LABELS["object_of_test"] + " *", value=session.info.object_of_test
            )
            values["program_doc"] = st.text_input(
                INFO_LABELS["program_doc"] + " *", value=session.info.program_doc
            )
        with second:
            values["customer"] = st.text_input(INFO_LABELS["customer"], value=session.info.customer)
            values["lab"] = st.text_input(INFO_LABELS["lab"], value=session.info.lab)
            values["commission"] = st.text_input(
                INFO_LABELS["commission"], value=session.info.commission
            )
        values["criteria"] = st.text_area(
            INFO_LABELS["criteria"], value=session.info.criteria, height=80
        )
        with st.expander("Цель, объём, условия, ограничения, подписи"):
            for field in ("goal", "scope", "conditions", "limitations", "signatures", "notes"):
                values[field] = st.text_area(
                    INFO_LABELS[field],
                    value=str(getattr(session.info, field) or ""),
                    height=70,
                    key=f"{KEY}_info_{field}",
                )
        saved = st.form_submit_button("Сохранить реквизиты", type="primary")

    if not saved:
        return
    changed = [key for key, value in values.items() if value != getattr(session.info, key)]
    if not changed:
        flash.info("Реквизиты не изменились: сохранять нечего")
        st.rerun()
    for key, value in values.items():
        setattr(session.info, key, value)
    session.add_history(
        "session_info_saved",
        "Реквизиты сессии сохранены",
        fields=", ".join(changed),
        author=session.info.operator_fio,
    )
    state.store_session(session)
    flash.success("Реквизиты сохранены: " + ", ".join(INFO_LABELS[key] for key in changed))
    st.rerun()


def _render_history(session: TestSession) -> None:
    """История работы сессии: что и когда делалось (`FR-P-55`)."""
    st.subheader("История событий")
    needle = st.text_input(
        "Фильтр по событиям",
        key=f"{KEY}_history_needle",
        placeholder="например: снимок, программа, прогон",
    )
    rows = [
        {**row, "at": layout.short_time(row["at"])} for row in history_rows(session, needle=needle)
    ]
    st.caption(f"показано {len(rows)} из {len(session.history)} событий (свежие сверху)")
    if not rows:
        st.caption("Событий по фильтру нет.")
        return
    layout.rows_table(
        rows,
        key=f"{KEY}_history",
        columns={"at": "Время", "event": "Событие", "message": "Что сделано"},
        height=280,
    )


def _render_transfer(session: TestSession) -> None:
    """Передача сессии: выгрузить файл и загрузить его на другой машине (`FR-P-52`)."""
    with st.expander("Передача сессии (выгрузка и загрузка файла сессии)"):
        st.caption(
            "Файл сессии содержит реквизиты, программу, очередь, результаты, замечания "
            "и историю: переданный файл восстанавливает сессию на другой машине."
        )
        left, right = st.columns(2)
        with left:
            st.download_button(
                "⬇ Выгрузить сессию",
                data=session_api.session_json(session),
                file_name=f"{session.session_id}.json",
                mime="application/json",
                key=f"{KEY}_download",
                use_container_width=True,
            )
            st.caption(
                layout.join_parts(
                    session.logs.get("session_log", ""),
                    session.logs.get("app_log", ""),
                )
            )
        with right:
            uploaded = st.file_uploader("⬆ Загрузить сессию", type=["json"], key=f"{KEY}_upload")
            if uploaded is not None and st.button(
                "Заменить текущую сессию", key=f"{KEY}_upload_apply"
            ):
                _apply_uploaded(uploaded.getvalue())


def _apply_uploaded(payload: bytes) -> None:
    """Загружает сессию из файла и делает её текущей (`FR-P-52`)."""
    try:
        loaded = session_api.load_session_from_text(payload.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        st.error(f"Файл сессии прочитать не удалось: {exc}")
        return
    state.store_session(loaded)
    state.ensure_logging(state.log_level(), loaded.session_id)
    flash.success(f"Сессия {loaded.session_id} загружена из файла · {loaded.status}")
    st.rerun()


# ---------------------------------------------------------------------------
# Панель контекста: состояние, снимки стенда и жизненный цикл сессии
# ---------------------------------------------------------------------------
def _render_state(session: TestSession) -> None:
    """Панель контекста: состояние сессии, снимки стенда и действия (макет `SCR-203`)."""
    queue = queue_api.load_queue(session)
    summary = results_api.summary(session)
    checks_total = queue.size or int(summary["total"])
    stand = state.stand_status() or {}
    warning = build_warning(session, build_label(stand.get("version")) if stand else "")

    st.subheader("Состояние")
    st.caption(
        layout.join_parts(
            f"Статус: {session.status}",
            f"Начало: {layout.short_time(session.started_at) or '—'}",
            f"Окончание: {layout.short_time(session.ended_at) or '—'}",
        )
    )
    st.caption(
        layout.join_parts(
            f"Обменов в этом запуске пульта: {len(state.journal_records())}",
            f"Проверок: {summary['done']} из {checks_total}" if checks_total else "Проверок: 0",
            f"Снимков: {len(session.snapshots)}",
            f"Реквизиты: {readiness(session.info)['percent']} %",
        )
    )
    if session.server_build:
        st.caption(f"Сборка в сессии: {session.server_build}")
    if warning:
        st.warning(warning)

    st.divider()
    st.markdown("**Снимки стенда**")
    snapshots.controls(session, key=KEY)
    _lifecycle_controls(session)


def _lifecycle_controls(session: TestSession) -> None:
    """Действия жизненного цикла: начать, завершить (с заключением), переоткрыть."""
    st.divider()
    actions = actions_for(session.status)
    conclusion = ""
    if "close" in actions:
        conclusion = st.selectbox("Заключение", session_api.CONCLUSIONS, key=f"{KEY}_conclusion")
    for action in actions:
        if st.button(
            ACTION_LABELS[action],
            key=f"{KEY}_action_{action}",
            use_container_width=True,
            type="primary" if action == "close" else "secondary",
        ):
            _apply_action(session, action, conclusion=conclusion)
    if st.button("→ К прогону (SCR-301)", key=f"{KEY}_goto_run", use_container_width=True):
        state.go_to("scr301_run")


def _apply_action(session: TestSession, action: str, *, conclusion: str = "") -> None:
    """Выполняет действие жизненного цикла сессии и сохраняет её (`FR-P-2…FR-P-5`)."""
    if action == "start":
        session_api.start_session(session)
    elif action == "close":
        session_api.close_session(session, conclusion=conclusion or None)
    elif action == "reopen":
        session_api.reopen_session(session)
    else:
        st.error(f"Неизвестное действие сессии: {action}")
        return
    state.store_session(session)
    flash.success(f"{ACTION_LABELS[action]} — сессия: {session.status}")
    st.rerun()
