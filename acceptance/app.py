"""Точка входа пульта испытаний сервера обучения (временный UI для приёмки).

Запуск: `streamlit run acceptance/app.py` из корня репозитория
(или `run_pult.bat` / `./run_pult.sh`).

Пульт работает против **текущего** API испытуемого сервера без его доработки:
всё, что не удалось проверить из-за дефектов API, фиксируется как результат
испытаний (статус «блокировано API» + замечание в отчёте).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import streamlit as st

# Корень репозитория — в sys.path, чтобы импорты client/, lib/, acceptance/
# работали независимо от рабочей директории запуска.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from acceptance.config import LOG_LEVELS  # noqa: E402
from acceptance.logging_setup import log_event  # noqa: E402
from acceptance.ui import state  # noqa: E402
from acceptance.ui.pages import (  # noqa: E402
    checks,
    console,
    logs,
    records,
    report,
    session,
    stand,
)

st.set_page_config(page_title="Пульт испытаний · сервер обучения", page_icon="🧪", layout="wide")

# Экраны пульта: FR-T1…FR-T9.
SCREENS: dict[str, tuple[str, Callable[[], None]]] = {
    "stand": ("🖥️ Стенд", stand.render),
    "session": ("🧪 Сессия испытаний", session.render),
    "records": ("🧩 Записи и субдатасеты", records.render),
    "checks": ("✅ Чек-лист проверок", checks.render),
    "console": ("📡 Консоль запросов", console.render),
    "logs": ("🧾 Журнал", logs.render),
    "report": ("📄 Отчёт испытаний", report.render),
}

GROUPS: list[tuple[str, list[str]]] = [
    ("", ["stand", "session"]),
    ("Испытания", ["records", "checks", "console"]),
    ("Результаты", ["logs", "report"]),
]

SCREEN_KEY = "pult_screen"

if SCREEN_KEY not in st.session_state:
    st.session_state[SCREEN_KEY] = "stand"


def _render_sidebar() -> None:
    """Шапка пульта: сервер, текущая сессия, уровень журнала, навигация."""
    current = state.current_session()
    runtime = state.get_runtime()

    st.sidebar.markdown("### 🧪 Пульт испытаний")
    if runtime is None:
        st.sidebar.error("Base URL не задан (`TRAINING_SERVER_BASE_URL`).")
    else:
        st.sidebar.caption(f"Сервер: {runtime.settings.base_url}")

    if current is None:
        st.sidebar.warning("Сессия испытаний не выбрана")
    else:
        st.sidebar.caption(f"Сессия: {current.session_id} · {current.status}")
        st.sidebar.caption(f"Сборка: {current.server_build}")

    level = state.log_level()
    chosen = st.sidebar.selectbox(
        "Уровень журнала",
        LOG_LEVELS,
        index=LOG_LEVELS.index(level) if level in LOG_LEVELS else 1,
        key="pult_log_level_select",
    )
    if chosen != level:
        state.set_log_level(chosen)
        st.rerun()

    st.sidebar.divider()
    for group_label, keys in GROUPS:
        if group_label:
            st.sidebar.markdown(f"**{group_label}**")
        for key in keys:
            label = SCREENS[key][0]
            active = st.session_state[SCREEN_KEY] == key
            if st.sidebar.button(
                label,
                key=f"nav_{key}",
                type="primary" if active else "secondary",
                use_container_width=True,
            ):
                st.session_state[SCREEN_KEY] = key
                st.rerun()

    st.sidebar.divider()
    st.sidebar.caption("Временный UI для испытаний текущего API · этап T0")


artifacts = state.ensure_logging(state.log_level(), state.current_session_id())
log_event(
    "pult_started",
    "Пульт испытаний запущен",
    module="app",
    payload={
        "screen": st.session_state[SCREEN_KEY],
        "session_id": state.current_session_id(),
        "log_level": artifacts.level,
        "app_log": artifacts.app_log.as_posix(),
        "session_log": artifacts.session_log.as_posix(),
    },
)
log_event(
    "app_run",
    "Перерисовка экрана пульта",
    level="DEBUG",
    module="app",
    payload={"screen": st.session_state[SCREEN_KEY]},
)

_render_sidebar()
SCREENS[st.session_state[SCREEN_KEY]][1]()
