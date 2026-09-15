"""Клиентская пагинация списков (NFR-4).

`GET /api/data/files` не поддерживает `limit`/`offset` — параметры отсутствуют
в спецификации и игнорируются сервером (замечание к API), поэтому страницы для
списков файлов и субдатасетов формируются на стороне UI. Серверная пагинация
(`GET /api/tasks/`, `GET /api/loads/list`) применяется там, где она есть.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

DEFAULT_PER_PAGE = 25
PER_PAGE_OPTIONS = (10, 25, 50, 100)


@dataclass(frozen=True)
class Page(Generic[T]):
    """Страница списка (нумерация страниц — с единицы)."""

    items: list[T]
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        """Общее число страниц (не меньше единицы)."""
        return max(1, -(-self.total // self.per_page))

    @property
    def first_index(self) -> int:
        """Номер первого элемента страницы (1-based; 0 для пустой страницы)."""
        return 0 if not self.items else (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        """Номер последнего элемента страницы (1-based)."""
        return (self.page - 1) * self.per_page + len(self.items)

    @property
    def has_prev(self) -> bool:
        """Есть ли предыдущая страница."""
        return self.page > 1

    @property
    def has_next(self) -> bool:
        """Есть ли следующая страница."""
        return self.page < self.pages

    @property
    def label(self) -> str:
        """Подпись «Показано X–Y из N» / «Ничего не найдено»."""
        if self.total == 0:
            return "Ничего не найдено"
        return f"Показано {self.first_index}–{self.last_index} из {self.total}"


def paginate(items: Sequence[T], page: int = 1, per_page: int = DEFAULT_PER_PAGE) -> Page[T]:
    """Возвращает страницу списка, безопасно корректируя границы.

    Args:
        items: полный список (фильтры применены ранее).
        page: запрошенная страница (1-based); значения вне диапазона обрезаются.
        per_page: размер страницы (минимум 1).
    """
    size = max(1, int(per_page))
    total = len(items)
    pages = max(1, -(-total // size))
    current = min(max(1, int(page)), pages)
    start = (current - 1) * size

    return Page(
        items=list(items[start : start + size]),
        page=current,
        per_page=size,
        total=total,
    )


def server_page(items: Sequence[T], page: int, per_page: int, total: int) -> Page[T]:
    """Страница по серверному срезу (`limit`/`offset` + `result_size`).

    Сервер сам применяет пагинацию (`GET /api/loads/list`, `GET /api/tasks/`):
    `items` — уже полученный срез, `total` — общее число записей (`result_size`).

    Args:
        items: срез, возвращённый сервером.
        page: номер страницы (1-based), соответствующий запрошенному `offset`.
        per_page: размер страницы (запрошенный `limit`).
        total: общее число записей на сервере.
    """
    return Page(
        items=list(items),
        page=max(1, int(page)),
        per_page=max(1, int(per_page)),
        total=max(0, int(total)),
    )
