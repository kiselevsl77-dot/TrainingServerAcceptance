"""Навигация по страницам списков (NFR-4).

Используется экранами «Файлы / Субдатасеты» (клиентская пагинация: сервер не
поддерживает `limit`/`offset` для `GET /api/data/files`) и «Нагрузки»
(серверная пагинация: `GET /api/loads/list` возвращает `limit`/`offset`/`result_size`).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from lib.pagination import DEFAULT_PER_PAGE, PER_PAGE_OPTIONS, Page


def render_pagination(state_key: str, page: Page[Any]) -> None:
    """Отрисовывает кнопки «назад/вперёд», подпись и выбор размера страницы.

    Args:
        state_key: ключ `session_state` с номером текущей страницы; рядом
            хранится размер страницы (`{state_key}_size`).
        page: текущая страница (`lib.pagination.Page`).
    """
    col_prev, col_info, col_next, col_size = st.columns([1, 3, 1, 1])
    per_page = st.session_state.get(f"{state_key}_size", DEFAULT_PER_PAGE)

    if col_prev.button(
        "‹ Назад",
        key=f"{state_key}_prev",
        disabled=not page.has_prev,
        use_container_width=True,
    ):
        st.session_state[state_key] = max(1, page.page - 1)
        st.rerun()

    col_info.caption(page.label)

    if col_next.button(
        "Вперёд ›",
        key=f"{state_key}_next",
        disabled=not page.has_next,
        use_container_width=True,
    ):
        st.session_state[state_key] = page.page + 1
        st.rerun()

    col_size.selectbox(
        "На странице",
        PER_PAGE_OPTIONS,
        index=PER_PAGE_OPTIONS.index(per_page) if per_page in PER_PAGE_OPTIONS else 1,
        key=f"{state_key}_size",
        label_visibility="collapsed",
    )
