"""Повторяющиеся куски интерфейса: заголовок экрана, три зоны, таблицы и KPI.

Макет `docs/16` §0 задаёт одинаковую рамку всех экранов: слева навигация (её
рисует `acceptance/app.py`), в центре рабочая область, справа панель контекста.
Этот модуль даёт экранам эту рамку и «мелкую типографику», чтобы одинаковые вещи
(шапка, KPI, таблица результатов) выглядели одинаково и не переписывались в
каждом экране.

Чистые функции (`header_caption`, `plural`, `percent`, `join_parts`) проверяются
unit-тестом; рисующие (`render_header`, `zones`, `kpi`, `rows_table`) используют их.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from acceptance import glossary
from acceptance.ui import nav, state

#: Соотношение зон: рабочая область против панели контекста (макет `docs/16` §0).
WORKSPACE_RATIO = (3, 1)

#: Экран, на который ведёт действие «открыть сессию» из состояния «нет сессии».
SESSION_SCREEN = "scr203_session"

#: Разделитель подписей в «одной строке» (макет печатает ` · `).
SEPARATOR = " · "


def header_caption(spec: nav.Screen, subtitle: str = "") -> str:
    """Подпись под заголовком: «SCR-301 · группа «Испытания» · фаза Ф1–Ф2»."""
    text = f"{spec.code}{SEPARATOR}группа «{spec.group}»{SEPARATOR}фаза {spec.phase}"
    return f"{text}{SEPARATOR}{subtitle}" if subtitle else text


def join_parts(*parts: Any) -> str:
    """Склеивает непустые части через ` · ` (пустые пропускаются)."""
    return SEPARATOR.join(str(part) for part in parts if str(part or "").strip())


def plural(number: int, forms: tuple[str, str, str]) -> str:
    """Русское склонение: `plural(3, ("пункт", "пункта", "пунктов"))` → «3 пункта»."""
    count = abs(int(number))
    if count % 10 == 1 and count % 100 != 11:
        form = forms[0]
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        form = forms[1]
    else:
        form = forms[2]
    return f"{count} {form}"


def percent(done: int, total: int) -> str:
    """Готовность в процентах («34 из 41 · 83 %»); без пунктов — «нет пунктов»."""
    if total <= 0:
        return "нет пунктов"
    share = round(int(done) * 100 / int(total))
    return f"{int(done)} из {int(total)}{SEPARATOR}{share} %"


def short_time(value: Any, format_text: str = "%d.%m %H:%M") -> str:
    """Короткое время для интерфейса: `21.09 10:12` (иначе — исходный текст).

    В таблицах и подписях нужен компактный вид времени, а в файлах сессии время
    хранится полным ISO-момент; функция переводит одно в другое, а нераспознанный
    текст возвращает как есть, чтобы событие не исчезло из-за формата.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    return f"{moment:{format_text}}"


def render_header(key: str, subtitle: str = "") -> None:
    """Рисует шапку экрана: заголовок из глоссария и подпись с кодом, группой, фазой."""
    st.title(glossary.screen_label(key))
    st.caption(header_caption(nav.by_key(key), subtitle))


def zones() -> tuple[DeltaGenerator, DeltaGenerator]:
    """Две зоны макета: рабочая область и панель контекста."""
    workspace, context = st.columns(list(WORKSPACE_RATIO), gap="large")
    return workspace, context


def kpi(items: Sequence[tuple[str, Any]], *, columns: int = 4) -> None:
    """Ряд KPI: подпись → значение (`items` — пары «подпись, значение»)."""
    if not items:
        return
    per_row = max(1, min(int(columns), len(items)))
    chunks = [items[index : index + per_row] for index in range(0, len(items), per_row)]
    for chunk in chunks:
        cells = st.columns(len(chunk))
        for cell, (label, value) in zip(cells, chunk, strict=True):
            cell.metric(label, value)


def rows_table(
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    columns: Mapping[str, str],
    height: int | None = None,
) -> None:
    """Таблица строк: `columns` — соответствие «поле строки → заголовок колонки».

    Высота задаётся, когда таблица — рабочий список (очередь, протокол): так
    прокручивается только список, а шапка экрана остаётся на месте.
    """
    config = {field: st.column_config.TextColumn(title) for field, title in columns.items()}
    size: int | Literal["auto"] = height if height is not None else "auto"
    st.dataframe(
        [dict(row) for row in rows],
        column_config=config,
        column_order=list(columns),
        hide_index=True,
        use_container_width=True,
        height=size,
        key=key,
    )


def render_no_session(*, what: str = "Экран работает в контексте сессии испытаний") -> None:
    """Состояние «нет сессии» (`IR-P-8`) с переходом к созданию сессии."""
    st.warning(f"Сессия испытаний не выбрана. {what}.")
    if st.button("Открыть «Сессия испытаний» (SCR-203)", type="primary", key="goto_session"):
        state.go_to(SESSION_SCREEN)
