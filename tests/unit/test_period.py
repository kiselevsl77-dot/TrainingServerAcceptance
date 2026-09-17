"""Тесты границ периода для фильтров даты-времени (`lib/period.py`, этап T4).

Фиксируется поведение, найденное на живом стенде 16.09.2026: `GET /api/tasks/`
принимает `start_date`/`end_date` только как дату-время (`format: date-time` в
спецификации) — строка из одной даты отвечает 422 с текстом «invalid datetime
separator, expected T, t, _ or space», а верхняя граница является исключающей
(`created_at < end_date`). Поэтому сутки переводятся в `<день>T00:00:00` и
`<следующий день>T00:00:00`, и выбранный день попадает в выборку целиком.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from lib.period import DAY_START, date_time_bound, day_bounds


def test_day_bounds_uses_date_time_format():
    """Обе границы периода — дата-время, а не «чистая» дата (иначе 422)."""
    assert day_bounds(date(2026, 9, 9), date(2026, 9, 16)) == (
        "2026-09-09T00:00:00",
        "2026-09-17T00:00:00",
    )


def test_day_bounds_keeps_explicit_time_of_day():
    """День начала и день конца различаются: конец — начало следующих суток."""
    start, end = day_bounds(date(2026, 9, 9), date(2026, 9, 9))
    assert start == "2026-09-09T00:00:00"
    assert end == "2026-09-10T00:00:00"
    assert datetime.fromisoformat(end) > datetime(2026, 9, 9, 23, 59, 59)


def test_day_bounds_returns_empty_string_for_missing_bounds():
    """Незаданная граница не отправляется на сервер (пустая строка)."""
    assert day_bounds(None, None) == ("", "")
    assert day_bounds(date(2026, 9, 9), None) == ("2026-09-09T00:00:00", "")
    assert day_bounds(None, date(2026, 9, 9)) == ("", "2026-09-10T00:00:00")


@pytest.mark.parametrize("value", ["2026-09-09", 20260909, None, [], object()])
def test_date_time_bound_ignores_non_dates(value):
    """Не-даты (в том числе результат очистки `st.date_input`) дают пустую строку."""
    assert date_time_bound(value) == ""


@pytest.mark.parametrize(
    ("day", "days", "expected"),
    [
        (date(2026, 9, 30), 1, "2026-10-01T00:00:00"),
        (date(2026, 12, 31), 1, "2027-01-01T00:00:00"),
        (date(2024, 2, 28), 1, "2024-02-29T00:00:00"),
        (date(2026, 9, 9), 0, "2026-09-09T00:00:00"),
        (date(2026, 1, 1), -1, "2025-12-31T00:00:00"),
    ],
)
def test_date_time_bound_crosses_month_year_and_leap_day(day, days, expected):
    """Границы корректно переходят через месяц, год и 29 февраля."""
    assert date_time_bound(day, days=days) == expected


def test_bounds_are_parsed_as_date_time_by_the_stand():
    """Значения разбираются как `datetime`: разделитель `T` присутствует всегда."""
    bounds = day_bounds(date(2026, 9, 9), date(2026, 9, 16))
    assert all("T" in bound for bound in bounds)
    assert all(datetime.fromisoformat(bound) for bound in bounds)
    assert DAY_START.hour == 0
    assert DAY_START.minute == 0
