"""Тесты view-моделей пульта: подписи, фазы, снимки, протокол, лента и итоги.

Эти функции — единственное место, где считаются подписи, проценты и разница снимков
(`llm_tasks/31.4` §3: JS ничего не считает). Тесты переносят покрытие прежнего
`test_ui_components.py`, потерявшего смысл при удалении Streamlit: логика осталась,
«рисующих» вызовов больше нет.
"""

from __future__ import annotations

from typing import Any

from acceptance import results as results_api
from acceptance import run_card as run_card_api
from acceptance import run_state
from acceptance import session as session_api
from acceptance import sets as sets_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckClass, CheckStatus
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


def test_journal_body_notes_explain_empty_bodies() -> None:
    """Пустое тело объясняется словами, а не подозрением в потере данных (`FR-P-21`).

    Дефект 29.09.2026: оператор видел «(тело не сохранилось)» у обычного `GET`, у
    которого тела и не было. Подпись считает сервер — он один знает метод, размер,
    тип содержимого и настройку `PULT_LOG_BODIES`.
    """
    empty = view.journal_row(
        _record(request_bytes=0, response_bytes=0, request_body="", response_body="")
    )
    assert empty["request_note"] == "тела нет: запрос без тела (GET/HEAD)"
    assert empty["response_note"] == "тела нет: ответ без тела (204 или пустой ответ)"

    post = view.journal_row(
        _record(method="post", request_bytes=0, request_body="", response_body="{}")
    )
    assert post["request_note"] == "тела нет: запрос отправлен без тела"

    filled = view.journal_row(_record())
    assert filled["request_note"] == "" and filled["response_note"] == ""

    binary = view.journal_row(
        _record(
            request_bytes=4096,
            request_body="",
            content_type="application/octet-stream",
        )
    )
    assert (
        binary["request_note"]
        == "тело не сохраняется: не текстовый формат, 4096 байт (смотрите размер)"
    )

    upload = view.journal_row(
        _record(request_bytes=512, request_body="", content_type="multipart/form-data; boundary=x")
    )
    assert "multipart/form-data" in upload["request_note"]
    assert "512 байт" in upload["request_note"]

    off = view.journal_row(_record(request_bytes=64, request_body=""), bodies_logged=False)
    assert off["request_note"] == (
        "тело не сохранялось: журналирование тел выключено (`PULT_LOG_BODIES`)"
    )

    cut = view.journal_row(_record(body_truncated=True), body_limit=4096)
    assert cut["request_note"] == "тело обрезано по лимиту 4096 символов"


def test_results_page_labels_are_built_in_python() -> None:
    """Пункты полноты отчёта и сводка данных стенда собирает сервер (`FR-P-48`, `UC-03`).

    Проверяем именно то, что интерфейс получает готовым: подписи, значения и «в порядке ли
    пункт». Браузер не решает, что считать готовностью отчёта (архитектура B).
    """
    stats: dict[str, Any] = {
        "checks": {
            "implemented": 69,
            "done": 12,
            "passed": 10,
            "failed": 1,
            "blocked": 1,
            "not_run": 57,
        },
        "notes": {"total": 3, "p0": 1, "p1": 2, "incomplete": 1},
        "tasks": {"observed": 4},
        "artifacts": {"total": 5, "kinds": {"json": 4, "md": 1}},
        "test_entities": {"total": 2, "pending": 1},
        "session": {"filled": True, "status": "идёт", "duration_seconds": 10, "build": "dev@1"},
    }
    points = {
        item["label"]: item
        for item in view.report_points(stats, snapshots={"taken": {"start": True, "end": False}})
    }
    assert points["Проверок выполнено"]["value"] == "12 из 69"
    assert points["Проверок выполнено"]["ok"] is False, "непройденные пункты не «готовы»"
    assert points["Проверок с отказом"]["ok"] is False
    assert points["Снимки стенда «Начало» / «Окончание»"]["value"] == "есть / нет"
    assert points["Замечания к API"]["value"] == "всего 3 · P0 1 · P1 2 · без воспроизведения 1"
    assert points["Артефактов в сессии"]["value"] == "5 · json 4, md 1"
    assert points["`__TEST__`-сущности"]["ok"] is False

    clean: dict[str, Any] = dict(stats)
    clean["checks"] = dict(stats["checks"])
    clean["checks"].update({"done": 69, "failed": 0, "blocked": 0, "not_run": 0})
    clean["notes"] = {"total": 0, "p0": 0, "p1": 0, "incomplete": 0}
    clean["test_entities"] = {"total": 1, "pending": 0}
    assert all(
        item["ok"]
        for item in view.report_points(clean, snapshots={"taken": {"start": True, "end": True}})
    )

    summary = view.data_summary(
        {
            "files": [1, 2],
            "records": [1],
            "records_attention": 1,
            "loads": [],
            "datasets": [1, 2, 3],
            "errors": {"files": "", "loads": "нет связи со стендом", "datasets": ""},
        }
    )
    assert [item["value"] for item in summary] == ["2", "1", "1", "0", "3", "1"]
    assert [item["label"] for item in summary][0] == "файлов"

    options = view.status_options()
    assert options and all(item["value"] and item["label"] for item in options)
    assert view.option_rows(["System", "", "System"]) == [
        {"value": "System", "label": "System"},
    ]


def test_report_view_carries_points_from_snapshots() -> None:
    """`report_view` добавляет пункты и видит снимки сессии (`FR-P-48`)."""
    session = _session()
    session_api.set_snapshot(session, session_api.SNAP_START, {"counts": {"files": 1}})
    readiness = {
        "checks": {"implemented": 69, "done": 0, "passed": 0, "failed": 0, "not_run": 69},
        "notes": {"total": 0, "p0": 0, "p1": 0, "incomplete": 0},
        "tasks": {"observed": 0},
        "artifacts": {"total": 0},
        "test_entities": {"total": 0, "pending": 0},
        "session": {"filled": True},
        "warnings": ["нет снимка стенда «окончание»"],
        "ready": False,
    }
    result = view.report_view(readiness, snapshots=view.snapshot_view(session))
    assert result["ready"] is False and result["warnings"] == ["нет снимка стенда «окончание»"]
    assert result["stats"]["checks"]["implemented"] == 69
    shots = {item["label"]: item for item in result["points"]}
    assert shots["Снимки стенда «Начало» / «Окончание»"]["value"] == "есть / нет"


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


def test_class_options_cover_registry() -> None:
    """Фильтр классов приходит с сервера: ключи реестра и человекочитаемые подписи."""
    options = view.class_options()
    keys = [item["key"] for item in options]
    assert keys == [str(item) for item in CheckClass]
    assert all(item["label"] and item["label"] != item["key"] for item in options)


def test_set_rows_include_composition_with_titles() -> None:
    """Состав набора отдаётся строками с названиями: страница только рисует (`SCR-101`)."""
    library = sets_api.library_from_catalog(author="Петров П.П.")
    rows = view.sets_rows(library)
    assert rows, "библиотека из каталога должна содержать наборы"
    row = next(item for item in rows if item["items"])
    first = row["items"][0]
    assert first["order"] == 1
    assert first["check_id"] == row["check_ids"][0]
    assert first["title"], "название проверки берётся из каталога"
    assert first["check_class"]
    assert first["in_catalog"] is True
    assert isinstance(first["mandatory"], bool)


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


def test_scenario_labels_name_how_check_runs() -> None:
    """Способ выполнения проверки (`M0`): сценарий, «по вызовам» или ручная отметка."""
    assert view.scenario_label("TC-SYS-01") == view.SCENARIO_RUN
    assert view.scenario_label("TC-MOD-01") == view.SCENARIO_CALL
    assert view.scenario_label("TC-CLEAN-04") == view.SCENARIO_OPERATOR
    assert view.scenario_label("TC-NOPE-01") == ""

    assert view.scenario_note("TC-SYS-01") == "", "у автоматизированной проверки — без оговорок"
    assert "сценарий проверки ещё не описан" in view.scenario_note("TC-MOD-01")
    assert "«по вызовам»" in view.scenario_note("TC-MOD-01")
    assert "ручная" in view.scenario_note("TC-CLEAN-04")
    assert view.scenario_note("TC-NOPE-01") == ""


def test_catalog_rows_cover_full_programme_with_run_modes() -> None:
    """Каталог отдаёт все 69 проверок программы и способ выполнения каждой (M0)."""
    rows = view.catalog_rows()
    modes = {
        mode: sum(1 for row in rows if row["scenario"] == mode)
        for mode in (view.SCENARIO_RUN, view.SCENARIO_CALL, view.SCENARIO_OPERATOR)
    }

    assert len(rows) == catalog.catalog_summary()["checks_total"] == 69
    assert all(row["scenario"] for row in rows), "подпись способа есть у каждой строки"
    assert modes == {view.SCENARIO_RUN: 45, view.SCENARIO_CALL: 20, view.SCENARIO_OPERATOR: 4}
    assert view.catalog_rows(only_section="TC-CLEAN")[0]["scenario"] == view.SCENARIO_CALL


# ---------------------------------------------------------------------------
# Прогон: готовность, карточка запуска, payload, отметка и детали (`SCR-301`)
# ---------------------------------------------------------------------------
def _programme_from_sets(*set_ids: str, approved: bool = False) -> Programme:
    """Программа сессии из наборов каталога (утверждение — с обоснованием сокращения)."""
    library = sets_api.library_from_catalog(author="Иванов И.И.")
    programme = Programme.from_sets(library, list(set_ids) or ["SET-SYS"])
    if approved:
        programme.approve(by="Иванов И.И.", comment="приёмка", reduction="срез приёмки")
    return programme


def test_readiness_explains_every_obstacle() -> None:
    """Одно правило «можно запускать»: код причины и панель, где её устраняют (`IR-P-8`)."""
    no_session = view.readiness_view(None, Programme(), queue_for_revision(Programme(), 0))
    assert no_session["blocked_by"] == "no-session"
    assert no_session["fix"]["panel"] == "sessions"

    programme = _programme_from_sets("SET-SYS")
    queue = queue_for_revision(programme, 0)
    no_stand = view.readiness_view(_session(), programme, queue, stand_configured=False)
    assert no_stand["blocked_by"] == "no-stand"
    assert no_stand["fix"]["label"]

    empty = view.readiness_view(_session(), Programme(), queue_for_revision(Programme(), 0))
    assert empty["blocked_by"] == "no-programme" and empty["fix"]["panel"] == "planning"

    draft = view.readiness_view(_session(), programme, queue)
    assert draft["runnable"] is False
    assert draft["blocked_by"] == "not-approved"
    assert "не утверждена" in draft["reason"]
    assert draft["fix"]["label"] == "Утвердить программу"


def test_readiness_allows_approved_programme_and_reports_finish() -> None:
    """Утверждённая ревизия запускается; закрытые пункты оставляют только протокол."""
    session = _session()
    programme = _programme_from_sets("SET-SYS", approved=True)
    ready = view.readiness_view(session, programme, queue_for_revision(programme, 0))
    assert ready["runnable"] is True and ready["reason"] == "" and ready["blocked_by"] == ""
    assert "утверждена" in ready["notice"]

    for item in programme.items:
        results_api.record(session, {"check_id": item.check_id, "status": "успех", "verdict": "ок"})
    queue = run_state.run_queue(session, programme)
    finished = view.readiness_view(session, programme, queue)
    assert finished["blocked_by"] == "finished"
    assert finished["fix"]["panel"] == "protocol"

    header = view.header_view(
        stand_configured=True,
        base_url="https://stand.local",
        session=session,
        programme=programme,
        queue=queue,
    )
    assert header["next"]["runnable"] is False
    assert header["next"]["reason"] == finished["reason"], "шапка и экран прогона — одно правило"
    assert header["next"]["fix"] == finished["fix"]


def test_modes_view_uses_session_flag() -> None:
    """Режим прогона: «только невыполненное» по умолчанию, «всё подряд» — по флагу."""
    session = _session()
    modes = view.modes_view(session)
    assert modes["current"] == run_state.RUN_ONLY_REMAINING
    assert [item["value"] for item in modes["options"]] == list(run_state.RUN_MODES)
    assert all(item["hint"] for item in modes["options"])

    assert run_state.set_run_mode(session, run_state.RUN_ALL) == run_state.RUN_ALL
    assert session.skip_done is False
    assert view.modes_view(session)["current"] == run_state.RUN_ALL

    assert run_state.set_run_mode(session, "чепуха") == run_state.RUN_ONLY_REMAINING
    assert session.skip_done is True


def test_run_card_view_shows_missing_until_confirmed() -> None:
    """Карточка запуска `live`: заготовки заполнены, подтверждение расходов — за оператором."""
    item = _item(check_id="TC-FILE-11", check_class=str(CheckClass.LIVE), title="Выгрузка файла")
    card = view.run_card_view(item, session_id="20260925-120000-0001", author="Петров П.П.")
    assert card["required"] is True and card["note"]
    fields = {field["name"]: field for field in card["fields"]}
    assert fields["goal"]["value"].startswith("TC-FILE-11")
    assert fields["responsible"]["value"] == "Петров П.П."
    assert fields["confirm"]["kind"] == "checkbox" and fields["confirm"]["checked"] is False
    assert card["filled"] is False and card["missing"]
    assert [field["name"] for field in card["fields"]][:3] == list(run_card_api.REQUIRED)

    saved = view.run_card_view(
        item,
        values={
            "goal": "проверить",
            "data": "__TEST__",
            "responsible": "Петров П.П.",
            "confirm": True,
        },
    )
    assert saved["filled"] is True and saved["missing"] == []
    assert saved["saved"] is True


def test_payload_view_shows_test_data_and_silence_for_read_only() -> None:
    """Предпросмотр отправляемого: у пишущей проверки видно `__TEST__`, у читающей — нет."""
    session = _session()
    writing = view.payload_view("TC-FILE-11", session=session)
    assert writing["count"] >= 1
    assert any("__TEST__" in line for line in writing["lines"])
    assert writing["summary"]

    reading = view.payload_view("TC-SYS-01", session=session)
    assert reading["count"] == 0
    assert "ничего не создаёт" in reading["lines"][0]
    assert view.payload_view("TC-НЕТ-ТАКОЙ") == {"summary": "", "lines": [], "count": 0}


def test_mark_view_offers_operator_statuses() -> None:
    """Форма ручной отметки: статусы оператора и обязательное заключение (`FR-P-32`)."""
    manual = view.mark_view(_item(check_id="TC-SYS-06", check_class=str(CheckClass.MANUAL)))
    assert manual["required"] is True and manual["note"]
    values = [item["value"] for item in manual["statuses"]]
    assert str(CheckStatus.MANUAL_OK) in values
    assert str(CheckStatus.SKIPPED) in values
    assert manual["note_hint"]
    assert manual["default_status"] == str(CheckStatus.MANUAL_OK)

    assert view.mark_view(_item())["required"] is False


def test_batch_view_counts_pending_tech_items() -> None:
    """«▶▶ Пачка tech» знает, сколько пунктов заберёт (`A4`)."""
    programme = _programme_from_sets("SET-SYS")
    queue = queue_for_revision(programme, 0)
    batch = view.batch_view(queue, programme)
    assert batch["count"] == len(batch["ids"])
    assert batch["enabled"] is bool(batch["ids"])
    assert batch["count"] == 0 or batch["label"] == f"▶▶ Пачка tech ({batch['count']})"
    for check_id in batch["ids"]:
        found = programme.find(check_id)
        assert found is not None and found.check_class == str(CheckClass.TECH)


def test_run_rows_carry_steps_and_payload_details() -> None:
    """Раскрытие пункта приходит готовым: шаги и payload считает Python (`FR-P-20`)."""
    session = _session()
    programme = _programme_from_sets("SET-SYS", approved=True)
    queue = run_state.run_queue(session, programme)
    rows = view.run_rows(session, programme, queue)
    assert rows and all("details" in row for row in rows)
    row = rows[0]
    assert row["details"]["steps"], "шаги проверки берутся из каталога"
    assert "summary" in row["details"]["payload"]
    assert row["confirm_required"] is False and row["operator_required"] is False


def test_next_view_describes_confirmable_item() -> None:
    """Следующий пункт до нажатия «Следующая»: требования, шаги и payload (`FR-P-19`)."""
    item = _item(check_id="TC-FILE-11", check_class=str(CheckClass.LIVE), title="Выгрузка файла")
    described = view.run_next_view(item, session=_session(), author="Петров П.П.")
    assert described["check_id"] == "TC-FILE-11"
    assert described["confirm_required"] is True and described["confirm_note"]
    assert described["operator_required"] is False
    assert described["steps"] and described["payload"]["count"] >= 1
    assert described["card"]["required"] is True
    assert view.run_next_view(None) == {}, "без пункта описания нет"


def test_outcome_rows_hint_about_card_and_mark() -> None:
    """Итог шага объясняет, почему запрос не ушёл: карточка запуска или отметка (`FR-P-19`)."""

    class _Card:
        check_id = "TC-FILE-11"
        ok = False
        icon = "⛔"
        state = "ожидает"
        status = ""
        verdict = ""
        detail = "нужна карточка запуска"
        journal_range = ""
        stopped = True
        stop_reason = "TC-FILE-11: нужна карточка запуска"
        duration_ms = 0.0
        steps = 0
        confirmation_required = True
        operator_required = False

    class _Mark:
        check_id = "TC-SYS-06"
        ok = False
        icon = "⛔"
        state = "ожидает"
        status = ""
        verdict = ""
        detail = "нужна отметка"
        journal_range = ""
        stopped = True
        stop_reason = ""
        duration_ms = 0.0
        steps = 0
        confirmation_required = False
        operator_required = True

    card_row = view.outcome_rows([_Card()])[0]
    assert card_row["confirmation_required"] is True
    assert "карточку запуска" in card_row["hint"]

    mark_row = view.outcome_rows([_Mark()])[0]
    assert mark_row["operator_required"] is True
    assert "отметку" in mark_row["hint"]


def test_next_view_warns_about_missing_scenario() -> None:
    """Следующая проверка без сценария приходит со способом прогона и предупреждением.

    Оператор видит это **до** нажатия «▶ Следующая»: иначе отказ ядра выглядел бы
    как необъяснимая ошибка (`M0`, группы T6–T9).
    """
    item = _item("TC-MOD-01", check_class="tech", title="Каталог архитектур")

    next_view = view.run_next_view(item)

    assert next_view["scenario"] == view.SCENARIO_CALL
    assert "сценарий проверки ещё не описан" in next_view["scenario_note"]
    assert next_view["confirm_required"] is False
    assert next_view["steps"] and next_view["payload"], "что вызывать — известно уже сейчас"


def test_outcome_rows_hint_about_missing_scenario() -> None:
    """Итог шага объясняет, что делать с проверкой без сценария (`M0`)."""

    class _NoScenario:
        check_id = "TC-MOD-01"
        ok = False
        icon = ""
        state = "ожидает"
        status = ""
        verdict = ""
        detail = "TC-MOD-01: сценарий проверки ещё не описан"
        journal_range = ""
        stopped = True
        stop_reason = "TC-MOD-01: сценарий проверки ещё не описан"
        duration_ms = 0.0
        steps = 0
        confirmation_required = False
        operator_required = False
        scenario_missing = True

    row = view.outcome_rows([_NoScenario()])[0]
    assert row["scenario_missing"] is True
    assert "«по вызовам»" in row["hint"]
    assert row["stop_reason"], "причина останова видна оператору"


def test_readiness_reports_queue_built_from_another_revision() -> None:
    """Очередь от другого состава — тоже препятствие: пульт не запускает пункт «вслепую».

    Ветка страховочная: обычно утверждение ревизии переводит сессию на неё
    (`session.run_revision`), но если очередь посчитана по прежнему составу, результат
    оказался бы привязан не к той ревизии программы (`DR-P-14`).
    """
    session = _session()
    programme = _programme_from_sets("SET-SYS", approved=True)
    session.run_revision = programme.revision
    programme.revise(author="Иванов И.И.", comment="правка состава")
    programme.rebuild(sets_api.library_from_catalog(author="Иванов И.И."), [sets_api.SMOKE_SET_ID])
    programme.approve(by="Иванов И.И.", comment="приёмка", reduction="срез приёмки")
    assert session.run_revision < programme.revision

    queue = run_state.run_queue(session, programme)
    blocked = view.readiness_view(session, programme, queue)
    assert blocked["runnable"] is False
    assert blocked["blocked_by"] == "queue"
    assert blocked["fix"]["panel"] == "planning"
    assert str(session.run_revision) in blocked["reason"], "причина называет ревизию очереди"


def test_dataset_and_composition_rows() -> None:
    """Датасет без состава и с составом подписывается по-разному (`SCR-204`)."""
    rows = view.dataset_rows([{"id": "d1"}])
    assert rows[0]["composition"] is None

    empty = view.composition_view(None)
    assert empty["source_label"] == "состав недоступен"
    assert empty["trusted"] is False
