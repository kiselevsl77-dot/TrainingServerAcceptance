"""Подписи интерфейса — реэкспорт из ядра (`acceptance/labels.py`).

Чистые текстовые хелперы переехали в ядро на шаге 2a этапа 2 (чтобы тесты снимка стенда
не зависели от UI-слоя). Модуль оставлен пустой обёрткой только для старого UI
(`acceptance/ui/pages/**`) и удаляется вместе с ним на шаге 2b.
"""

from __future__ import annotations

from acceptance.labels import (
    ELLIPSIS,
    HEAD_TEXT_LIMIT,
    SHORT_ID_KEEP,
    build_label,
    head_text,
    short_id,
)

__all__ = [
    "ELLIPSIS",
    "HEAD_TEXT_LIMIT",
    "SHORT_ID_KEEP",
    "build_label",
    "head_text",
    "short_id",
]
