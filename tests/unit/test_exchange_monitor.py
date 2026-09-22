"""Тесты монитора обмена (компонент `acceptance.ui.common.exchange_monitor`).

Проверяется то, что решает комиссия, разбирая прогон: отбор и порядок записей в
ленте, бейдж результата и режим «только ошибки». Виджеты Streamlit не вызываются —
проверяются чистые функции компонента.
"""

from __future__ import annotations

from typing import Any

import pytest

from acceptance.http_log import HttpExchange
from acceptance.ui.common import exchange_monitor


def _record(
    seq: int,
    *,
    method: str = "GET",
    path: str = "/api/data/files",
    status: int | None = 200,
    label: str | None = None,
    error: str | None = None,
) -> HttpExchange:
    """Запись журнала с минимально нужными полями."""
    return HttpExchange(
        seq=seq,
        started_at=f"2026-09-21T10:00:{seq:02d}",
        method=method,
        path=path,
        query="",
        status=status,
        duration_ms=10.0 * seq,
        request_bytes=10,
        response_bytes=20,
        content_type="application/json",
        error=error,
        label=label,
    )


@pytest.fixture
def records() -> list[HttpExchange]:
    """Лента из трёх обменов: успех, ошибка API и сетевая ошибка."""
    return [
        _record(1, label="TC-FILE-01"),
        _record(2, method="POST", path="/api/datasets/", status=422, label="TC-DS-01"),
        _record(3, path="/api/tasks/", status=None, error="ConnectError: недоступен"),
    ]


def test_default_settings_are_visible_and_detailed_enough():
    """По умолчанию лента показывает новые сверху, ответ — с телом."""
    settings = exchange_monitor.MonitorSettings()

    assert settings.request_detail == "сводка"
    assert settings.response_detail == "тело"
    assert settings.newest_first is True
    assert settings.only_errors is False
    assert settings.rows == exchange_monitor.DEFAULT_ROWS
    assert settings.request_hint and settings.response_hint


def test_select_records_orders_newest_first(records: list[HttpExchange]):
    """«Сначала новые» переворачивает ленту: последний обмен — наверху."""
    settings = exchange_monitor.MonitorSettings(newest_first=True)

    selected = exchange_monitor.select_records(records, settings)

    assert [item.seq for item in selected] == [3, 2, 1]


def test_select_records_keeps_chronology_when_asked(records: list[HttpExchange]):
    """«Сначала старые» сохраняет хронологию прогона."""
    settings = exchange_monitor.MonitorSettings(newest_first=False)

    assert [item.seq for item in exchange_monitor.select_records(records, settings)] == [1, 2, 3]


def test_select_records_filters_by_errors_and_label(records: list[HttpExchange]):
    """Фильтры ленты: «только ошибки» и метка проверки/плана."""
    errors_only = exchange_monitor.MonitorSettings(only_errors=True)
    by_label = exchange_monitor.MonitorSettings(label="TC-DS-01")

    assert [item.seq for item in exchange_monitor.select_records(records, errors_only)] == [3, 2]
    assert [item.seq for item in exchange_monitor.select_records(records, by_label)] == [2]


def test_select_records_limits_the_feed(records: list[HttpExchange]):
    """Лимит записей действует после фильтра и порядка."""
    settings = exchange_monitor.MonitorSettings(newest_first=False, rows=2)

    assert [item.seq for item in exchange_monitor.select_records(records, settings)] == [1, 2]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_record(1, status=200), "✅ 200"),
        (_record(2, status=404), "⚠️ 404"),
        (_record(3, status=503), "❌ 503"),
        (_record(4, status=None, error="ConnectError"), "🚫 нет ответа"),
    ],
)
def test_status_badge_shows_result_class(record: HttpExchange, expected: str):
    """Бейдж результата: 2xx — успех, 4xx — предупреждение, 5xx и сеть — ошибка."""
    assert exchange_monitor.status_badge(record) == expected


def test_detail_levels_come_from_exchange_module():
    """Уровни подробности не дублируются: компонент берёт их из `exchange`."""
    from acceptance import exchange as exchange_api

    assert exchange_monitor.DETAIL_LEVELS is exchange_api.DETAIL_LEVELS
    assert set(exchange_api.DETAIL_HINTS) == set(exchange_api.DETAIL_LEVELS)


def test_render_controls_uses_two_independent_selectors(monkeypatch: pytest.MonkeyPatch):
    """Подробность запроса и ответа выбирается раздельно (требование заказчика)."""
    captured: dict[str, Any] = {}

    class _Column:
        """Заглушка колонки Streamlit: виджеты делегируют в подменённые функции."""

        def __getattr__(self, name: str) -> Any:
            return getattr(exchange_monitor.st, name)

    def fake_selectbox(label: str, options: Any, **kwargs: Any) -> str:
        captured[str(kwargs.get("key"))] = (label, list(options))
        return str(list(options)[0])

    monkeypatch.setattr(exchange_monitor.st, "selectbox", fake_selectbox)
    monkeypatch.setattr(exchange_monitor.st, "columns", lambda spec: [_Column() for _ in spec])
    monkeypatch.setattr(exchange_monitor.st, "checkbox", lambda *args, **kwargs: False)
    monkeypatch.setattr(exchange_monitor.st, "radio", lambda *args, **kwargs: "сначала новые")

    settings = exchange_monitor.render_controls(key_prefix="unit")

    assert set(captured) >= {"unit_detail_request", "unit_detail_response"}
    assert captured["unit_detail_request"][0] == "Подробность запроса"
    assert captured["unit_detail_response"][0] == "Подробность ответа"
    assert settings.request_detail == exchange_monitor.DETAIL_LEVELS[0]
