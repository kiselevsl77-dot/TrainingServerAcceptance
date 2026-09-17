"""Тесты движка проверок (FR-T4, этап T4).

Проверяется то, что делает проверку воспроизводимой: метка `TC-…` на запросах,
диапазон номеров журнала (`journal_from`…`journal_to`), фиксация результата в
сессии (одна запись на проверку), ручные статусы оператора и требования
подтверждения для классов `live`/`heavy` (NFR-T4, FR-T10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckStatus
from acceptance.http_log import Journal, LoggingTransport, current_label
from acceptance.session import new_session
from client.http import ApiHttpClient

SPEC = catalog.find("TC-TASK-02")
LIVE_SPEC = catalog.find("TC-TASK-01")


def _session():
    """Сессия испытаний для прогонов движка."""
    session = new_session(base_url="http://test.local")
    session.info.operator_fio = "Иванов И.И."
    return session


def _parse(value: str) -> datetime:
    """Разбирает метку времени (структурный журнал отчёта)."""
    return datetime.fromisoformat(value)


@dataclass
class _Card:
    """Карточка запуска в терминах движка (`missing`/`confirmed`/`to_dict`)."""

    goal: str = "проверить очередь"
    data: str = "celery-test"
    responsible: str = "Иванов И.И."
    confirmed: bool = False

    @property
    def missing(self) -> tuple[str, ...]:
        """Незаполненные обязательные поля карточки."""
        return () if (self.goal and self.data and self.responsible) else ("цель проверки",)

    def to_dict(self) -> dict[str, object]:
        """Представление карточки для доказательств."""
        return {
            "goal": self.goal,
            "data": self.data,
            "responsible": self.responsible,
            "confirmed": self.confirmed,
        }


def test_check_run_marks_requests_and_bounds_journal():
    """Метка проверки ставится на запросы, начало диапазона — в результат."""
    journal = Journal(max_records=20)
    journal.next_seq()  # «чужая» запись до проверки
    session = _session()

    with checks_engine.start_check(SPEC, journal=journal, params={"limit": 1}) as run:
        assert current_label() == SPEC.check_id
        result = run.finish(CheckStatus.PASSED, verdict="список получен")

    assert result.check_id == "TC-TASK-02"
    assert result.journal_from == 2
    assert result.journal_to is None  # обменов с меткой проверки не было
    assert result.params == {"limit": 1}
    assert result.started_at and result.ended_at
    assert _parse(result.started_at) <= _parse(result.ended_at)
    assert result.duration_ms is not None and result.duration_ms >= 0

    checks_engine.record_result(session, result)
    assert session.checks[-1]["status"] == str(CheckStatus.PASSED)
    assert [item["event"] for item in session.history][-1] == "check_recorded"
    assert current_label() is None


def test_check_label_recorded_in_journal_with_check_id():
    """Обмены внутри проверки помечаются её меткой — по метке фильтруется журнал."""
    journal = Journal(max_records=20)
    client = ApiHttpClient(
        base_url="http://test.local",
        timeout=5.0,
        transport=LoggingTransport(
            journal,
            inner=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
        ),
    )

    with checks_engine.check_label(SPEC.check_id):
        client.get("/api/tasks/")

    assert journal.records[-1].label == "TC-TASK-02"
    assert checks_engine.journal_bounds(journal, SPEC.check_id) == (1, 1)
    assert checks_engine.journal_bounds(journal, "TC-TASK-99") == (None, None)


def test_record_result_keeps_one_entry_per_check():
    """Повторный прогон проверки перезаписывает результат, а не добавляет новый."""
    session = _session()
    first = checks_engine.mark(session, SPEC, CheckStatus.SKIPPED, verdict="нет данных")
    second = checks_engine.mark(session, SPEC, CheckStatus.PASSED, verdict="список получен")

    assert len(session.checks) == 1
    assert first.status == CheckStatus.SKIPPED
    assert second.status == CheckStatus.PASSED
    assert session.checks[0]["verdict"] == "список получен"


def test_manual_statuses_and_group_stats():
    """Ручные отметки оператора входят в сводку группы проверок."""
    session = _session()
    blocked = checks_engine.mark(
        session,
        SPEC,
        CheckStatus.BLOCKED,
        verdict="дефект API",
        operator_note="зафиксировано замечание",
    )
    manual = checks_engine.mark(
        session, catalog.find("TC-TASK-08"), CheckStatus.MANUAL_OK, operator_note="проверено"
    )

    task_ids = [spec.check_id for spec in catalog.by_group("TC-TASK")]
    stats = checks_engine.group_stats(session, task_ids)

    assert blocked.is_done
    assert manual.icon == "🖐️"
    assert stats["total"] == 8
    assert stats["done"] == 2
    assert stats["blocked"] == 1
    assert stats["not_run"] == 6
    assert CheckStatus.MANUAL_OK in checks_engine.status_options()


def test_overall_stats_cover_whole_checklist():
    """Сводка по программе охватывает все наполненные группы каталога."""
    session = _session()
    checks_engine.mark(session, SPEC, CheckStatus.PASSED, verdict="ок")

    stats = checks_engine.overall_stats(session)

    assert stats["implemented"] == 41
    assert stats["program_total"] == 69
    assert stats["total"] == 41
    assert stats["done"] == 1
    assert stats["passed"] == 1
    assert stats["not_run"] == 40


def test_checklist_rows_filter_by_group_status_and_text():
    """Строки чек-листа фильтруются по группе, статусу, классу и поиску."""
    session = _session()
    checks_engine.mark(session, catalog.find("TC-FILE-01"), CheckStatus.PASSED, verdict="реестр ок")
    checks_engine.mark(
        session, catalog.find("TC-FILE-10"), CheckStatus.BLOCKED, verdict="404 latin-1"
    )

    files_rows = checks_engine.checklist_rows(session, groups=("TC-FILE",))
    assert len(files_rows) == 14
    assert files_rows[0]["journal_range"] == "—"

    blocked = checks_engine.checklist_rows(session, statuses=("блокировано API",))
    assert [row["check_id"] for row in blocked] == ["TC-FILE-10"]
    assert blocked[0]["blocked_by_api"]

    found = checks_engine.checklist_rows(session, text="дубл")
    assert {row["check_id"] for row in found} == {"TC-FILE-07", "TC-REC-02", "TC-REC-06"}

    live = checks_engine.checklist_rows(session, classes=("live",))
    assert len(live) == 6

    pending = checks_engine.checklist_rows(session, groups=("TC-FILE",), only_pending=True)
    assert len(pending) == 12


def test_ensure_defect_note_creates_and_deduplicates_note():
    """Статус «блокировано API» автоматически формирует замечание — один раз (FR-T7)."""
    session = _session()
    spec = catalog.find("TC-FILE-10")
    result = checks_engine.mark(
        session, spec, CheckStatus.BLOCKED, verdict="404 latin-1", evidence={"status": 404}
    )

    created = checks_engine.ensure_defect_note(session, spec, result)
    again = checks_engine.ensure_defect_note(session, spec, result)

    assert created is not None
    assert created["priority"] == "P0"
    assert created["check_id"] == "TC-FILE-10"
    assert "TC-FILE-10" in created["evidence"]
    assert again is None
    assert len(session.notes) == 1

    passed = checks_engine.mark(session, catalog.find("TC-LOAD-02"), CheckStatus.PASSED)
    assert checks_engine.ensure_defect_note(session, catalog.find("TC-LOAD-02"), passed) is None


def test_attach_artifact_links_file_to_result(tmp_path):
    """Артефакт проверки попадает в сессию и в доказательства результата."""
    session = _session()
    spec = catalog.find("TC-FILE-13")
    result = checks_engine.mark(session, spec, CheckStatus.PASSED, verdict="круговой рейс")
    path = tmp_path / "round_trip.csv"
    path.write_text("chunkID;timestamp\n1;2026-01-01\n", encoding="utf-8")

    record = checks_engine.attach_artifact(
        session, result, kind="check-evidence", path=path, note="CSV кругового рейса"
    )

    assert record["name"] == "round_trip.csv"
    assert session.artifacts[-1]["kind"] == "check-evidence"
    stored = checks_engine.result_of(session, "TC-FILE-13")
    assert stored.evidence["artifacts"] == ["round_trip.csv"]


def test_run_checks_runs_only_tech_and_skips_manual():
    """Групповой прогон выполняет только дешёвые проверки класса `tech` с сценарием."""
    session = _session()
    calls: list[str] = []

    def factory(spec):
        """Контекст без стенда: сценарии вернут «пропущена» — вызовы фиксируются."""
        calls.append(spec.check_id)
        return check_runner.AutomationContext(session=session, spec=spec)

    specs = catalog.by_group("TC-LOAD")
    results = checks_engine.run_checks(session, specs, context_factory=factory)

    # TC-LOAD-02/03 — техкласс, но требуют стенда: сценарий отработает «пропущено»,
    # поэтому вызовы есть у всех семи проверок группы (все — `tech`)
    assert [spec.check_id for spec in specs] == calls
    assert len(results) == 7
    assert {str(result.status) for result in results} == {str(CheckStatus.SKIPPED)}

    calls.clear()
    tech_only = catalog.by_group("TC-FILE")
    checks_engine.run_checks(session, tech_only, context_factory=factory)
    assert "TC-FILE-11" not in calls  # класс `live` не запускается групповой кнопкой
    assert "TC-FILE-01" in calls


def test_results_map_and_result_of_tolerate_missing_entries():
    """Результаты читаются по `check_id`, отсутствующие — статус «не выполнена»."""
    session = _session()
    checks_engine.mark(session, SPEC, CheckStatus.PASSED, verdict="ок")
    session.checks.append({"check_id": "TC-?-01", "status": "странный статус"})
    session.checks.append({"status": "нет идентификатора"})

    results = checks_engine.results_map(session)

    assert results["TC-TASK-02"].status == CheckStatus.PASSED
    assert results["TC-?-01"].status == CheckStatus.NOT_RUN
    assert len(results) == 3
    assert checks_engine.result_of(session, "TC-TASK-05").status == CheckStatus.NOT_RUN


def test_confirmation_required_only_for_live_and_heavy():
    """Подтверждение требуется для `live`/`heavy` и фиксируется в доказательствах."""
    assert checks_engine.check_confirmation(SPEC) == (True, "", {})
    assert checks_engine.check_confirmation(LIVE_SPEC)[0] is False
    assert "подтверждением" in checks_engine.check_confirmation(LIVE_SPEC)[1]

    card = _Card(confirmed=False)
    ready, reason, evidence = checks_engine.check_confirmation(LIVE_SPEC, card)
    assert not ready
    assert "FR-T10" in reason
    assert evidence["confirmed"] is False

    card.confirmed = True
    ready, reason, evidence = checks_engine.check_confirmation(LIVE_SPEC, card)
    assert ready and not reason
    assert "подтверждён" in str(evidence["confirmation"])


def test_confirmation_lists_missing_card_fields():
    """Незаполненная карточка запуска не пропускает проверку класса `live`."""
    card = _Card(goal="", confirmed=True)

    ready, reason, _ = checks_engine.check_confirmation(LIVE_SPEC, card)

    assert not ready
    assert "цель проверки" in reason


def test_snapshot_keeps_run_state_for_next_rerun():
    """Ассистируемая проверка хранит начало прогона обычными данными (не контекстом)."""
    journal = Journal(max_records=20)
    journal.next_seq()

    state = checks_engine.snapshot(LIVE_SPEC, journal=journal, params={"duration": 2})

    assert state["label"] == "TC-TASK-01"
    assert state["journal_from"] == 2
    assert state["params"] == {"duration": 2}
    assert _parse(str(state["started_at"])) <= datetime.now()


def test_check_run_without_journal_is_safe():
    """Проверка без журнала (юнит-сценарий) не падает и не выдумывает диапазон."""
    with checks_engine.start_check(SPEC) as run:
        result = run.finish(CheckStatus.PASSED, verdict="ок")

    assert result.journal_from is None
    assert result.journal_to is None
    assert checks_engine.journal_start(None) is None


def test_exit_does_not_swallow_exceptions():
    """Контекст проверки закрывается, но исключения не подавляет."""
    run = checks_engine.start_check(SPEC)
    with run:
        pass
    assert run.finished
    run.close()  # повторное закрытие безопасно

    try:
        with checks_engine.start_check(SPEC):
            raise RuntimeError("сбой сценария")
    except RuntimeError:
        pass
    else:  # pragma: no cover - защита от подавления ошибок
        raise AssertionError("исключение было подавлено контекстом проверки")
