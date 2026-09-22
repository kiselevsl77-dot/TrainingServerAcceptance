"""Тесты компактных подписей интерфейса (`acceptance.labels`).

Таблицы пульта показывают «хвост» идентификатора и первые символы текста: колонки
«Задача», «Название» и «Описание» иначе выдавливают друг друга (например, имя задачи
`fill_dataset_06b5d3f3-…` шириной в экран). Полные значения оператор видит в панели
строки, поэтому сокращение должно быть предсказуемым: ровно 8 последних символов и
21 первый символ с многоточием.
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

TASK = "9f0a5b7e-0000-4000-8000-000000000001"


def test_short_id_keeps_the_tail():
    """Идентификатор сокращается до последних символов с многоточием в начале."""
    assert short_id(TASK) == f"{ELLIPSIS}{TASK[-SHORT_ID_KEEP:]}"
    assert short_id(TASK) == "…00000001"
    assert len(short_id(TASK)) == SHORT_ID_KEEP + 1


def test_short_id_passes_short_and_empty_values_through():
    """Короткое значение (или пустое) не портится: многоточие не добавляется."""
    assert short_id("b090e792") == "b090e792"
    assert short_id("") == ""
    assert short_id(None) == ""
    assert short_id("  b090e792  ") == "b090e792"


def test_short_id_respects_custom_keep():
    """Длину «хвоста» можно задать (таблица против отчёта)."""
    assert short_id(TASK, keep=4) == "…0001"


def test_head_text_truncates_long_values():
    """Длинный текст обрезается по границе с многоточием."""
    text = "fill_dataset_06b5d3f3-0000-4000-8000-000000000001"

    cut = head_text(text)

    assert cut == f"{text[:HEAD_TEXT_LIMIT]}{ELLIPSIS}"
    assert len(cut) == HEAD_TEXT_LIMIT + 1
    assert cut.startswith("fill_dataset_06b5d3")


def test_head_text_keeps_short_values_intact():
    """Короткий текст отдаётся как есть — в том числе ровно по границе."""
    assert head_text("celery-test") == "celery-test"
    assert head_text("x" * HEAD_TEXT_LIMIT) == "x" * HEAD_TEXT_LIMIT
    assert head_text(None) == ""
    assert head_text("") == ""


def test_head_text_limit_is_configurable():
    """Лимит можно уменьшить для узких колонок."""
    assert head_text("abcdef", limit=3) == "abc…"


def test_build_label_formats_branch_and_revision():
    """Подпись сборки: `branch@revision` (ревизия — до 12 символов)."""
    assert build_label({"branch": "dev", "revision": "58ac72f1234567"}) == "dev@58ac72f12345"
    assert build_label({"commit": "58ac72f1234567"}) == "58ac72f12345"
    assert build_label({"branch": "dev"}) == "dev"
    assert build_label({}) == "не определена"
    assert build_label(None) == "не определена"
