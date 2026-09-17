"""Тесты сценариев проверок модуля «Система» (TC-SYS-01…05, этап T3).

Стенд собирается без сети: `httpx.MockTransport` отвечает так, как нужно проверке,
а журнал (`acceptance.http_log.Journal`) фиксирует каждый обмен. Через сценарии
проверяется, что вердикты соответствуют правилам программы испытаний:

    * `TC-SYS-01` — 200 = «сервис доступен», ошибка сервера = отказ;
    * `TC-SYS-02` — состав сборки попадает в сессию (шапка отчёта), нехватка полей — факт;
    * `TC-SYS-03` — неизвестный путь обязан ответить 404 с разобранным `detail`;
    * `TC-SYS-04` — неизвестная задача отдаётся статусом `not_found` либо 404;
    * `TC-SYS-05` — формат ошибок валидации согласован между эндпоинтами.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import runner as check_runner
from acceptance.checks import system as check_system
from acceptance.checks.registry import CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
TASK_ID = "11111111-1111-4111-8111-111111111111"
Handler = Callable[[httpx.Request], httpx.Response]


def spec_of(check_id: str):
    """Описание проверки каталога с проверкой наличия."""
    spec = catalog.find(check_id)
    assert spec is not None
    return spec


def stand(handler: Handler, check_id: str = "TC-SYS-01", **params):
    """Контекст сценария поверх подменённого транспорта (без реальной сети)."""
    journal = Journal(max_records=100)
    transport = httpx.MockTransport(handler)
    client = ApiHttpClient(
        base_url=BASE_URL, timeout=5.0, transport=LoggingTransport(journal, inner=transport)
    )
    settings = TrainingServerSettings(base_url=BASE_URL, timeout=5.0)
    console = build_console_client(settings, journal, inner=transport)
    context = check_runner.AutomationContext(
        session=new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=Apis.build(client),
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
    )
    return context, journal


def response(request: httpx.Request) -> httpx.Response:
    """Маршруты подменённого сервера: система, задачи и ошибки валидации."""
    path = request.url.path
    query = dict(request.url.params)
    if path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if path == "/version":
        return httpx.Response(
            200,
            json={
                "branch": "main",
                "revision": "abc1234",
                "commit": "abc1234",
                "build_date": "2026-09-01T10:00:00",
            },
        )
    if path.startswith("/api/tasks/"):
        return httpx.Response(
            200,
            json={
                "type": "celery-test",
                "name": "test",
                "id": TASK_ID,
                "created_at": "2026-09-01T10:00:00",
                "runtimes": [
                    {
                        "task_id": TASK_ID,
                        "status": "not_found",
                        "parameters": {},
                        "id": "22222222-2222-4222-8222-222222222222",
                    }
                ],
            },
        )
    if path == "/api/data/files":
        if query.get("id") == "not-a-uuid":
            return httpx.Response(
                422,
                json={"detail": [{"loc": ["query", "id"], "msg": "not a uuid", "type": "uuid"}]},
            )
        return httpx.Response(200, json={"files": [], "count": 0})
    if path.startswith("/api/datasets/"):
        return httpx.Response(422, json={"detail": "invalid dataset id"})
    return httpx.Response(404, json={"detail": "Not Found"})


# ---------------------------------------------------------------------------
# TC-SYS-01 — доступность сервиса
# ---------------------------------------------------------------------------
def test_health_passes_on_200():
    """200 от `/health` — сервис помечен доступным."""
    context, journal = stand(response)

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["status"] == 200
    assert journal.records[-1].path == "/health"


def test_health_fails_on_server_error():
    """Ошибка сервера на `/health` — отказ проверки."""
    context, _ = stand(lambda request: httpx.Response(500, text="boom"))

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "недоступен" in outcome.verdict


def test_health_skipped_without_stand():
    """Без адреса стенда проверка «пропущена», а не «отказана»."""
    context = check_runner.AutomationContext(
        session=new_session(base_url=""), spec=spec_of("TC-SYS-01")
    )

    outcome = check_runner.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "API системы недоступен" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-SYS-02 — версия сборки и её состав
# ---------------------------------------------------------------------------
def test_version_records_build_in_session():
    """Состав сборки попадает в сессию и в доказательства проверки."""
    context, _ = stand(response, "TC-SYS-02")

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert context.session.server_version["revision"] == "abc1234"
    assert context.session.server_build == "main@abc1234"
    assert outcome.evidence["fields_missing"] == []
    assert outcome.evidence["build"] == "main@abc1234"


def test_version_reports_missing_fields_as_fact():
    """Нехватка полей сборки — факт в вердикте, а не отказ проверки."""
    context, _ = stand(
        lambda request: httpx.Response(200, json={"branch": "main", "revision": "abc1234"}),
        "TC-SYS-02",
    )

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["fields_missing"] == ["commit", "build_date"]
    assert "нет полей: commit, build_date" in outcome.verdict


def test_version_fails_on_empty_answer():
    """Пустой ответ `/version` — отказ: состав сборки подтвердить нельзя."""
    context, _ = stand(lambda request: httpx.Response(200, json={}), "TC-SYS-02")

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "пустой ответ" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-SYS-03 — поведение на неизвестном пути
# ---------------------------------------------------------------------------
def test_unknown_path_passes_on_404_with_detail():
    """404 с разобранным `detail` — ошибка обработана по NFR-2."""
    context, journal = stand(
        lambda request: httpx.Response(404, json={"detail": "Not Found"}), "TC-SYS-03"
    )

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["status"] == 404
    assert outcome.evidence["detail_kind"] == "str"
    assert journal.records[-1].path == "/api/__test_unknown__"
    assert journal.records[-1].label == "TC-SYS-03"


def test_unknown_path_fails_on_5xx():
    """5xx на неизвестном пути — отказ проверки (пользователю показывают не ту ошибку)."""
    context, _ = stand(lambda request: httpx.Response(503, text="unavailable"), "TC-SYS-03")

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "5xx" in outcome.verdict


def test_unknown_path_fails_without_detail():
    """404 без разобранного `detail` — отказ: формат ошибки не соответствует NFR-2."""
    context, _ = stand(lambda request: httpx.Response(404, text="<html>404</html>"), "TC-SYS-03")

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "detail" in outcome.verdict


def test_unknown_path_requires_probe_client():
    """Без клиента консоли (проверка вне пульта) проба не выполняется — «пропущена»."""
    context, _ = stand(response, "TC-SYS-03")
    context.probe = None

    outcome = check_runner.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "клиента консоли" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-SYS-04 — неизвестная задача
# ---------------------------------------------------------------------------
def test_unknown_task_passes_on_not_found_status():
    """Сервер отвечает карточкой со статусом `not_found` — вариант зафиксирован."""
    context, _ = stand(response, "TC-SYS-04")

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["statuses"] == ["not_found"]


def test_unknown_task_passes_on_404():
    """404 на карточку неизвестной задачи — тоже согласованный вариант (BR-R7)."""
    context, _ = stand(
        lambda request: httpx.Response(404, json={"detail": "task not found"}), "TC-SYS-04"
    )

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["variant"] == "404"


def test_unknown_task_fails_on_other_statuses():
    """Карточка без `not_found` — отказ: пульт не различит «не найдена» и «есть»."""
    context, _ = stand(
        lambda request: httpx.Response(
            200,
            json={
                "type": "celery-test",
                "name": "test",
                "id": TASK_ID,
                "created_at": "2026-09-01T10:00:00",
                "runtimes": [
                    {
                        "task_id": TASK_ID,
                        "status": "new",
                        "parameters": {},
                        "id": "33333333-3333-4333-8333-333333333333",
                    }
                ],
            },
        ),
        "TC-SYS-04",
    )

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "not_found" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-SYS-05 — единый формат ошибок
# ---------------------------------------------------------------------------
def test_error_format_passes_for_consistent_detail():
    """`detail[]` и строка `detail` — разобранные ошибки: проверка пройдена."""
    context, journal = stand(response, "TC-SYS-05")

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["detail_kinds"] == ["HTTPValidationError.detail[]", "строка"]
    assert "форматы различаются между эндпоинтами" in outcome.verdict
    assert [record.label for record in journal.records] == ["TC-SYS-05", "TC-SYS-05"]


def test_error_format_fails_on_5xx():
    """5xx вместо ошибки валидации — отказ проверки."""
    context, _ = stand(lambda request: httpx.Response(500, text="boom"), "TC-SYS-05")

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "5xx" in outcome.verdict


def test_error_format_fails_without_detail():
    """Ответ без `detail` — формат ошибки не соблюдён."""
    context, _ = stand(lambda request: httpx.Response(400, text="bad request"), "TC-SYS-05")

    outcome = check_system.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "detail" in outcome.verdict


# ---------------------------------------------------------------------------
# Обвязка: движок и ручные проверки группы
# ---------------------------------------------------------------------------
def test_automate_records_system_check_in_session():
    """Прогон через движок сохраняет результат в сессии и ставит метку журнала."""
    context, journal = stand(response, "TC-SYS-01")

    result = check_runner.automate(context)

    assert result is not None
    assert result.status == CheckStatus.PASSED
    assert result.journal_from is not None and result.journal_to is not None
    assert context.session.checks[-1]["check_id"] == "TC-SYS-01"
    assert journal.records[-1].label == "TC-SYS-01"


def test_scenario_returns_none_for_manual_check():
    """У ручной проверки TC-SYS-06 сценария нет — её отмечает оператор."""
    manual = spec_of("TC-SYS-06")

    assert check_runner.scenario(manual) is None
    assert (
        check_runner.automate(
            check_runner.AutomationContext(session=new_session(base_url=BASE_URL), spec=manual)
        )
        is None
    )


@pytest.mark.parametrize(
    "check_id", ["TC-SYS-01", "TC-SYS-02", "TC-SYS-03", "TC-SYS-04", "TC-SYS-05"]
)
def test_failed_stand_never_raises(check_id: str):
    """Отказ стенда превращается в вердикт, а не в исключение сценария.

    TC-SYS-04 на 404 «проходит» осознанно: 404 — согласованный вариант ответа на
    неизвестную задачу (BR-R7).
    """
    context, _ = stand(lambda request: httpx.Response(404, text="nope"), check_id)

    outcome = check_runner.evaluate(context)

    assert outcome is not None
    assert outcome.status in {
        CheckStatus.PASSED,
        CheckStatus.FAILED,
        CheckStatus.SKIPPED,
        CheckStatus.BLOCKED,
    }
