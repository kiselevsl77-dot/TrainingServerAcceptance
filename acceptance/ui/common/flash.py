"""Отложенные сообщения UI (показываются после `st.rerun`).

Streamlit не сохраняет вывод после перезапуска скрипта, поэтому результат
действий (удаление файла, загрузка) показывается в следующем прогоне:
действие кладёт сообщение через `set_flash`, а `render_flash` выводит его
один раз в начале отрисовки экрана.

Если внутри прогона экран перерисовывается сам (экран «Задачи» догружает статусы
батчами и вызывает `st.rerun`), показанное сообщение вернуло бы промежуточное
состояние: `repeat_flash` возвращает его в очередь, и оператор всё равно увидит
результат своего действия.
"""

from __future__ import annotations

import streamlit as st

FLASH_KEY = "ui_flash"
SHOWN_KEY = "ui_flash_shown"


def set_flash(level: str, message: str) -> None:
    """Запоминает сообщение для показа после ближайшего rerun."""
    st.session_state[FLASH_KEY] = (level, message)


def render_flash() -> None:
    """Показывает и сбрасывает отложенное сообщение (запоминая его для повтора)."""
    flash = st.session_state.pop(FLASH_KEY, None)
    if not flash:
        return

    st.session_state[SHOWN_KEY] = flash
    level, message = flash
    renderers = {"success": st.success, "warning": st.warning, "error": st.error}
    renderers.get(level, st.info)(message)


def repeat_flash() -> None:
    """Возвращает показанное в этом прогоне сообщение в очередь для следующей отрисовки."""
    flash = st.session_state.get(SHOWN_KEY)
    if flash:
        st.session_state[FLASH_KEY] = flash
