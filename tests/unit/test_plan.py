"""Тесты программы испытаний (`acceptance/plan.py`).

Проверяются те свойства плана, на которых держатся пожелания заказчика
(21.09.2026, п. 3): состав и порядок запланированных вызовов, галочки «что пойдёт
на стенд», переход «следующий пункт» и оценка «соответствует ожиданию».
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from acceptance import endpoints as ep
from acceptance import plan
from acceptance.checks import catalog
from acceptance.checks.registry import CheckClass, CheckSpec


@pytest.fixture(scope="module")
def built() -> plan.PlanState:
    """План, собранный по наполненному каталогу проверок."""
    return plan.build_plan()


def item_of(state: plan.PlanState, item_id: str) -> plan.PlanItem:
    """Пункт плана по идентификатору — с проверкой, что он есть.

    Помощник нужен ещё и типизации: `PlanState.find` возвращает `PlanItem | None`,
    а обращение к атрибутам найденного пункта в тесте должно быть безопасным.
    """
    found = state.find(item_id)
    assert found is not None, f"пункт {item_id} не найден в плане"
    return found


def test_plan_covers_every_check_and_its_calls(built: plan.PlanState):
    """На каждую проверку каталога — пункт проверки и пункты её вызовов."""
    checks = built.checks()

    assert [item.item_id for item in checks] == [spec.check_id for spec in catalog.CHECKS]
    for spec in catalog.CHECKS:
        calls = built.calls_of(spec.check_id)
        expected = len(spec.endpoints) + len(spec.probe_paths)
        assert len(calls) == expected, spec.check_id
        for index, call in enumerate(calls, start=1):
            assert call.item_id == f"{spec.check_id}#{index}"
            assert call.parent_id == spec.check_id
            assert call.is_call and call.level == plan.LEVEL_CALL


def test_item_ids_are_unique(built: plan.PlanState):
    """Идентификаторы пунктов уникальны (иначе поедут ключи виджетов и журнал)."""
    ids = [item.item_id for item in built.items]

    assert len(ids) == len(set(ids))


def test_order_follows_program_of_checks(built: plan.PlanState):
    """Порядок пунктов — как в каталоге (порядок этапов T3 → T5)."""
    groups = [item.group for item in built.checks()]

    assert groups == [catalog.group_of(spec.check_id) for spec in catalog.CHECKS]
    assert groups[0] == "TC-SYS"


def test_calls_reference_registry_operations(built: plan.PlanState):
    """Вызовы ссылаются на операции реестра: метод, путь, класс безопасности, ожидание."""
    call = built.find("TC-FILE-01#1")

    assert call is not None
    assert call.method == "GET"
    assert call.path == "/api/data/files"
    assert call.endpoint_key == "get /api/data/files"
    assert call.safety == str(ep.Safety.READ)
    assert "2xx" in call.expected
    assert call.probe is False


def test_negative_probes_are_marked_and_expect_missing_route(built: plan.PlanState):
    """Негативные пробы помечены: ожидается отсутствие маршрута (404/405)."""
    probes = [item for item in built.items if item.probe]

    assert probes, "в каталоге есть пробы (TC-SYS-03/05, TC-FILE-14, TC-LOAD-07)"
    for probe in probes:
        assert probe.endpoint_key == ""
        assert "404/405" in probe.expected


def test_heavy_check_calls_expect_task_id():
    """Вызовы ресурсоёмкой проверки ожидают 202 и `task_id`."""
    spec = CheckSpec(
        check_id="TC-DS-99",
        title="наполнение датасета",
        module="Datasets",
        requirement="FR-4",
        check_class=CheckClass.HEAVY,
        endpoints=("post /api/datasets/fill/{dataset_id}",),
    )

    built = plan.build_plan([spec])
    call = built.find("TC-DS-99#1")

    assert call is not None
    assert "202" in call.expected
    assert call.safety == str(ep.Safety.HEAVY)


def test_checkbox_on_check_disables_its_calls(built: plan.PlanState):
    """Снятие галочки с проверки снимает все её вызовы (и наоборот)."""
    built.set_enabled_many([item.item_id for item in built.items], True)
    check = built.find("TC-FILE-01")
    assert check is not None
    calls = built.calls_of(check.item_id)

    built.set_enabled("TC-FILE-01", False)

    assert check.enabled is False
    assert all(call.enabled is False for call in calls)

    built.set_enabled(calls[0].item_id, True)

    assert calls[0].enabled is True
    assert item_of(built, "TC-FILE-01").enabled is True


def test_bulk_checkbox_change_works_over_selection(built: plan.PlanState):
    """Массовая установка галочек (кнопки «включить/снять всё в выборке»)."""
    selection = [item.item_id for item in built.items if item.group == "TC-LOAD"]

    built.set_enabled_many(selection, False)

    assert all(not item_of(built, item_id).enabled for item_id in selection)

    built.set_enabled_many(selection, True)

    assert all(item_of(built, item_id).enabled for item_id in selection)


def test_next_item_skips_disabled_and_walks_the_plan(built: plan.PlanState):
    """«Следующая» идёт по включённым пунктам режима и уважает снятые галочки."""
    built.reset()
    built.mode = plan.MODE_CHECK
    first = built.next_item()

    assert first is not None and first.item_id == "TC-SYS-01"

    built.last_item_id = first.item_id
    second = built.next_item()

    assert second is not None and second.item_id == "TC-SYS-02"

    built.set_enabled("TC-SYS-02", False)
    built.last_item_id = "TC-SYS-01"
    third = built.next_item()

    assert third is not None and third.item_id == "TC-SYS-03"

    built.set_enabled_many([item.item_id for item in built.items], False)

    assert built.next_item() is None
    built.set_enabled_many([item.item_id for item in built.items], True)


def test_call_mode_requires_enabled_parent_check(built: plan.PlanState):
    """В режиме «по вызовам» вызов исполняется, только если включена его проверка."""
    built.reset()
    built.set_enabled_many([item.item_id for item in built.items], True)
    built.mode = plan.MODE_CALL
    call = built.find("TC-FILE-01#1")
    assert call is not None

    built.set_enabled("TC-FILE-01", False)

    assert call not in built.enabled_items()
    assert built.find("TC-FILE-02#1") in built.enabled_items()
    built.mode = plan.MODE_CHECK


@pytest.mark.parametrize(
    ("status", "error", "expected_status", "expected_verdict"),
    [
        (200, "", plan.STATUS_PASSED, plan.VERDICT_MATCH),
        (422, "", plan.STATUS_FAILED, plan.VERDICT_MISMATCH),
        (500, "", plan.STATUS_FAILED, plan.VERDICT_MISMATCH),
    ],
)
def test_judge_call_for_reading_operation(
    built: plan.PlanState, status: int, error: str, expected_status: str, expected_verdict: str
):
    """Вердикт вызова чтения: 2xx — соответствует ожиданию, иначе — нет."""
    item = built.find("TC-FILE-01#1")
    assert item is not None

    verdict = plan.judge_call(item, status=status, error=error)

    assert verdict.status == expected_status
    assert verdict.verdict == expected_verdict


def test_judge_call_for_probe_and_network_error(built: plan.PlanState):
    """Проба ждёт 404/405, сетевая ошибка — «вызов не выполнен»."""
    probe = next(item for item in built.items if item.probe)

    missing = plan.judge_call(probe, status=404)
    unexpected = plan.judge_call(probe, status=200)
    network = plan.judge_call(probe, status=None, error="ConnectError: недоступен")

    assert missing.status == plan.STATUS_PASSED and missing.verdict == plan.VERDICT_MATCH
    assert unexpected.status == plan.STATUS_FAILED
    assert unexpected.verdict == plan.VERDICT_MISMATCH
    assert network.status == plan.STATUS_FAILED
    assert network.verdict == plan.VERDICT_NOT_RUN
    assert "ConnectError" in network.detail


def test_judge_call_notes_missing_task_id_for_heavy_operation():
    """Для `fill` 202 без `task_id` — успех с пометкой об известном дефекте."""
    spec = CheckSpec(
        check_id="TC-DS-98",
        title="наполнение",
        module="Datasets",
        requirement="FR-4",
        check_class=CheckClass.HEAVY,
        endpoints=("post /api/datasets/fill/{dataset_id}",),
    )
    item = plan.build_plan([spec]).find("TC-DS-98#1")
    assert item is not None

    without_task_id = plan.judge_call(item, status=202, task_id="")
    with_task_id = plan.judge_call(item, status=202, task_id="abc")

    assert without_task_id.status == plan.STATUS_PASSED
    assert "task_id" in without_task_id.detail
    assert with_task_id.status == plan.STATUS_PASSED
    assert with_task_id.detail == "ответ 202"


def test_summary_counts_statuses_and_checkboxes(built: plan.PlanState):
    """Сводка плана считает включённые, выполненные и отказы."""
    built.reset()
    item = built.find("TC-SYS-01")
    assert item is not None
    built.replace_item(item.mark(plan.STATUS_PASSED, verdict=plan.VERDICT_MATCH))
    built.set_enabled("TC-SYS-02", False)

    summary = built.summary()

    assert summary["total"] == len(built.items)
    assert summary["checks"] == len(catalog.CHECKS)
    assert summary["passed"] == 1
    assert summary["disabled"] >= 1
    assert summary["enabled"] + summary["disabled"] == summary["total"]
    assert summary["mode"] == plan.MODE_CHECK
    built.reset()
    built.set_enabled("TC-SYS-02", True)


def test_plan_survives_serialization(built: plan.PlanState):
    """План сохраняется в сессию и читается обратно (схема v6)."""
    built.reset()
    item = built.find("TC-TASK-01#1")
    assert item is not None
    built.replace_item(item.mark(plan.STATUS_FAILED, verdict=plan.VERDICT_MISMATCH))
    built.set_enabled("TC-DS-01", False)
    built.mode = plan.MODE_CALL
    built.pause_seconds = 7.5

    restored = plan.PlanState.from_dict(built.to_dict())

    assert restored.mode == plan.MODE_CALL
    assert restored.pause_seconds == 7.5
    assert item_of(restored, item.item_id).status == plan.STATUS_FAILED
    assert item_of(restored, "TC-DS-01").enabled is False
    assert item_of(restored, "TC-DS-01#1").enabled is False
    built.reset()
    built.set_enabled("TC-DS-01", True)
    built.mode = plan.MODE_CHECK


def test_session_helpers_build_and_store_the_plan():
    """`load_plan`/`save_plan` держат план в сессии (поле `plan`, схема v6)."""
    events: list[tuple[str, str]] = []
    session = SimpleNamespace(
        plan={},
        add_history=lambda event, message="", **payload: events.append((event, message)),
    )

    fresh = plan.load_plan(session)

    assert fresh.items, "пустая сессия получает план по каталогу"

    fresh.set_enabled("TC-SYS-01", False)
    plan.save_plan(session, fresh, history=True)

    assert session.plan["items"]
    reloaded = plan.load_plan(session)
    assert reloaded.find("TC-SYS-01").enabled is False
    assert events and events[-1][0] == "plan_updated"


def test_pause_is_clamped_to_allowed_range():
    """Пауза авто-прогона ограничена разумным диапазоном."""
    assert plan.clamp_pause(0) == plan.MIN_PAUSE_SECONDS
    assert plan.clamp_pause(10_000) == plan.MAX_PAUSE_SECONDS
    assert plan.clamp_pause("не число") == plan.DEFAULT_PAUSE_SECONDS


def test_verdicts_for_feed_map_journal_range_to_items():
    """Лента монитора получает оценку по диапазону журнала пункта."""
    state = plan.build_plan()
    item = state.find("TC-FILE-01#1")
    assert item is not None
    state.replace_item(
        item.mark(plan.STATUS_PASSED, verdict=plan.VERDICT_MATCH, journal_from=5, journal_to=6)
    )
    records = [SimpleNamespace(seq=seq) for seq in range(1, 8)]

    verdicts = plan.verdicts_for_feed(state, records)

    assert verdicts[5] == ("✅", plan.VERDICT_MATCH)
    assert verdicts[6] == ("✅", plan.VERDICT_MATCH)
    assert 4 not in verdicts and 7 not in verdicts


def test_expected_statuses_for_probe_and_ordinary_call(built: plan.PlanState):
    """Ожидаемые коды ответа: проба — 404/405, обычный вызов — 2xx."""
    probe = next(item for item in built.items if item.probe)
    ordinary = built.find("TC-SYS-01#1")
    assert ordinary is not None

    assert plan.expected_statuses(probe) == (404, 405)
    assert plan.expected_statuses(ordinary) == (200, 201, 202, 204)


def test_parse_operation_keeps_path_with_parameters():
    """Разбор строки операции проверки: метод в верхнем регистре, путь — как есть."""
    assert plan.parse_operation("get /api/data/files") == ("GET", "/api/data/files")
    assert plan.parse_operation("delete /api/loads/{load_id}") == ("DELETE", "/api/loads/{load_id}")
    assert plan.parse_operation("мусор") == ("", "мусор")


def test_item_helpers_expose_operation_and_destructive_flag():
    """Подпись операции и признак удаляющего вызова — для предупреждений в UI."""
    item = plan.PlanItem(
        item_id="TC-LOAD-07#1",
        level=plan.LEVEL_CALL,
        title="удаление нагрузки",
        method="DELETE",
        path="/api/loads/{load_id}",
        safety=str(ep.Safety.DESTRUCTIVE),
    )

    assert item.operation == "DELETE /api/loads/{load_id}"
    assert item.is_destructive is True
    assert item.is_done is False
    assert item.icon == "⚪"


def test_plan_item_round_trip_keeps_params():
    """Параметры вызова (то, что оператор поправил руками) не теряются."""
    item = plan.PlanItem(
        item_id="TC-DS-01#2",
        level=plan.LEVEL_CALL,
        title="создание датасета",
        method="POST",
        path="/api/datasets/",
        body_kind=ep.BODY_JSON,
        params={"body": '{"name": "__TEST__dataset"}'},
    )

    restored = plan.PlanItem.from_dict(item.to_dict())

    assert restored.params["body"] == '{"name": "__TEST__dataset"}'
    assert restored.plan_key == "TC-DS-01_2"
    assert restored.operation == "POST /api/datasets/"


def test_unknown_payload_falls_back_to_defaults():
    """Неизвестные/битые данные пункта не ломают восстановление плана."""
    state = plan.PlanState.from_dict({"mode": "чужой", "pause_seconds": "abc", "items": [{}]})

    assert state.mode == plan.MODE_CHECK
    assert state.pause_seconds == plan.DEFAULT_PAUSE_SECONDS
    assert state.items[0].item_id == "PLAN-?"
    assert state.items[0].status == plan.STATUS_PENDING


def test_plan_state_accessors_are_consistent():
    """`find`, `index_of`, `replace_item` работают согласованно."""
    state = plan.build_plan([catalog.CHECKS[0]])
    item = state.items[0]

    assert state.index_of(item.item_id) == 0
    assert state.find(item.item_id) is item
    state.replace_item(item.mark(plan.STATUS_SKIPPED))

    assert state.find(item.item_id).status == plan.STATUS_SKIPPED
    assert state.index_of("нет-такого") == -1
    assert state.replace_item(item) is None


def test_plan_item_serializes_to_plain_dict():
    """Пункт — обычный dataclass: сериализация даёт словарь для JSON-файла сессии."""
    item = plan.build_plan().items[0]
    payload: dict[str, Any] = item.to_dict()

    assert payload["item_id"] == item.item_id
    assert isinstance(payload["params"], dict)
    assert set(payload) >= {"enabled", "status", "verdict", "expected"}
