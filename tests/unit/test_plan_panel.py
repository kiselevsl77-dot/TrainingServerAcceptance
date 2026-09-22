"""Тесты панели программы испытаний (`acceptance/ui/common/plan_panel.py`).

Проверяются решения панели, а не разметка: как читаются галочки, как массовые
действия синхронизируют виджеты и как считается пауза авто-прогона. Логика галочек
здесь принципиальна: если сравнивать значение виджета с моделью «в лоб», интерфейс
и план начинают перетягивать друг друга (найдено смоук-тестом — бесконечная
перерисовка), поэтому переключения идут через колбэки и `session_state`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from acceptance import plan
from acceptance.ui.common import plan_panel


class _FakeStreamlit:
    """Заглушка `st` с `session_state`, достаточная для чистых помощников панели."""

    def __init__(self) -> None:
        self.session_state: dict[str, Any] = {}


@pytest.fixture
def fake_st(monkeypatch: pytest.MonkeyPatch) -> _FakeStreamlit:
    """Подменяет `streamlit` внутри панели и возвращает управляемое состояние."""
    fake = _FakeStreamlit()
    monkeypatch.setattr(plan_panel, "st", fake)
    return fake


def _built() -> plan.PlanState:
    """Небольшой план: одна проверка с одним вызовом."""
    from acceptance.checks import catalog

    spec = next(item for item in catalog.CHECKS if item.endpoints or item.probe_paths)
    return plan.build_plan([spec])


def test_checkbox_value_prefers_widget_state(fake_st: _FakeStreamlit):
    """Значение галочки берётся из виджета: он знает про последнее действие оператора."""
    state = _built()
    item = state.items[0]
    fake_st.session_state[plan_panel._widget_key(item)] = False

    assert item.enabled is True
    assert plan_panel._checkbox_value(item) is False


def test_checkbox_value_falls_back_to_model(fake_st: _FakeStreamlit):
    """Если виджета ещё нет, значение берётся из пункта плана."""
    state = _built()
    item = state.items[0]

    assert plan_panel._checkbox_value(item) is item.enabled


def test_sync_checkbox_touches_only_existing_widgets(fake_st: _FakeStreamlit):
    """Синхронизация не создаёт виджеты, которых нет (иначе Streamlit теряет состояние)."""
    state = _built()
    check = state.items[0]
    fake_st.session_state[plan_panel._widget_key(check)] = True

    plan_panel.sync_checkbox(check, False)

    assert fake_st.session_state[plan_panel._widget_key(check)] is False


def test_toggle_check_updates_children_widgets(fake_st: _FakeStreamlit):
    """Снятие галочки проверки снимает виджеты её вызовов (и модель)."""
    state = _built()
    check = state.items[0]
    calls = state.calls_of(check.item_id)
    assert calls, "у выбранной проверки есть вызов"
    fake_st.session_state[plan_panel._widget_key(check)] = False
    for call in calls:
        fake_st.session_state[plan_panel._widget_key(call)] = True

    plan_panel._on_check_toggle(state, check)

    assert check.enabled is False
    assert all(call.enabled is False for call in calls)
    assert all(fake_st.session_state[plan_panel._widget_key(call)] is False for call in calls)


def test_toggle_call_wakes_up_parent_widget(fake_st: _FakeStreamlit):
    """Включение вызова поднимает галочку проверки-родителя (модель и виджет)."""
    state = _built()
    check = state.items[0]
    call = state.calls_of(check.item_id)[0]
    check.enabled = False
    call.enabled = False
    fake_st.session_state[plan_panel._widget_key(check)] = False
    fake_st.session_state[plan_panel._widget_key(call)] = True

    plan_panel._on_call_toggle(state, call)

    assert call.enabled is True
    assert check.enabled is True
    assert fake_st.session_state[plan_panel._widget_key(check)] is True


def test_toggle_saves_plan_into_session(fake_st: _FakeStreamlit, monkeypatch: pytest.MonkeyPatch):
    """Колбэк галочки сохраняет план в сессию (иначе решение оператора потеряется)."""
    session = SimpleNamespace(session_id="S-1", plan={}, add_history=lambda *a, **k: None)
    stored: list[Any] = []
    monkeypatch.setattr(plan_panel.state, "current_session", lambda: session)
    monkeypatch.setattr(plan_panel.state, "store_session", lambda value: stored.append(value))
    state = _built()
    check = state.items[0]
    fake_st.session_state[plan_panel._widget_key(check)] = False

    plan_panel._on_check_toggle(state, check)

    assert session.plan["items"], "план записан в сессию"
    assert stored == [session]


def test_plan_state_loads_once_and_is_reused(fake_st: _FakeStreamlit, monkeypatch):
    """План читается из сессии один раз и живёт в `session_state` между перерисовками."""
    session = SimpleNamespace(session_id="S-2", plan={}, add_history=lambda *a, **k: None)
    monkeypatch.setattr(plan_panel.state, "current_session", lambda: session)

    first = plan_panel.plan_state()
    session.plan = {"items": [{"item_id": "TC-OTHER-01", "level": plan.LEVEL_CHECK}]}
    second = plan_panel.plan_state()

    assert first is second, "план не пересобирается на каждой перерисовке"


def test_plan_state_reloads_for_other_session(fake_st: _FakeStreamlit, monkeypatch):
    """При смене сессии план берётся из неё, а не из прежнего рабочего состояния."""
    holder = SimpleNamespace(session_id="S-1", plan={}, add_history=lambda *a, **k: None)
    monkeypatch.setattr(plan_panel.state, "current_session", lambda: holder)
    first = plan_panel.plan_state()

    other = SimpleNamespace(
        session_id="S-2",
        plan={"items": [{"item_id": "TC-NEW-01", "level": plan.LEVEL_CHECK}]},
        add_history=lambda *a, **k: None,
    )
    monkeypatch.setattr(plan_panel.state, "current_session", lambda: other)
    second = plan_panel.plan_state()

    assert first is not second
    assert second.find("TC-NEW-01") is not None


def test_plan_state_builds_plan_without_session(fake_st: _FakeStreamlit, monkeypatch):
    """Без выбранной сессии панель всё равно показывает план (по каталогу проверок)."""
    monkeypatch.setattr(plan_panel.state, "current_session", lambda: None)

    state = plan_panel.plan_state()

    assert state.items
    assert state.find("TC-SYS-01") is not None


def test_pause_elapsed_respects_interval():
    """Пауза авто-прогона: пока интервал не выдержан, следующий вызов не уходит."""
    state = plan.PlanState(pause_seconds=30.0)

    assert plan_panel._pause_elapsed(state) is True, "без отметки времени шаг разрешён"

    state.last_at = "2999-09-21T10:00:00"
    assert plan_panel._pause_elapsed(state) is False

    state.last_at = "2000-01-01T00:00:00"
    assert plan_panel._pause_elapsed(state) is True


def test_pause_elapsed_survives_broken_timestamp():
    """Битая отметка времени не блокирует прогон (лучше продолжить, чем встать)."""
    state = plan.PlanState(pause_seconds=30.0, last_at="не дата")

    assert plan_panel._pause_elapsed(state) is True


def test_curl_preview_substitutes_path_and_query():
    """Предпросмотр `curl` подставляет параметры пункта и query-строку."""
    from acceptance import plan_runner

    state = plan.build_plan()
    call = next(item for item in state.items if item.is_call and plan_runner.path_params(item))
    name = plan_runner.path_params(call)[0]
    values = dict(call.params)
    values[name] = "__TEST__entity"
    runtime = SimpleNamespace(settings=SimpleNamespace(base_url="https://stand.local"))

    command = plan_panel._curl_preview(call, values, runtime)

    assert command.startswith(f"curl -X {call.method}")
    assert "https://stand.local" in command
    assert f"{{{name}}}" not in command
    assert "__TEST__entity" in command


def test_default_curl_preview_keeps_placeholder_for_empty_value():
    """Без значения параметра в команде остаётся шаблон — оператор видит, что не заполнено."""
    from acceptance import plan_runner

    state = plan.build_plan()
    call = next(item for item in state.items if item.is_call and plan_runner.path_params(item))
    name = plan_runner.path_params(call)[0]
    runtime = SimpleNamespace(settings=SimpleNamespace(base_url="https://stand.local"))

    command = plan_panel._curl_preview(call, {}, runtime)

    assert f"{{{name}}}" in command


def test_matches_text_filters_items_by_id_title_and_operation():
    """Поиск по выборке ловит пункт по id, названию и операции."""
    state = plan.build_plan()
    item = state.find("TC-FILE-01#1")
    assert item is not None

    assert plan_panel._matches_text(item, "TC-FILE-01")
    assert plan_panel._matches_text(item, "/api/data/files")
    assert not plan_panel._matches_text(item, "TC-LOAD-07")


def test_verdict_hint_shows_duration_and_journal_range():
    """Подсказка вердикта показывает длительность и диапазон записей журнала."""
    item = plan.PlanItem(
        item_id="TC-FILE-01#1",
        level=plan.LEVEL_CALL,
        title="чтение",
        duration_ms=12.4,
        journal_from=5,
        journal_to=7,
    )

    hint = plan_panel._verdict_hint(item)

    assert "12 мс" in hint
    assert "#5–#7" in hint
