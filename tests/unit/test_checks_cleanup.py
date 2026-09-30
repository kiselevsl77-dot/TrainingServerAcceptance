"""Тесты сценариев группы `TC-CLEAN` (этап T9): перечень `__TEST__` и снимки стенда.

Проверки ничего не создают: источник факта — сессия испытаний (`session.counters`,
`session.snapshots`). Тесты собирают сессию вручную и проверяют, что сценарий честно
отличает «всё объяснено» от «есть расхождения», а расхождения идут **примечанием**
(замечание отчёта), как требует чек-лист `FR-T1`/`BR-R6`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from acceptance import session as session_api
from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import cleanup as check_cleanup
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckSpec, CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
Handler = Callable[[httpx.Request], httpx.Response]

FILE_ID = "11111111-1111-4111-8111-111111111111"
MODEL_ID = "3d2d1139-0000-4000-8000-000000000001"


def spec_of(check_id: str) -> CheckSpec:
    """Описание проверки из каталога (в тестах оно всегда существует)."""
    spec = catalog.find(check_id)
    assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
    return spec


def _not_found(request: httpx.Request) -> httpx.Response:  # pragma: no cover - не вызывается
    """Сценарии сессии на стенд не ходят: любой запрос — ошибка теста."""
    return httpx.Response(500, json={"detail": "сценарий не должен обращаться к стенду"})


def stand(check_id: str, session: Any = None, **params: Any) -> Any:
    """Контекст сценария с заранее собранной сессией (без реальной сети)."""
    journal = Journal(max_records=200)
    transport = httpx.MockTransport(_not_found)
    client = ApiHttpClient(
        base_url=BASE_URL, timeout=5.0, transport=LoggingTransport(journal, inner=transport)
    )
    settings = TrainingServerSettings(base_url=BASE_URL, timeout=5.0)
    console = build_console_client(settings, journal, inner=transport)
    return check_runner.AutomationContext(
        session=session or new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=Apis.build(client),
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
    )


def entity(session: Any, *, entity_id: str, entity_type: str, action: str, check_id: str) -> None:
    """Регистрирует `__TEST__`-сущность сессии тем же способом, что и проверки."""
    session_api.add_test_entity(
        session,
        entity_id=entity_id,
        entity_type=entity_type,
        action=action,
        check_id=check_id,
        note=f"создан проверкой {check_id}",
    )


def snapshot(
    *, files: int, models: int, datasets: int, tasks: int, version: str = "dev@abc"
) -> dict:
    """Снимок стенда фазы: счётчики реестров и версия сервера."""
    return {
        "at": "2026-09-30T12:00:00",
        "version": {"branch": version.split("@")[0], "revision": version.split("@")[-1]},
        "counts": {"files": files, "models": models, "datasets": datasets, "tasks": tasks},
        "files": {},
        "records": {},
        "errors": {},
    }


# ---------------------------------------------------------------------------
# TC-CLEAN-02 — перечень созданного и удалённого
# ---------------------------------------------------------------------------
def test_entities_pass_with_created_and_deleted():
    """Перечень полон: записи с id/типом/действием/временем, не удалось — примечание."""
    session = new_session(base_url=BASE_URL)
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="создан", check_id="TC-FILE-13"
    )
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="удалён", check_id="TC-FILE-13"
    )
    entity(session, entity_id=MODEL_ID, entity_type="модель", action="создан", check_id="TC-MOD-02")

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["total"] == 3
    assert outcome.evidence["created"] == 2
    assert outcome.evidence["deleted"] == 1
    assert outcome.evidence["pending"] == 1
    assert outcome.evidence["pending_ids"] == [MODEL_ID]
    assert outcome.evidence["by_type"] == {"файл RAW": 2, "модель": 1}
    assert outcome.evidence["checks"] == ["TC-FILE-13", "TC-MOD-02"]
    assert "не удалено 1" in outcome.note


def test_entities_skipped_without_entities():
    """Сессия без `__TEST__`-сущностей — «пропущена»: учитывать нечего."""
    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02"))

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет `__TEST__`-сущностей" in outcome.verdict


def test_entities_fail_on_incomplete_record():
    """Запись без времени — отказ: в отчёте должно быть видно, когда создано."""
    session = new_session(base_url=BASE_URL)
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="создан", check_id="TC-FILE-13"
    )
    session.counters["test_entities"][0].pop("at")

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02", session))

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "неполные записи" in outcome.verdict


def test_entities_fail_on_unknown_action():
    """Действие вне «создан/удалён» — отказ: перечень перестаёт быть однозначным."""
    session = new_session(base_url=BASE_URL)
    session.counters["test_entities"] = [
        {
            "id": FILE_ID,
            "type": "файл RAW",
            "action": "создано",
            "at": "2026-09-30T12:00:00",
            "check_id": "TC-FILE-13",
            "note": "",
        }
    ]

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02", session))

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "неизвестные действия" in outcome.verdict


def test_entities_note_for_unknown_type():
    """Тип без соответствия счётчику снимка — примечание, а не отказ."""
    session = new_session(base_url=BASE_URL)
    entity(session, entity_id=FILE_ID, entity_type="запись", action="создан", check_id="TC-REC-01")

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "типы без соответствия" in outcome.note


# ---------------------------------------------------------------------------
# TC-CLEAN-03 — сверка снимков стенда
# ---------------------------------------------------------------------------
def test_snapshots_explain_all_deltas():
    """Дельта каждого счётчика объясняется сущностями и задачами испытаний (FR-T1)."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=11, models=2, datasets=7, tasks=6)
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="создан", check_id="TC-FILE-13"
    )
    entity(session, entity_id=MODEL_ID, entity_type="модель", action="создан", check_id="TC-MOD-02")

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    rows = {row["counter"]: row for row in outcome.evidence["delta"]}
    assert rows["files"]["delta"] == 1 and rows["files"]["explained"] is True
    assert rows["models"]["delta"] == 1 and rows["models"]["expected"] == 1
    assert rows["datasets"]["delta"] == 0
    assert rows["tasks"]["delta"] == 1 and "задачи испытаний" in rows["tasks"]["reason"]
    assert outcome.evidence["unexplained"] == []
    assert outcome.note == ""
    assert "не объяснено 0" in outcome.verdict


def test_snapshots_note_unexplained_delta():
    """Рост реестра без сущностей в сессии — примечание (расхождение в отчёт)."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=12, models=1, datasets=7, tasks=5)

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["unexplained"] == ["files: +2 (ожидалось +0)"]
    assert "расхождения для отчёта" in outcome.note
    assert "files" in outcome.note


def test_snapshots_note_negative_delta():
    """Уменьшение счётчика — необъяснённое расхождение (удалено больше, чем учтено)."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=8, models=1, datasets=7, tasks=5)

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    rows = {row["counter"]: row for row in outcome.evidence["delta"]}
    assert "уменьшился" in rows["files"]["reason"]
    assert outcome.evidence["unexplained"] == ["files: -2 (ожидалось +0)"]


def test_snapshots_explains_returned_entities():
    """Создано и удалено по одной сущности — счётчик вернулся к прежнему значению."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="создан", check_id="TC-FILE-13"
    )
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="удалён", check_id="TC-FILE-13"
    )

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["unexplained"] == []
    assert outcome.evidence["entities"]["created"] == {"files": 1}
    assert outcome.evidence["entities"]["deleted"] == {"files": 1}


def test_snapshots_skipped_without_end_snapshot():
    """Снимок «Окончание» не снят — «пропущена» с подсказкой оператору."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "Окончание" in outcome.verdict


def test_snapshots_skipped_without_start_snapshot():
    """Снимок «Начало» не снят — «пропущена»."""
    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03"))

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "Начало" in outcome.verdict


def test_snapshots_note_for_snapshot_errors_and_version_change():
    """Ошибки чтения реестров и смена сборки — примечания отчёта."""
    session = new_session(base_url=BASE_URL)
    start = snapshot(files=10, models=1, datasets=7, tasks=5, version="dev@abc")
    end = snapshot(files=10, models=1, datasets=7, tasks=5, version="dev@def")
    end["errors"] = {"loads": "реестр нагрузок не получен"}
    session.snapshots["start"] = start
    session.snapshots["end"] = end

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "ошибки чтения реестров" in outcome.note
    assert "сборка сервера" in outcome.note
