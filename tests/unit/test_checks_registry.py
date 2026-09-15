"""Тесты моделей чек-листа (FR-T4): классы проверок, статусы, сводка."""

from __future__ import annotations

from acceptance.checks.registry import (
    CLASS_LABELS,
    DONE_STATUSES,
    CheckClass,
    CheckResult,
    CheckSpec,
    CheckStatus,
    new_result,
    summarize,
)


def _spec(
    check_id: str = "TC-FILE-01",
    check_class: CheckClass = CheckClass.TECH,
    blocked: str | None = None,
) -> CheckSpec:
    return CheckSpec(
        check_id=check_id,
        title="Проверка реестра файлов",
        module="File Import",
        requirement="FR-2, UC-03",
        check_class=check_class,
        steps=("GET /api/data/files", "сравнить count"),
        expected="Список файлов получен, count совпадает",
        endpoints=("GET /api/data/files",),
        blocked_by_api=blocked,
    )


def test_spec_class_labels_and_confirmation():
    tech = _spec()
    heavy = _spec("TC-TR-01", CheckClass.HEAVY)
    live = _spec("TC-DS-02", CheckClass.LIVE)

    assert tech.class_label == CLASS_LABELS[CheckClass.TECH]
    assert not tech.is_confirmation_required
    assert heavy.is_confirmation_required
    assert live.is_confirmation_required


def test_new_result_starts_not_run():
    result = new_result(_spec())

    assert result.check_id == "TC-FILE-01"
    assert result.status == CheckStatus.NOT_RUN
    assert not result.is_done
    assert result.icon == "⚪"


def test_result_serialization_and_status_coercion():
    result = CheckResult(
        check_id="TC-SYS-01",
        status=CheckStatus.BLOCKED,
        verdict="Блокировано дефектом API",
        evidence={"status_code": 404},
        params={"endpoint": "GET /api/data/files"},
    )

    payload = result.to_dict()
    assert payload["status"] == str(CheckStatus.BLOCKED)
    restored = CheckResult.from_dict(payload)
    assert restored.status == CheckStatus.BLOCKED
    assert restored.is_done
    assert restored.evidence == {"status_code": 404}


def test_result_from_dict_tolerates_unknown_status():
    restored = CheckResult.from_dict({"check_id": "TC-?-01", "status": "странный статус"})

    assert restored.status == CheckStatus.NOT_RUN


def test_summarize_counts_statuses():
    results = [
        CheckResult("TC-SYS-01", status=CheckStatus.PASSED, verdict="ок"),
        CheckResult("TC-FILE-01", status=CheckStatus.FAILED, verdict="нет"),
        CheckResult("TC-FILE-02", status=CheckStatus.BLOCKED, verdict="дефект API"),
        CheckResult("TC-LOAD-01"),
    ]

    summary = summarize(results)

    assert summary["total"] == 4
    assert summary["done"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["blocked"] == 1
    assert summary["not_run"] == 1
    assert set(DONE_STATUSES).issubset(set(summary["counts"]))


def test_summarize_accepts_serialized_dicts():
    summary = summarize([CheckResult("TC-SYS-01", status=CheckStatus.MANUAL_OK).to_dict()])

    assert summary["done"] == 1
