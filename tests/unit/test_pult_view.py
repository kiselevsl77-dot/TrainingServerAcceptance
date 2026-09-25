"""Тесты view-моделей пульта: подписи, фазы, снимки, протокол, лента и итоги.

Эти функции — единственное место, где считаются подписи, проценты и разница снимков
(`llm_tasks/31.4` §3: JS ничего не считает). Тесты переносят покрытие прежнего
`test_ui_components.py`, потерявшего смысл при удалении Streamlit: логика осталась,
«рисующих» вызовов больше нет.
"""

from __future__ import annotations

from acceptance import results as results_api
from acceptance import run_state
from acceptance import session as session_api
from acceptance.checks import catalog
from acceptance.http_log import HttpExchange
from acceptance.programme import Programme, ProgrammeItem
from acceptance.queue import Queue, QueueItem, queue_for_revision
from acceptance.session import SNAP_END
from acceptance.session import TestSession as SessionModel
from pult import view


def _session(*, status: str = session_api.STATUS_RUNNING, build: str = "") -> SessionModel:
    """Сессия для проверок: без стенда и без программы."""
    session = SessionModel(session_id="20260925-120000-0001")
    session.status = status
    if build:
        session.server_version = {"branch": build.split("@")[0], "revision": build.split("@")[-1]}
    return session


def _item(
    check_id: str = "TC-SYS-01",
    *,
    order: int = 1,
    check_class: str = "tech",
    title: str = "Здоровье сервера",
) -> ProgrammeItem:
    """Пункт программы для строк прогона и протокола."""
    return ProgrammeItem(
        check_id=check_id,
        order=order,
        check_class=check_class,
        title=title,
        module="system",
        group="System",
        requirement="FR-T1",
        expected="200 OK",
    )


def _queue(*items: QueueItem, mode: str = "сценарием") -> Queue:
    """Очередь с заданными пунктами (для маркеров и процентов)."""
    queue = Queue(revision=1, approved=True, items=list(items))
    queue.set_mode(mode)
    return queue


def _record(**overrides: object) -> HttpExchange:
    """Запись журнала обмена с осмысленными значениями по умолчанию."""
    payload: dict[str, object] = {
        "seq": 7,
        "started_at": "2026-09-25T12:03:04",
        "method": "get",
        "path": "/api/data/files",
        "query": "limit=10",
        "status": 200,
        "duration_ms": 12.5,
        "request_bytes": 0,
        "response_bytes": 2048,
        "content_type": "application/json",
        "request_body": '{"a": 1}',
        "response_body": '{"files": []}',
        "label": "TC-FILE-01",
    }
    payload.update(overrides)
    return HttpExchange(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Типографика и подписи
# ---------------------------------------------------------------------------
def test_typography_helpers() -> None:
    """Склейка частей, склонения, проценты, короткое время и пустые значения."""
    assert view.join_parts("ревизия 2", "", None, "утверждена") == "ревизия 2 · утверждена"
    assert view.plural(1, ("пункт", "пункта", "пунктов")) == "1 пункт"
    assert view.plural(3, ("пункт", "пункта", "пунктов")) == "3 пункта"
    assert view.plural(11, ("пункт", "пункта", "пунктов")) == "11 пунктов"
    assert view.percent(34, 41) == "34 из 41 · 83 %"
    assert view.percent(0, 0) == "нет пунктов"
    assert view.short_time("2026-09-21T10:12:00") == "21.09 10:12"
    assert view.short_time("2026-09-21T10:12:00", "%H:%M:%S") == "10:12:00"
    assert view.short_time("не время") == "не время"
    assert view.short_time(None) == ""
    assert view.cell(None) == ""
    assert view.cell(0) == "0"


def test_labels_for_status_markers_and_classes() -> None:
    """Подписи статусов, маркеров, классов и происхождения результата."""
    assert view.status_icon("успех") == "✅"
    assert view.status_icon("неизвестный") == "⚪"
    assert view.status_labels()[0] == "отказ"
    assert view.queue_state_label("снята") == "снята"
    assert view.queue_state_label("мусор") == view.queue_state_label("ожидает")
    assert view.queue_state_icon("выполнена") == "✅"
    assert view.marker_text("следующая") == "▷ следующая"
    assert view.marker_text("") == ""
    assert view.origin_label("прогон") == "пульт"
    assert view.origin_label("по запросу") == "запрос сессии"
    assert view.origin_label("прогон", suspended=True) == "оператор"
    assert view.class_label("live") == "боевая (реальные данные)"
    assert view.is_confirmable("live") is True
    assert view.is_confirmable("tech") is False
    assert view.is_manual("manual") is True
    assert view.result_text({"status": "успех", "verdict": "соответствует ожиданию"}) == (
        "✅ успех · соответствует ожиданию"
    )


def test_build_warning_only_on_mismatch() -> None:
    """Расхождение сборок не молчит, совпадение — молчит (`AC-P-1`)."""
    session = _session(build="dev@111")
    assert session.server_build == "dev@111"
    assert view.build_warning(session, "dev@111") == ""
    assert view.build_warning(session, "") == ""
    warning = view.build_warning(session, "dev@222")
    assert "dev@222" in warning and "dev@111" in warning


# ---------------------------------------------------------------------------
# Фаза процесса
# ---------------------------------------------------------------------------
def _programme(*check_ids: str, approved: bool = True) -> Programme:
    """Программа из указанных проверок: черновик или утверждённая ревизия."""
    programme = Programme(
        items=[_item(check_id, order=index) for index, check_id in enumerate(check_ids, start=1)],
        revision=1,
    )
    if approved:
        programme.approve(by="Петров П.П.")
    return programme


def test_phase_without_session_and_programme() -> None:
    """Без сессии и без утверждённой программы фаза — подготовка (`Ф0`)."""
    empty = Programme()
    assert view.phase_of(None, empty, None, queue_for_revision(empty)) == "Ф0"
    assert view.phase_of(_session(), empty, None, queue_for_revision(empty)) == "Ф0"
    draft = _programme("TC-SYS-01", approved=False)
    assert view.phase_of(_session(), draft, None, queue_for_revision(draft)) == "Ф0"


def test_phase_run_fix_and_finish() -> None:
    """Фазы прогона, разбора и финализации различаются по состоянию сессии (`docs/14`)."""
    programme = _programme("TC-SYS-01", "TC-SYS-02")
    queue = queue_for_revision(programme)
    assert view.phase_of(_session(), programme, {}, queue) == "Ф1"  # пункты не закрыты

    queue.finish("TC-SYS-01")
    queue.finish("TC-SYS-02")
    assert (
        view.phase_of(_session(), programme, {}, queue) == "Ф2"
    )  # прогон пройден, «Окончания» нет

    failed = {item.check_id: {"status": "отказ"} for item in programme.items}
    assert view.phase_of(_session(), programme, failed, queue) == "Ф3"

    done = _session()
    done.snapshots[SNAP_END] = {"at": "2026-09-25T18:00:00", "counts": {}}
    assert view.phase_of(done, programme, {}, queue) == "Ф4"

    closed = _session(status=session_api.STATUS_CLOSED)
    assert view.phase_of(closed, programme, {}, queue) == "Ф4"


# ---------------------------------------------------------------------------
# Снимки стенда
# ---------------------------------------------------------------------------
def test_counters_and_delta_rows() -> None:
    """Счётчики снимка и разница «до/после» считаются и подписываются (`DR-P-8`)."""
    start = {
        "counts": {"files": 4, "records": 2, "loads": 1},
        "files": {"total_bytes": 2_500_000_000, "duplicate_names": 1},
    }
    rows = view.counters_rows(start)
    assert any(row["value"] == "4" for row in rows)
    assert any("нет данных" == row["value"] for row in rows)
    volume = next(row for row in rows if row["counter"].startswith("Объём"))
    assert "2.50 ГБ" in volume["value"] and "дублей имён: 1" in volume["value"]

    end = {"counts": {"files": 6, "records": 2, "loads": 0}}
    delta = view.delta_rows(start, end)
    by_name = {row["counter"]: row for row in delta}
    assert by_name["Записи (RAW + markup)"]["delta"] == "0"

    files_delta = next(row for row in delta if row["delta"] == "+2")
    assert files_delta["before"] == "4" and files_delta["after"] == "6"
    assert view.delta_rows({}, {})[0]["before"] == "—"

    csv = view.delta_csv(delta)
    assert csv.startswith("Реестр;Начало;Окончание;Разница")
    assert len(csv.strip().splitlines()) == len(delta) + 1


def test_snapshot_view_and_partial_errors() -> None:
    """Подписи «Начало/Окончание» и честная пометка частичного снимка (`FR-P-6`)."""
    session = _session()
    empty = view.snapshot_view(session)
    assert [label["taken"] for label in empty["labels"]] == [False, False]
    assert empty["errors"] == ""

    session.snapshots[SNAP_END] = {
        "at": "2026-09-25T18:00:00",
        "counts": {"files": 6},
        "errors": {"models": "503"},
    }
    data = view.snapshot_view(session)
    assert data["taken"] == {"start": False, "end": True}
    assert "частичный" in data["errors"] and "models: 503" in data["errors"]
    assert data["labels"][1]["at"] == "25.09 18:00"


# ---------------------------------------------------------------------------
# Прогон, протокол и лента обмена
# ---------------------------------------------------------------------------
def _run_case() -> tuple[SessionModel, Programme, Queue]:
    """Сессия с записанным результатом первой проверки и очередью прогона (`v8`).

    Очередь строится тем же путём, что и на сервере (`run_state.run_queue`): состояние
    пунктов восстанавливается из результатов сессии, а не хранится в очереди.
    """
    programme = _programme("TC-SYS-01", "TC-SYS-02")
    session = _session()
    results_api.record(
        session,
        {"check_id": "TC-SYS-01", "status": "успех", "verdict": "соответствует ожиданию"},
        revision=programme.revision,
    )
    return session, programme, run_state.run_queue(session, programme, ())


def test_run_rows_show_marker_result_and_origin() -> None:
    """Строка прогона показывает маркер, состояние, результат и способ получения (`SCR-301`)."""
    session, programme, queue = _run_case()
    first, second = view.run_rows(session, programme, queue)

    assert first["check_id"] == "TC-SYS-01"
    assert first["status"] == "успех"
    assert first["result"] == "✅ успех · соответствует ожиданию"
    assert first["origin"] == "пульт"
    assert first["title"] == "Здоровье сервера"
    assert first["state"] == "выполнена"
    assert second["check_id"] == "TC-SYS-02"
    assert second["confirm_required"] is False
    assert second["marker"] or second["queue_state"]


def test_run_rows_mark_confirmed_by_request() -> None:
    """Пункт, закрытый по факту запроса, помечается и не выдаётся за запуск (`UC-03`)."""
    programme = _programme("TC-FILE-01")
    session = _session()
    results_api.record(
        session,
        {
            "check_id": "TC-FILE-01",
            "status": "успех",
            "verdict": "подтверждено ранее выполненным запросом",
            "evidence": {"note": "выполнено по факту запроса"},
        },
        origin="по запросу",
        revision=programme.revision,
    )
    row = view.run_rows(session, programme, queue_for_revision(programme))[0]
    assert row["origin"] == "запрос сессии"
    assert row["done_by"] == "выполнено по факту запроса"


def test_protocol_rows_include_checks_outside_programme() -> None:
    """Протокол показывает пункты программы и результаты «вне программы» (`FR-P-35`)."""
    session, programme, _queue_obj = _run_case()
    outside = next(
        check_id for check_id in catalog.check_ids() if check_id not in {"TC-SYS-01", "TC-SYS-02"}
    )
    results_api.record(
        session, {"check_id": outside, "status": "успех", "verdict": "вручную"}, revision=1
    )

    rows = view.protocol_rows(session, programme)
    assert [row["check_id"] for row in rows[:2]] == ["TC-SYS-01", "TC-SYS-02"]
    extra = next(row for row in rows if row["check_id"] == outside)
    assert extra["in_programme"] is False
    assert extra["section"]

    summary = view.protocol_summary(session, programme)
    assert summary["programme_size"] == 2
    assert summary["closed"] == 1
    assert summary["coverage"] == "1 из 2 · 50 %"
    assert summary["passed"] == 2

    filtered = view.filter_rows(rows, status="успех")
    assert {row["check_id"] for row in filtered} == {"TC-SYS-01", outside}
    assert view.filter_rows(rows, query="TC-SYS-02")[0]["title"] == "Здоровье сервера"

    csv = view.protocol_csv(rows)
    assert csv.startswith("№;Проверка;Название")
    assert "TC-SYS-01" in csv and csv.endswith("\n")
    assert ";да\n" in csv and ";нет\n" in csv  # отметка «в программе»


def test_journal_rows_have_independent_detail_and_curl() -> None:
    """Тела запроса и ответа включаются по отдельности, `curl` собирается (`FR-P-21`)."""
    row = view.journal_row(_record(), base_url="http://test.local")
    assert row["seq"] == 7
    assert row["at"] == "12:03:04"
    assert row["method"] == "GET"
    assert row["ok"] is True
    assert row["duration_ms"] == 12.5
    assert row["sizes"] == "запрос 0 Б · ответ 2.0 КБ"
    assert row["request"] == '{"a": 1}'
    assert row["response"] == '{"files": []}'
    assert "curl -X GET 'http://test.local/api/data/files?limit=10'" in row["curl"]
    assert "-d '{\"a\": 1}'" in row["curl"]

    short = view.journal_row(_record(), detail_response=False)
    assert short["request"] and short["response"] == ""

    rows = view.journal_rows(
        [_record(seq=1), _record(seq=2, status=500, error="отказ")],
        errors_only=True,
    )
    assert [row["seq"] for row in rows] == [2]
    assert rows[0]["ok"] is False and rows[0]["error"] == "отказ"
    assert view.journal_rows([_record(seq=1), _record(seq=2)], limit=1)[0]["seq"] == 2


def test_outcome_rows_keep_stop_reason() -> None:
    """Итог прогона не теряет причину останова и диапазон журнала (`FR-P-30`)."""

    class _Outcome:
        check_id = "TC-SYS-01"
        ok = False
        icon = "❌"
        state = "выполнена"
        status = "отказ"
        verdict = "код 500 вместо 200"
        detail = "ожидалось 200"
        journal_range = "#12–#13"
        stopped = True
        stop_reason = "пункт требует карточки запуска"
        duration_ms = 41.7
        steps = 2

    row = view.outcome_rows([_Outcome()])[0]
    assert row["check_id"] == "TC-SYS-01"
    assert row["ok"] is False
    assert row["journal"] == "#12–#13"
    assert row["stopped"] is True
    assert row["stop_reason"] == "пункт требует карточки запуска"
    assert row["duration_ms"] == 41.7
    assert row["steps"] == 2


def test_catalog_rows_mark_membership_and_confirmable() -> None:
    """Каталог отмечает членство в наборе и программе, класс и карточку запуска (`SCR-101`)."""
    all_rows = view.catalog_rows()
    assert len(all_rows) == len(catalog.check_ids())
    tech = view.catalog_rows(only_class="tech")
    assert tech and all(row["class_key"] == "tech" for row in tech)
    section = tech[0]["section"]
    same_section = view.catalog_rows(only_section=section)
    assert same_section and all(row["section"] == section for row in same_section)

    marked = view.catalog_rows(in_set=["TC-SYS-01"], in_programme=["TC-SYS-02"])
    by_id = {row["check_id"]: row for row in marked}
    assert by_id["TC-SYS-01"]["in_set"] is True
    assert by_id["TC-SYS-02"]["in_programme"] is True
    assert by_id["TC-SYS-01"]["in_programme"] is False


def test_dataset_and_composition_rows() -> None:
    """Датасет без состава и с составом подписывается по-разному (`SCR-204`)."""
    rows = view.dataset_rows([{"id": "d1"}])
    assert rows[0]["composition"] is None

    empty = view.composition_view(None)
    assert empty["source_label"] == "состав недоступен"
    assert empty["trusted"] is False
