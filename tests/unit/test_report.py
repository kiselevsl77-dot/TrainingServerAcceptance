"""Тесты комплекта отчёта об испытаниях (этап T3: каркас md + json + приложения).

Отчёт строится **только** по данным сессии и структурного журнала: отдельный тест
подменяет `httpx.Client.send` и падает, если формирование отчёта начнёт обращаться
к испытуемому серверу (повторяемость результата — требование к отчёту).
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from acceptance import dataset_composition, report
from acceptance import notes as notes_api
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks.registry import CheckResult, CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal
from acceptance.session import (
    SessionInfo,
    add_artifact,
    add_task,
    add_test_entity,
    close_session,
    mark_test_entity_deleted,
    new_session,
    set_markup_stats,
    set_snapshot,
    start_session,
    update_task,
)
from acceptance.session import (
    TestSession as Session,
)
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"


def filled_info() -> SessionInfo:
    """Заполненное ядро сессии и программа испытаний (шапка и выводы отчёта)."""
    return SessionInfo(
        title="Приёмочные испытания сервера обучения",
        program_doc="docs/06. План реализации (этап T3)",
        object_of_test="Сервер обучения SOM1, сборка 16.09.2026",
        customer="Заказчик",
        lab="Лаборатория приёмки",
        operator_fio="Иванов И.И.",
        operator_position="инженер-испытатель",
        commission="Петров П.П., Сидоров С.С.",
        goal="Подтвердить соответствие API требованиям docs/01",
        scope="Чек-лист docs/02: система, файлы, записи, нагрузки",
        conditions="Локальный стенд, одна сессия",
        limitations="Боевые данные не удаляются",
        criteria="Нет отказов и неразобранных дефектов API",
        conclusion="годен с замечаниями",
        signatures="Иванов И.И. ____________",
        notes="Пульт испытаний, версия 0.1",
    )


def mark_check(
    session,
    check_id: str,
    status: CheckStatus = CheckStatus.PASSED,
    *,
    verdict: str = "автоматизированная проверка пройдена",
    evidence: dict[str, Any] | None = None,
) -> CheckResult:
    """Фиксирует результат проверки в сессии (как это делает движок)."""
    result = CheckResult(
        check_id=check_id,
        status=status,
        verdict=verdict,
        evidence=dict(evidence or {}),
        started_at="2026-09-17T10:00:00",
        ended_at="2026-09-17T10:00:01",
        duration_ms=12.5,
        journal_from=3,
        journal_to=7,
    )
    checks_engine.record_result(session, result)
    return result


def mark_all(session) -> None:
    """Проставляет «успех» всем проверкам каталога — сессия без незавершённых проверок."""
    for group in catalog.groups(implemented_only=True):
        for spec in group.checks:
            mark_check(session, spec.check_id, verdict="выполнено для теста отчёта")


def stand_snapshot(
    counts: dict[str, int], records: dict[str, int] | None = None, *, revision: str = "abc123"
) -> dict[str, Any]:
    """Снимок стенда в том виде, в котором его сохраняет пульт (FR-T1)."""
    return {
        "version": {"revision": revision},
        "counts": dict(counts),
        "records": dict(records or {}),
        "errors": {},
    }


def full_session() -> Session:
    """Сессия, доведённая до подписания: снимки, артефакты, задача, сущности, все проверки."""
    session = new_session(base_url=BASE_URL, info=filled_info())
    start_session(session)
    set_snapshot(session, "start", stand_snapshot({"files": 10, "loads": 4}, {"raw": 3}))
    set_snapshot(session, "end", stand_snapshot({"files": 12, "loads": 4}, {"raw": 4}))
    add_artifact(
        session,
        kind="manifest",
        path="acceptance_data/artifacts/manifest.json",
        size_bytes=120,
        note="манифест боевых файлов",
    )
    add_task(session, task_id="task-1", task_type="train", name="Обучение", status="PENDING")
    update_task(session, "task-1", status="SUCCESS", note="обучение завершено")
    add_test_entity(session, entity_id="__TEST__file-1", entity_type="file", check_id="TC-FILE-12")
    mark_test_entity_deleted(session, "__TEST__file-1", check_id="TC-FILE-13")
    set_markup_stats(session, "markup-1", records=3, chunks=2, status="клиентская оценка")
    mark_all(session)
    close_session(session)
    return session


def blocked_session() -> Session:
    """Сессия с ожидаемым дефектом API: `TC-FILE-10` блокирована и имеет замечание P0."""
    session = new_session(base_url=BASE_URL, info=filled_info())
    spec = catalog.find("TC-FILE-10")
    assert spec is not None
    result = mark_check(
        session,
        spec.check_id,
        CheckStatus.BLOCKED,
        verdict="скачивание файла с не-ASCII именем отвечает 404 (дефект latin-1) — замечание P0",
        evidence={"status": 404, "file": "Antminer_№1.raw.csv"},
    )
    checks_engine.ensure_defect_note(session, spec, result)
    return session


def journal_records() -> list[Any]:
    """Записи журнала с меткой проверки (для приложения `journal.jsonl`)."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Ответ подменённого сервера."""
        return httpx.Response(200, json={"ok": True})

    journal = Journal(max_records=50)
    settings = TrainingServerSettings(base_url=BASE_URL, timeout=5.0)
    client = build_console_client(settings, journal, inner=httpx.MockTransport(handler))
    with checks_engine.check_label("TC-LOAD-02"):
        client.get("/api/loads/list", params={"limit": 1000})
    return list(journal.records)


def implemented_ids() -> list[str]:
    """Идентификаторы реализованных проверок (группы, попавшие в каталог этапа T3)."""
    return [
        spec.check_id for group in catalog.groups(implemented_only=True) for spec in group.checks
    ]


def csv_rows(text: str) -> list[dict[str, str]]:
    """Строки CSV-приложения (с BOM-совместимым чтением)."""
    return list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))


# ---------------------------------------------------------------------------
# Состав комплекта: markdown, JSON и все разделы
# ---------------------------------------------------------------------------
def test_report_contains_all_sections():
    """Markdown-отчёт содержит все разделы каркаса T3 в заданном порядке."""
    markdown = report.report_markdown(full_session())

    assert markdown.startswith("# Протокол испытаний сервера обучения")
    for number, title in enumerate(report.SECTION_TITLES, start=1):
        assert f"## {number}. {title}" in markdown


def test_report_does_not_touch_network(monkeypatch: pytest.MonkeyPatch):
    """Отчёт строится по данным сессии и журнала — запросов к серверу нет."""
    session = full_session()
    records = journal_records()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        """Любая попытка HTTP-обмена при построении отчёта — ошибка теста."""
        raise AssertionError("формирование отчёта не должно обращаться к испытуемому серверу")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    bundle = report.build(session, journal_records=records)

    assert bundle.markdown and bundle.payload


def test_report_json_is_machine_readable():
    """JSON-копия разбирается и содержит ключевые блоки комплекта."""
    session = full_session()
    payload = json.loads(report.report_json(session))

    assert payload["report_version"] == report.REPORT_VERSION
    assert payload["session"]["session_id"] == session.session_id
    assert payload["session"]["info"]["title"] == session.info.title
    assert [row["check_id"] for row in payload["checks"]] == implemented_ids()
    assert payload["readiness"]["ready"] is True
    assert payload["snapshots"]["rows"]
    assert payload["markup_stats"]["markup-1"]["records"] == 3
    assert "checks.csv" in payload["annexes"]


def test_report_groups_cover_checklist():
    """Раздел сводки показывает все наполненные группы чек-листа."""
    markdown = report.report_markdown(full_session())
    payload = json.loads(report.report_json(full_session()))

    groups = {row["group"]: row for row in payload["groups"]}
    assert set(groups) == {group.key for group in catalog.groups(implemented_only=True)}
    assert sum(row["total"] for row in groups.values()) == len(implemented_ids())
    for key in groups:
        assert f"| {key} |" in markdown


# ---------------------------------------------------------------------------
# Полнота отчёта: предупреждения и готовность к подписанию
# ---------------------------------------------------------------------------
def test_empty_session_reports_warnings():
    """Черновик без данных: отчёт формируется, но помечен как неполный."""
    session = new_session(base_url=BASE_URL)
    bundle = report.build(session)
    warnings = bundle.warnings

    assert any("не заполнено ядро сессии" in text for text in warnings)
    assert any("нет снимка стенда «начало»" in text for text in warnings)
    assert any("нет снимка стенда «окончание»" in text for text in warnings)
    assert any("не выполнено проверок" in text for text in warnings)
    assert any("не зафиксировано итоговое решение" in text for text in warnings)
    assert bundle.is_complete() is False
    assert bundle.payload["readiness"]["ready"] is False
    assert bundle.payload["readiness"]["checks"]["not_run"] == len(implemented_ids())
    assert "> ⚠️ **Отчёт неполон:**" in bundle.markdown
    assert "не определён" in bundle.markdown  # рекомендация, пока не выполнены проверки


def test_blocked_without_note_is_a_warning():
    """«Блокировано API» без замечания — предупреждение о полноте (FR-T7)."""
    session = new_session(base_url=BASE_URL, info=filled_info())
    mark_check(session, "TC-LOAD-02", CheckStatus.BLOCKED, verdict="фильтр ph_n не работает")

    warnings = report.build(session).warnings

    assert any("«блокировано API» без замечания: TC-LOAD-02" in text for text in warnings)


def test_pending_test_entities_are_a_warning():
    """Созданная и не удалённая `__TEST__`-сущность — предупреждение (NFR-T4, TC-CLEAN-01)."""
    session = new_session(base_url=BASE_URL, info=filled_info())
    add_test_entity(session, entity_id="__TEST__stale", entity_type="file", check_id="TC-FILE-12")

    bundle = report.build(session)

    assert any("__TEST__stale" in text for text in bundle.warnings)
    assert "> ⚠️ Не удалены: __TEST__stale." in bundle.markdown


def test_complete_session_is_ready_to_sign():
    """Полная сессия: предупреждений нет, рекомендация пульта — «годен»."""
    session = full_session()
    bundle = report.build(session)

    assert bundle.warnings == ()
    assert bundle.is_complete() is True
    assert bundle.payload["readiness"]["ready"] is True
    assert "Рекомендация пульта по результатам сессии: **годен**" in bundle.markdown
    assert "Самоочистка выполнена: созданных и не удалённых сущностей нет." in bundle.markdown


def test_failed_check_changes_recommendation():
    """Отказ проверки меняет рекомендацию и попадает в предупреждения."""
    session = full_session()
    mark_check(session, "TC-FILE-01", CheckStatus.FAILED, verdict="реестр файлов недоступен")
    bundle = report.build(session)

    assert any("проверок с отказом: 1" in text for text in bundle.warnings)
    assert "не годен (есть отказы проверок)" in bundle.markdown


# ---------------------------------------------------------------------------
# Снимки стенда «начало/окончание» и их сравнение (FR-T1, BR-R6)
# ---------------------------------------------------------------------------
def test_snapshot_comparison_shows_delta():
    """Сравнение снимков показывает дельту по счётчикам реестров."""
    session = full_session()
    payload = report.build(session).payload

    rows = {row["metric"]: row for row in payload["snapshots"]["rows"]}
    assert rows["files"] == {"metric": "files", "start": 10, "end": 12, "delta": 2}
    assert rows["loads"] == {"metric": "loads", "start": 4, "end": 4, "delta": 0}
    assert payload["snapshots"]["build_start"] == "abc123"
    markdown = report.report_markdown(session)
    assert "| files | 10 | 12 | 2 |" in markdown
    assert "**Записи (RAW + markup) на окончание сессии:** raw = 4" in markdown


def test_missing_end_snapshot_is_marked_in_report():
    """Без снимка «окончание» раздел прямо предупреждает об отсутствии подтверждения."""
    session = new_session(base_url=BASE_URL, info=filled_info())
    set_snapshot(session, "start", stand_snapshot({"files": 10}))

    markdown = report.report_markdown(session)

    assert "> ⚠️ Снимок «окончание» отсутствует: изменение реестров не подтверждено." in markdown


# ---------------------------------------------------------------------------
# Раздел замечаний к API и ожидаемо блокированные проверки (FR-T7)
# ---------------------------------------------------------------------------
def test_blocked_check_is_listed_with_auto_note():
    """Ожидаемый дефект попадает в сводку «блокировано API» и в раздел замечаний P0."""
    session = blocked_session()
    bundle = report.build(session)
    note = notes_api.note_from_check("TC-FILE-10")
    assert note is not None

    assert "**Ожидаемо блокированные проверки (дефекты API):**" in bundle.markdown
    assert "| TC-FILE-10 |" in bundle.markdown
    assert "### P0 — блокирует корректную работу UI/проверки (1)" in bundle.markdown
    assert note.title in bundle.markdown
    assert "проверка: `TC-FILE-10`" in bundle.markdown
    assert bundle.payload["readiness"]["notes"] == {
        "total": 1,
        "p0": 1,
        "p1": 0,
        "p2": 0,
        "auto": 1,
    }
    assert [item["check_id"] for item in bundle.payload["notes"]] == ["TC-FILE-10"]


def test_notes_annex_lists_priorities():
    """Приложение «Замечания к API» — CSV с приоритетами и привязкой к проверке."""
    session = blocked_session()
    rows = csv_rows(report.notes_csv(session))

    assert rows and rows[0]["priority"] == "P0"
    assert rows[0]["check_id"] == "TC-FILE-10"
    assert "не-ASCII" in rows[0]["title"]
    assert "проверка TC-FILE-10" in rows[0]["evidence"]


def test_prospective_requirements_section():
    """Раздел перспективных требований выводит бэклог из реестра требований."""
    session = full_session()
    requirements = notes_api.prospective_requirements()

    assert requirements, "реестр перспективных требований не должен быть пустым"
    markdown = report.report_markdown(session)
    assert "Перспективные требования" in markdown
    for item in requirements[:3]:
        assert str(item["title"])[:40] in markdown


# ---------------------------------------------------------------------------
# Задачи (FSM-1), артефакты и `__TEST__`-сущности
# ---------------------------------------------------------------------------
def test_tasks_section_shows_fsm_chain():
    """Раздел задач показывает таблицу наблюдения и цепочку переходов FSM-1."""
    session = full_session()
    markdown = report.report_markdown(session)

    assert "**История FSM-1 задачи `task-1`:**" in markdown
    assert "— → PENDING" in markdown
    assert "PENDING → SUCCESS" in markdown
    assert "обучение завершено" in markdown


def test_tasks_section_notes_absence_of_tasks():
    """Без наблюдаемых задач раздел сообщает об этом прямо."""
    session = new_session(base_url=BASE_URL, info=filled_info())
    markdown = report.report_markdown(session)

    assert "Наблюдаемых задач нет." in markdown


def test_artifacts_and_entities_sections():
    """Раздел 7 перечисляет артефакты и `__TEST__`-сущности с действиями."""
    session = full_session()
    bundle = report.build(session)

    assert "| manifest | manifest.json | 120 |" in bundle.markdown
    assert "__TEST__file-1" in bundle.markdown
    assert "| __TEST__file-1 | file | создан |" in bundle.markdown
    assert "| __TEST__file-1 | file | удалён |" in bundle.markdown
    assert bundle.payload["readiness"]["artifacts"]["total"] == 1
    assert bundle.payload["readiness"]["test_entities"] == {"total": 2, "pending": 0}
    assert [item["action"] for item in bundle.payload["test_entities"]] == ["создан", "удалён"]


def test_details_section_shows_evidence_and_journal_range():
    """Детализация проверки содержит вердикт, диапазон журнала и доказательства."""
    session = new_session(base_url=BASE_URL, info=filled_info())
    mark_check(
        session,
        "TC-SYS-01",
        evidence={"version": {"revision": "abc123"}},
        verdict="сервер доступен: revision abc123",
    )
    markdown = report.report_markdown(session)

    assert (
        "| TC-SYS-01 | tech | успех | сервер доступен: revision abc123 | #3…7 "
        "| 2026-09-17T10:00:01 |" in markdown
    )
    assert "* **TC-SYS-01** — успех: сервер доступен: revision abc123" in markdown
    assert '"revision": "abc123"' in markdown


# ---------------------------------------------------------------------------
# Приложения комплекта: CSV и выдержка журнала
# ---------------------------------------------------------------------------
def test_checks_annex_covers_catalog():
    """Приложение «Таблица проверок» содержит все проверки каталога и статусы."""
    session = blocked_session()
    rows = csv_rows(report.checks_csv(session))

    assert [row["check_id"] for row in rows] == implemented_ids()
    assert tuple(rows[0]) == report.CHECK_COLUMNS
    blocked = next(row for row in rows if row["check_id"] == "TC-FILE-10")
    assert blocked["status"] == str(CheckStatus.BLOCKED)
    assert blocked["group"] == "TC-FILE"
    assert blocked["class"] == "tech"
    assert blocked["journal_range"] == "#3…7"


def test_entities_annex_has_columns():
    """Приложение «`__TEST__`-сущности» выводит создание и удаление (NFR-T4)."""
    rows = csv_rows(report.entities_csv(full_session()))

    assert tuple(rows[0]) == report.ENTITY_COLUMNS
    assert [(row["id"], row["action"], row["check_id"]) for row in rows] == [
        ("__TEST__file-1", "создан", "TC-FILE-12"),
        ("__TEST__file-1", "удалён", "TC-FILE-13"),
    ]


def test_empty_annex_is_still_valid_csv():
    """Пустые приложения (нет замечаний/сущностей) остаются корректным CSV с шапкой."""
    session = new_session(base_url=BASE_URL, info=filled_info())

    notes_text = report.notes_csv(session)
    entities_text = report.entities_csv(session)

    assert notes_text.strip().splitlines() == [",".join(report.NOTE_COLUMNS)]
    assert entities_text.strip().splitlines() == [",".join(report.ENTITY_COLUMNS)]


def test_journal_annex_and_labels_from_records():
    """Выдержка журнала — JSONL; сводка по меткам считает обмены каждой проверки."""
    session = full_session()
    records = journal_records()
    bundle = report.build(session, journal_records=records)

    lines = bundle.annexes["journal.jsonl"].strip().splitlines()
    assert len(lines) == len(records) == 1
    payload = json.loads(lines[0])
    assert payload["method"] == "GET" and payload["path"] == "/api/loads/list"
    assert bundle.payload["journal_labels"] == {"TC-LOAD-02": 1}
    assert report.journal_labels(records) == {"TC-LOAD-02": 1}
    assert "journal.jsonl" in bundle.markdown


def test_journal_annex_accepts_session_text():
    """Готовый текст структурного журнала сессии подставляется без изменений."""
    text = report.journal_annex(journal_text='{"seq": 1, "method": "GET"}')

    assert text == '{"seq": 1, "method": "GET"}\n'
    assert report.journal_annex() == ""
    assert report.journal_labels() == {}


def test_bundle_saves_full_kit(tmp_path: Path):
    """`ReportBundle.save` выгружает md, json и приложения в указанный каталог."""
    session = full_session()
    bundle = report.build(
        session, journal_records=journal_records(), meta={"журнал": "logs/x.jsonl"}
    )
    written = bundle.save(tmp_path)

    assert set(written) == {
        "report_md",
        "report_json",
        "checks.csv",
        "notes.csv",
        "test_entities.csv",
        "journal.jsonl",
    }
    stem = bundle.file_stem
    assert written["report_md"].name == f"{stem}.md"
    assert written["report_json"].name == f"{stem}.json"
    assert written["checks.csv"].name == f"{stem}_checks.csv"
    for path in written.values():
        assert path.exists()
    assert (
        written["report_md"]
        .read_text(encoding="utf-8-sig")
        .startswith("# Протокол испытаний сервера обучения")
    )
    assert json.loads(written["report_json"].read_text(encoding="utf-8-sig"))["report_version"] == 1
    assert "logs/x.jsonl" in bundle.markdown  # meta попала в шапку комплекта


# ---------------------------------------------------------------------------
# Приложение «Манифест записей» (выгрузка экрана «Записи», FR-T8)
# ---------------------------------------------------------------------------
def test_manifest_annex_from_saved_artifacts(tmp_path: Path):
    """Сохранённый манифест записей попадает в комплект приложением (без запросов к серверу)."""
    session = full_session()
    csv_path = tmp_path / "records_manifest_20260917-120000.csv"
    json_path = tmp_path / "records_manifest_20260917-120000.json"
    csv_path.write_text("record,raw\nAntminer_S19,Antminer_S19.raw.csv\n", encoding="utf-8-sig")
    json_path.write_text('{"rows": []}', encoding="utf-8")
    add_artifact(session, kind="records_manifest (CSV)", path=csv_path, note="манифест записей")
    add_artifact(session, kind="records_manifest (JSON)", path=json_path, note="манифест записей")

    bundle = report.build(session)

    assert csv_path.name in bundle.annexes and json_path.name in bundle.annexes
    assert "Antminer_S19.raw.csv" in bundle.annexes[csv_path.name]
    assert csv_path.name in bundle.markdown  # приложение указано в разделе «Приложения»
    assert bundle.warnings == (), "читаемые манифесты не должны давать предупреждений"
    written = bundle.save(tmp_path / "kit")
    assert written[csv_path.name].name == f"{bundle.file_stem}_{csv_path.name}"


def test_manifest_annex_missing_file_is_a_warning(tmp_path: Path):
    """Пропавший файл манифеста — предупреждение о полноте, а не падение отчёта."""
    session = new_session(base_url=BASE_URL, info=filled_info())
    add_artifact(
        session,
        kind="records_manifest (CSV)",
        path=tmp_path / "records_manifest_lost.csv",
        note="манифест записей",
    )

    bundle = report.build(session)

    assert any("манифест записей не прочитан" in text for text in bundle.warnings)
    assert bundle.payload["readiness"]["ready"] is False
    assert not [name for name in bundle.annexes if name.startswith("records_manifest")]


# ---------------------------------------------------------------------------
# Датасеты и состав (этап T5)
# ---------------------------------------------------------------------------
def test_datasets_section_lists_sources_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Состав датасетов в отчёте: ярлык источника и косвенные признаки дублей (T5)."""
    monkeypatch.setattr(dataset_composition, "DATA_DIR", tmp_path)
    session = new_session(base_url=BASE_URL, info=filled_info())
    start_session(session)
    raw_a = "11111111-1111-4111-8111-111111111111"
    markup_a = "22222222-2222-4222-8222-222222222222"
    raw_b = "33333333-3333-4333-8333-333333333333"
    dataset_composition.record_fill(
        session,
        dataset_id="d-1",
        raw_file_ids=[raw_a],
        markup_file_ids=[markup_a],
        name="skfu-train",
        check_id="TC-DS-05",
    )
    dataset_composition.record_composition(
        session,
        dataset_composition.DatasetComposition(
            dataset_id="d-2",
            source=dataset_composition.SOURCE_HEURISTIC,
            name="leti-train",
            files=(
                dataset_composition.CompositionFile(
                    file_id=raw_a, role=dataset_composition.ROLE_RAW
                ),
                dataset_composition.CompositionFile(
                    file_id=raw_b, role=dataset_composition.ROLE_RAW
                ),
            ),
        ),
    )

    bundle = report.build(session)

    assert "## 7. Датасеты и состав" in bundle.markdown
    assert "локальный учёт пульта" in bundle.markdown
    assert "предположение по реестру файлов" in bundle.markdown
    assert "Файлы, входящие в несколько датасетов" in bundle.markdown
    payload = bundle.payload["datasets"]
    assert payload["overlap"]["totals"]["cross_dataset"] == 1
    assert {item["dataset_id"] for item in payload["compositions"]} == {"d-1", "d-2"}


def test_datasets_section_notes_absence_of_composition():
    """Без учёта состава раздел честно сообщает, что состав не фиксировался (T5)."""
    session = new_session(base_url=BASE_URL, info=filled_info())

    bundle = report.build(session)

    assert "## 7. Датасеты и состав" in bundle.markdown
    assert "Состав датасетов в этой сессии не фиксировался" in bundle.markdown
    assert bundle.payload["datasets"]["compositions"] == []
