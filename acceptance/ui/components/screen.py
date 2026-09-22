"""Каркас экрана: трёхзонная раскладка и содержимое скелета (этап 2).

Макет `docs/16` §0 задаёт три зоны: слева навигация (её рисует `acceptance/app.py`),
в центре рабочая область, справа панель контекста (выбранный объект, действия, свежие обмены).
До наполнения экранов (этап 3) зоны рисуются каркасно, но сам каркас — уже настоящий:
экраны этапа 3 заменяют содержимое зон, а не переделывают раскладку.

Состояния экрана тоже часть макета (`IR-P-8`): «пусто», «загрузка», «ошибка», «нет сессии» —
скелет перечисляет их списком, чтобы на этапе 3 каждое было реализовано и проверено.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from acceptance import glossary
from acceptance.ui import nav

#: Ширина зон: рабочая область против панели контекста (макет `docs/16` §0).
WORKSPACE_RATIO = (3, 1)

#: Текст бейджа: пульт переходит на новый каркас поэтапно, и это видно на экране.
STAGE_BADGE = "Каркас этапа 2: экран ещё не наполнен данными (наполнение — этап 3 по `docs/16`)"


@dataclass(frozen=True)
class ScreenContent:
    """Содержимое скелета: что известно об экране из макета до его реализации."""

    purpose: str
    blocks: tuple[str, ...]
    states: tuple[str, ...]
    transitions: tuple[tuple[str, str], ...]
    requirements: tuple[str, ...]


def render_screen(key: str, content: ScreenContent) -> None:
    """Рисует каркас экрана: шапку, три зоны, состояния и переходы.

    Args:
        key: ключ маршрута экрана (`nav.SCREENS`), задаёт код, группу и фазу.
        content: содержимое скелета, перенесённое из макета `docs/16`.
    """
    spec = nav.by_key(key)
    st.title(glossary.screen_label(spec.key))
    st.caption(f"{spec.code} · группа «{spec.group}» · фаза {spec.phase}")
    st.info(STAGE_BADGE)
    st.markdown(content.purpose)
    st.divider()

    workspace, context = st.columns(list(WORKSPACE_RATIO), gap="large")
    with workspace, st.container(border=True):
        st.subheader("Рабочая область")
        st.caption("Блоки экрана по макету (наполнение — этап 3):")
        for block in content.blocks:
            st.markdown(f"- {block}")
    with context, st.container(border=True):
        st.subheader("Панель контекста")
        st.caption("Состояния экрана (`IR-P-8`):")
        for state in content.states:
            st.markdown(f"- {state}")
        st.caption("Переходы:")
        for code, title in content.transitions:
            st.markdown(f"- {code} «{title}»")
        st.caption("Требования ТЗ: " + ", ".join(f"`{item}`" for item in content.requirements))
