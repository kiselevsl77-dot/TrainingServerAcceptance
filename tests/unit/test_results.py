"""Тесты результатов проверок (`acceptance/results.py`).

Проверяются свойства, на которых держатся «единый след результата» (`DR-P-5`),
история повторов (`FR-P-36`) и снятие пункта с причиной (`FR-P-15`), а также
представление результата для таблиц «Прогон» и «Протокол».
"""

from __future__ import annotations

from typing import Any

import pytest

from acceptance import programme as programme_api
from acceptance import results as results_api
from acceptance import sets as sets_api
from acceptance.checks.registry import CheckResult, CheckStatus
from acceptance.session import new_session


@pytest.fixture()
def session() -> Any:
    """Сессия испытаний без записи на диск (результатов ещё нет)."""
    return new_session(base_url="https://stand.local")


def _result(
    check_id: str = "TC-SYS-01",
    *,
    status: CheckStatus = CheckStatus.PASSED,
    verdict: str = "сервер отвечает",
    journal_to: int = 12,
) -> CheckResult:
    """Результат проверки в том виде, в каком его пишет движок."""
    return CheckResult(
        check_id=check_id,
        status=status,
        verdict=verdict,
        journal_from=1,
        journal_to=journal_to,
        duration_ms=15.0,
    )


def _record(session: Any, check_id: str) -> dict[str, Any]:
    """Запись результата проверки (в тестах результат обязан быть записан)."""
    found = results_api.result_of(session, check_id)
    assert found is not None, f"результат {check_id} не записан"
    return found


def test_record_keeps_one_entry_per_check(session: Any):
    """Результат проверки — одна запись на проверку: повтор заменяет, а не дублирует."""
    results_api.record(session, _result(verdict="первый прогон"))
    results_api.record(session, _result(verdict="второй прогон"))

    assert len(session.checks) == 1
    assert results_api.status_of(session, "TC-SYS-01") == str(CheckStatus.PASSED)
    assert _record(session, "TC-SYS-01")["verdict"] == "второй прогон"


def test_repeat_keeps_previous_result_in_history(session: Any):
    """Повтор сохраняет прежний результат как историю (`FR-P-36`)."""
    results_api.record(session, _result(verdict="первый прогон"))
    results_api.record(session, _result(verdict="второй прогон"))

    assert results_api.repeats(session, "TC-SYS-01") == 1
    assert results_api.history(session, "TC-SYS-01")[0]["verdict"] == "первый прогон"
    assert _record(session, "TC-SYS-01")["verdict"] == "второй прогон"


def test_prepare_repeat_is_idempotent(session: Any):
    """Повторный вызов `prepare_repeat` без прогона не плодит историю."""
    results_api.record(session, _result())

    first = results_api.prepare_repeat(session, "TC-SYS-01")
    second = results_api.prepare_repeat(session, "TC-SYS-01")

    assert first is not None and second is not None
    assert results_api.repeats(session, "TC-SYS-01") == 1


def test_prepare_repeat_ignores_missing_result(session: Any):
    """Если результата ещё нет (или он «не выполнена»), история не растёт."""
    assert results_api.prepare_repeat(session, "TC-SYS-01") is None

    session.checks.append({"check_id": "TC-SYS-01", "status": results_api.STATUS_NOT_RUN})

    assert results_api.prepare_repeat(session, "TC-SYS-01") is None
    assert results_api.repeats(session, "TC-SYS-01") == 0


def test_record_requires_check_id(session: Any):
    """Запись без `check_id` записать нельзя (иначе результат «потеряется»)."""
    with pytest.raises(ValueError):
        results_api.record(session, {"status": str(CheckStatus.PASSED)})


def test_suspend_requires_reason(session: Any):
    """Снятие без причины не принимается (`FR-P-15`, `AC-P-15`)."""
    for reason in ("", "   "):
        with pytest.raises(ValueError):
            results_api.suspend(session, "TC-SYS-01", reason)

    assert results_api.result_of(session, "TC-SYS-01") is None


def test_suspend_is_a_result_and_resume_returns_it(session: Any):
    """Снятие — результат прогона (виден в протоколе), возврат убирает результат снятия."""
    results_api.suspend(session, "TC-SYS-01", "данные стенда заняты", author="Петров П.П.")

    state = results_api.row(session, "TC-SYS-01")
    assert state["status"] == str(CheckStatus.SKIPPED)
    assert state["origin"] == results_api.ORIGIN_SUSPENDED
    assert state["reason"] == "данные стенда заняты"
    assert state["suspended"] is True
    assert results_api.is_suspended(session, "TC-SYS-01")

    assert results_api.resume(session, "TC-SYS-01") is True

    assert results_api.is_suspended(session, "TC-SYS-01") is False
    assert results_api.result_of(session, "TC-SYS-01") is None
    assert results_api.status_of(session, "TC-SYS-01") == results_api.STATUS_NOT_RUN
    assert results_api.summary(session)["suspended"] == 0
    assert any(entry.get("event") == "check_resumed" for entry in session.history)


def test_resume_without_suspension_does_nothing(session: Any):
    """Возврат неснятого пункта — не ошибка: просто нечего возвращать."""
    assert results_api.resume(session, "TC-SYS-01") is False


def test_state_feeds_programme_rows(session: Any):
    """`state()` — вход таблицы программы: статус берётся из результата (`DR-P-5`)."""
    library = sets_api.library_from_catalog()
    programme = programme_api.Programme.from_sets(library, ["SET-SYS"])
    results_api.record(session, _result("TC-SYS-01", verdict="сервер отвечает"))

    rows = {row["check_id"]: row for row in programme.rows(results_api.state(session))}

    assert rows["TC-SYS-01"]["status"] == str(CheckStatus.PASSED)
    assert rows["TC-SYS-01"]["icon"] == "✅"
    assert rows["TC-SYS-01"]["verdict"] == "сервер отвечает"
    # у проверки без результата программы статус пуст — программа его не хранит
    sys_set = library.find("SET-SYS")
    assert sys_set is not None
    assert rows[sys_set.check_ids[1]]["status"] == ""


def test_state_returns_rows_for_all_programme_checks(session: Any):
    """`rows()` отдаёт строку на каждую проверку, даже без результата («не выполнена»)."""
    library = sets_api.library_from_catalog()
    programme = programme_api.Programme.from_sets(library, ["SET-SYS"])

    rows = results_api.rows(session, programme.check_ids)

    assert [row["check_id"] for row in rows] == programme.check_ids
    assert all(row["status"] == results_api.STATUS_NOT_RUN for row in rows)
    assert all(row["icon"] == "⚪" for row in rows)


def test_summary_counts_results(session: Any):
    """Сводка считает статусы, снятия и повторы."""
    results_api.record(session, _result("TC-SYS-01"))
    results_api.record(session, _result("TC-SYS-02", status=CheckStatus.FAILED, verdict="500"))
    results_api.suspend(session, "TC-SYS-03", "вне контура испытаний")
    results_api.record(session, _result("TC-SYS-01", verdict="повтор"))

    summary = results_api.summary(session)

    assert summary["total"] == 3
    assert summary["done"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["suspended"] == 1
    assert summary["repeats"] == 1


def test_broken_records_are_tolerated(session: Any):
    """Повреждённые записи не ломают чтение результатов и сводку."""
    session.checks = [
        "мусор",
        {"status": str(CheckStatus.PASSED)},
        {"check_id": "TC-SYS-01", "status": str(CheckStatus.PASSED), "verdict": "ок"},
    ]
    session.check_history = {"TC-SYS-01": ["мусор", {"status": str(CheckStatus.PASSED)}]}

    assert results_api.status_of(session, "TC-SYS-01") == str(CheckStatus.PASSED)
    assert len(results_api.records_of(session)) == 2
    assert results_api.repeats(session, "TC-SYS-01") == 1
    assert results_api.summary(session)["total"] == 1


def test_annotate_adds_trace_fields_without_repeat(session: Any):
    """`annotate` дополняет запись движка полями пульта и не создаёт повтора."""
    results_api.record(session, _result())

    annotated = results_api.annotate(
        session, _result(), origin=results_api.ORIGIN_RUN, author="Петров П.П.", revision=5
    )

    assert results_api.repeats(session, "TC-SYS-01") == 0
    assert len(session.checks) == 1
    assert annotated["revision"] == 5
    assert annotated["author"] == "Петров П.П."
    assert _record(session, "TC-SYS-01")["revision"] == 5


def test_annotate_writes_record_if_absent(session: Any):
    """Если записи ещё нет, `annotate` создаёт её (движок мог не писать результат)."""
    annotated = results_api.annotate(session, _result("TC-REC-01"), revision=2)

    assert annotated["check_id"] == "TC-REC-01"
    assert results_api.status_of(session, "TC-REC-01") == str(CheckStatus.PASSED)
