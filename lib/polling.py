"""Сервис поллинга статуса задач (NFR-1).

Синхронная реализация, пригодная для использования внутри Streamlit:
длинные операции не блокируют UI, статус опрашивается через `GET /api/tasks/{id}`
до достижения терминального состояния или таймаута.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from loguru import logger

T = TypeVar("T")


def poll_until(
    fetch: Callable[[], T],
    is_done: Callable[[T], bool],
    *,
    interval: float = 1.0,
    timeout: float | None = None,
) -> T:
    """Опрашивает `fetch()` до тех пор, пока `is_done()` не вернёт True.

    Args:
        fetch: функция, возвращающая актуальное состояние (например, задачу).
        is_done: предикат терминального состояния.
        interval: пауза между опросами, секунды.
        timeout: максимальное время поллинга (None — без ограничения).

    Returns:
        Последнее полученное состояние (возможно, не терминальное при таймауте).
    """
    started = time.monotonic()

    while True:
        result = fetch()
        if is_done(result):
            return result

        if timeout is not None and (time.monotonic() - started) >= timeout:
            logger.warning("Поллинг прерван по таймауту ({}s)", timeout)
            return result

        time.sleep(interval)
