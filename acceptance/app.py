"""Точка входа пульта испытаний сервера обучения (целевой каркас, этап 2 big bang).

Запуск: `streamlit run acceptance/app.py` из корня репозитория
(или `run_pult.bat` / `./run_pult.sh`).

Каркас: 15 целевых экранов (`SCR-101`…`SCR-501`) в пяти группах навигации и раскладка макета
(`docs/16` §0) — навигация слева, рабочая область и панель контекста в центре. Наполнение
экранов данными — этап 3; до него каждый экран показывает скелет (назначение, блоки,
состояния, переходы, требования) и вызовов стенда не делает.

Пульт работает против **текущего** API испытуемого сервера без его доработки: всё, что не
удалось проверить из-за дефектов API, фиксируется как результат испытаний (статус
«блокировано API» + замечание в отчёте).
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

from acceptance import glossary, programme  # noqa: E402
from acceptance.config import LOG_LEVELS  # noqa: E402
from acceptance.logging_setup import log_event  # noqa: E402
from acceptance.session import TestSession  # noqa: E402
from acceptance.ui import nav, state  # noqa: E402
from acceptance.ui.screens import RENDERERS  # noqa: E402

st.set_page_config(page_title="Пульт испытаний · сервер обучения", page_icon="🧪", layout="wide")

#: Экраны пульта: ключ маршрута → (подпись кнопки навигации, отрисовка экрана).
#: Подписи берутся из глоссария (`acceptance/glossary.py`), поэтому экраны называются
#: одинаково в интерфейсе, документации и отчёте, а сущности стенда — с префиксом `$`.
SCREENS: dict[str, tuple[str, Callable[[], None]]] = {
    key: (glossary.nav_label(key), renderer) for key, renderer in RENDERERS.items()
}

#: Порядок навигации — часть бизнес-процесса испытаний (`docs/14` §4): данные вынесены в
#: `acceptance/ui/nav.py` (без Streamlit), поэтому порядок проверяется unit-тестом.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = nav.GROUPS

#: Навигация обязана перечислять все экраны ровно один раз: экран, забытый в реестре
#: (`acceptance/ui/screens/__init__.py`), сразу даёт понятную ошибку вместо «молча
#: невидимой» страницы.
nav.validate(SCREENS)

SCREEN_KEY = state.KEY_SCREEN

if SCREEN_KEY not in st.session_state:
    st.session_state[SCREEN_KEY] = nav.DEFAULT_SCREEN


def _programme_caption(session: TestSession) -> str:
    """Подпись программы в шапке: ревизия, состояние и объём (`SCR-102`)."""
    current = programme.load_programme(session)
    if not current.size:
        return "Программа: не собрана"
    state_label = "утв." if current.is_approved else current.status
    return f"Программа: ревизия {current.revision} · {state_label} · пунктов {current.size}"


def _render_sidebar() -> None:
    """Шапка пульта: стенд, сессия, программа, уровень журнала и навигация (пять групп)."""
    current = state.current_session()
    runtime = state.get_runtime()

    st.sidebar.markdown("### 🧪 Пульт испытаний")
    if runtime is None:
        st.sidebar.error("Base URL не задан (`TRAINING_SERVER_BASE_URL`).")
    else:
        st.sidebar.caption(f"Стенд: {runtime.settings.base_url}")

    if current is None:
        st.sidebar.warning("Сессия испытаний не выбрана")
    else:
        st.sidebar.caption(f"Сессия: {current.session_id} · {current.status}")
        st.sidebar.caption(f"Сборка: {current.server_build}")
        st.sidebar.caption(_programme_caption(current))

    st.sidebar.caption(glossary.PREFIX_HINT)

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
        st.sidebar.markdown(f"**{group_label}**")
        for key in keys:
            label = SCREENS[key][0]
            active = st.session_state[SCREEN_KEY] == key
            if st.sidebar.button(
                label,
                key=f"nav_{key}",
                type="primary" if active else "secondary",
                use_container_width=True,
                help=f"{nav.by_key(key).code} · фаза {nav.by_key(key).phase}",
            ):
                st.session_state[SCREEN_KEY] = key
                st.rerun()

    st.sidebar.divider()
    st.sidebar.caption("Каркас 15 целевых экранов (`SCR-101`…`SCR-501`) · этап 2 big bang")


artifacts = state.ensure_logging(state.log_level(), state.current_session_id())
log_event(
    "pult_started",
    "Пульт испытаний запущен",
    module="app",
    payload={
        "screen": st.session_state[SCREEN_KEY],
        "screen_code": nav.by_key(st.session_state[SCREEN_KEY]).code,
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
