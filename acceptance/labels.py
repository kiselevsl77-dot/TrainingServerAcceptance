"""Подписи и компактные представления: сборка стенда, идентификаторы и длинные тексты.

Модуль лежит в ядре (`acceptance/`), а не в UI-слое: это чистые функции без Streamlit,
и ими пользуются и экраны, и снимок стенда, и отчёт.

* `build_label` формирует краткую идентификацию сборки испытуемого сервера
  (`branch@revision`) — она используется в шапке сессии, на экране «Стенд» и в отчёте;
* `short_id` и `head_text` сокращают длинные значения для таблиц: идентификатор $задачи
  нужен оператору редко, а описание занимает всю ширину. Полные значения всегда доступны
  в панели строки (экран `SCR-303`), поэтому в таблице показывается «хвост» идентификатора
  и первые символы текста.
"""

from __future__ import annotations

from typing import Any

#: Сколько последних символов идентификатора оставлять в таблице.
SHORT_ID_KEEP = 8

#: Сколько первых символов названия/описания показывать в таблице.
HEAD_TEXT_LIMIT = 21

#: Символ-многоточие в начале сокращённого идентификатора.
ELLIPSIS = "…"


def build_label(version: Any) -> str:
    """Подпись сборки сервера из ответа `GET /version` (или «не определена»)."""
    if not isinstance(version, dict):
        return "не определена"

    revision = str(version.get("revision") or version.get("commit") or "")[:12]
    branch = str(version.get("branch") or "")
    if revision and branch:
        return f"{branch}@{revision}"
    return revision or branch or "не определена"


def short_id(value: Any, keep: int = SHORT_ID_KEEP) -> str:
    """Хвост идентификатора с многоточием: `b090e792` → `…b090e792`.

    Полный идентификатор показывается в панели строки (с копированием), а в таблице
    достаточно хвоста: он уникален и не занимает пол-экрана. Короткие значения
    (или уже сокращённые) возвращаются как есть.
    """
    text = str(value or "").strip()
    if not text or len(text) <= keep:
        return text
    return f"{ELLIPSIS}{text[-keep:]}"


def head_text(value: Any, limit: int = HEAD_TEXT_LIMIT) -> str:
    """Первые `limit` символов текста с многоточием (`…`), если текст длиннее.

    Нужна для колонок «Название» и «Описание»: имена $задач вида
    `fill_dataset_06b5d3f3-…` иначе выдавливают из таблицы все остальные колонки.
    Полный текст — в панели строки.
    """
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}{ELLIPSIS}"
