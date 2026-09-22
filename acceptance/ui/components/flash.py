"""Короткие сообщения экрана: результат действия виден после перерисовки (`FR-T9`).

Streamlit перерисовывает страницу после каждого действия, поэтому сообщение «набор
сохранён» нельзя показать сразу в том же проходе: оно складывается в состояние
сессии и выводится на следующей перерисовке (`render`). Правило одно: **экран не
молчит** — любое действие либо меняет данные, либо сообщает, почему не получилось.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

#: Ключ состояния сессии, где ждут показа сообщения.
KEY_FLASH = "pult_flash"

#: Вид сообщения → функция Streamlit, которой оно показывается.
KINDS: dict[str, str] = {
    "success": "успех",
    "warning": "внимание",
    "error": "ошибка",
    "info": "подсказка",
}


def message(kind: str, text: str) -> dict[str, str]:
    """Сообщение одного вида (чистая функция — проверяется без Streamlit).

    Raises:
        ValueError: если вид сообщения неизвестен (опечатка в коде экрана).
    """
    if kind not in KINDS:
        raise ValueError(f"неизвестный вид сообщения: {kind} (есть: {', '.join(KINDS)})")
    return {"kind": kind, "text": str(text)}


def push(kind: str, text: str) -> None:
    """Кладёт сообщение в очередь показа (появится после перерисовки экрана)."""
    queue: list[dict[str, str]] = st.session_state.setdefault(KEY_FLASH, [])
    queue.append(message(kind, text))


def success(text: str) -> None:
    """Сообщает об успешном действии."""
    push("success", text)


def warning(text: str) -> None:
    """Сообщает о предупреждении: действие выполнено не полностью."""
    push("warning", text)


def error(text: str) -> None:
    """Сообщает об ошибке: действие не выполнено."""
    push("error", text)


def info(text: str) -> None:
    """Показывает подсказку по действию."""
    push("info", text)


def render(items: list[dict[str, Any]] | None = None) -> None:
    """Показывает накопленные сообщения и очищает очередь.

    Args:
        items: сообщения, переданные явно (когда экран рисует их в своём порядке);
            по умолчанию берётся очередь из состояния сессии.
    """
    queue = items if items is not None else st.session_state.pop(KEY_FLASH, [])
    for item in queue or []:
        kind = str(item.get("kind") or "info")
        text = str(item.get("text") or "")
        if not text:
            continue
        if kind == "success":
            st.success(text)
        elif kind == "warning":
            st.warning(text)
        elif kind == "error":
            st.error(text)
        else:
            st.info(text)
