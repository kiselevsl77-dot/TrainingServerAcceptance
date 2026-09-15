"""Отложенные сообщения UI (показываются после `st.rerun`).

Streamlit не сохраняет вывод после перезапуска скрипта, поэтому результат
действий (удаление файла, загрузка) показывается в следующем прогоне:
действие кладёт сообщение через `set_flash`, а `render_flash` выводит его
один раз в начале отрисовки экрана.
"""

from __future__ import annotations

import streamlit as st

FLASH_KEY = "ui_flash"


def set_flash(level: str, message: str) -> None:
    """Запоминает сообщение для показа после ближайшего rerun."""
    st.session_state[FLASH_KEY] = (level, message)


def render_flash() -> None:
    """Показывает и сбрасывает отложенное сообщение."""
    flash = st.session_state.pop(FLASH_KEY, None)
    if not flash:
        return

    level, message = flash
    renderers = {"success": st.success, "warning": st.warning, "error": st.error}
    renderers.get(level, st.info)(message)
