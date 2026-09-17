"""Автоматические сценарии проверок модуля «Система» (TC-SYS-01…05, этап T3).

Проверки этой группы — «дешёвые» и безопасные: чтение `/health` и `/version`,
негативные пробы неизвестного пути и неизвестной задачи, сравнение формата ошибок
валидации. Все они работают через контекст проверки (`AutomationContext`), поэтому
получают метку `TC-SYS-NN`, диапазон журнала и запись в сессии.

TC-SYS-06 — ручная проверка (оператор сравнивает сборки двух сессий), сценария нет.
"""

from __future__ import annotations

from typing import Any

from acceptance.checks.registry import CheckStatus
from acceptance.checks.runner import (
    AutomationContext,
    CheckOutcome,
    PreconditionError,
    automate,
    evaluate,
    scenario,
)
from client.errors import ApiError, ClientError, NotFoundError, ServerUnavailableError

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: Задача, которой заведомо нет в реестре (валидный UUID — BR-R7, TC-SYS-04).
DEFAULT_UNKNOWN_TASK = "00000000-0000-4000-8000-000000000000"

#: Поля сборки, наличие которых проверяет TC-SYS-02 (BR-R8).
BUILD_FIELDS: tuple[str, ...] = ("branch", "revision", "commit", "build_date")

#: Негативные пробы формата ошибок (TC-SYS-05): метод, путь и query.
ERROR_FORMAT_PROBES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", "/api/data/files", {"id": "not-a-uuid"}),
    ("GET", "/api/datasets/not-a-uuid", None),
)


def _health(context: AutomationContext) -> CheckOutcome:
    """TC-SYS-01: `GET /health` отвечает 200 — сервис доступен (FR-1, UC-01, BR-R8)."""
    try:
        context.system_api().health()
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(CheckStatus.FAILED, f"сервис недоступен: {exc}", {"error": str(exc)})
    return CheckOutcome(
        CheckStatus.PASSED,
        "GET /health отвечает 200: сервис помечен доступным",
        {"endpoint": "get /health", "status": 200},
    )


def _version(context: AutomationContext) -> CheckOutcome:
    """TC-SYS-02: ответ `/version` разобран, состав сборки зафиксирован в сессии (BR-R8)."""
    try:
        version = context.system_api().version()
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(CheckStatus.FAILED, f"версия не получена: {exc}", {"error": str(exc)})

    if not version:
        return CheckOutcome(
            CheckStatus.FAILED,
            "GET /version вернул пустой ответ: состав сборки подтвердить нельзя",
            {"endpoint": "get /version", "fields_total": len(BUILD_FIELDS)},
        )

    present = [field for field in BUILD_FIELDS if str(version.get(field) or "").strip()]
    missing = [field for field in BUILD_FIELDS if field not in present]
    if not context.session.server_version:
        # сборка сохраняется в сессии — попадает в шапку отчёта (BR-R8)
        context.session.server_version = dict(version)

    evidence: dict[str, Any] = {
        "endpoint": "get /version",
        "fields_present": present,
        "fields_missing": missing,
        "build": context.session.server_build,
        "response": {key: str(value) for key, value in version.items()},
    }
    verdict = f"сборка {context.session.server_build}; заполнено полей: {len(present)}"
    if missing:
        verdict += " · нет полей: " + ", ".join(missing)
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _unknown_path(context: AutomationContext) -> CheckOutcome:
    """TC-SYS-03: неизвестный путь отвечает 404 (не 5xx) с разобранной ошибкой (NFR-2)."""
    result = context.probe_request("GET", "/api/__test_unknown__")
    body = result.body_json
    detail = body.get("detail") if isinstance(body, dict) else None
    evidence: dict[str, Any] = {
        "path": result.path,
        "status": result.status,
        "detail": detail if isinstance(detail, str) else None,
        "detail_kind": type(detail).__name__ if detail is not None else "нет `detail`",
        "body": result.body_text[:400],
        "error": result.error or None,
    }

    if result.status is None:
        return CheckOutcome(
            CheckStatus.FAILED, f"нет ответа на неизвестный путь: {result.error}", evidence
        )
    if 500 <= int(result.status) <= 599:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"неизвестный путь отвечает {result.status} (5xx): ошибка сервера вместо 404",
            evidence,
        )
    if int(result.status) != 404:
        return CheckOutcome(
            CheckStatus.FAILED, f"неизвестный путь отвечает {result.status} вместо 404", evidence
        )
    if detail is None:
        return CheckOutcome(
            CheckStatus.FAILED,
            "404 без разобранного `detail`: формат ошибки не соответствует NFR-2",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"неизвестный путь: 404, ошибка разобрана как `detail` ({evidence['detail_kind']})",
        evidence,
    )


def _unknown_task(context: AutomationContext) -> CheckOutcome:
    """TC-SYS-04: неизвестная задача отдаётся как `not_found` либо 404 (BR-R7)."""
    unknown = context.param("unknown_task_id", DEFAULT_UNKNOWN_TASK)
    try:
        card = context.tasks_api().get_task(unknown)
    except NotFoundError as exc:
        return CheckOutcome(
            CheckStatus.PASSED,
            f"неизвестная задача: 404 — пульт различает «не найдена» и «сервер недоступен» ({exc})",
            {"task_id": unknown, "variant": "404", "error": str(exc)},
        )
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"неизвестная задача: неожиданный ответ ({type(exc).__name__}): {exc}",
            {"task_id": unknown, "error": str(exc)},
        )

    statuses = sorted({str(runtime.status) for runtime in card.runtimes})
    evidence: dict[str, Any] = {
        "task_id": unknown,
        "variant": "карточка со статусом",
        "runtimes": len(card.runtimes),
        "statuses": statuses,
    }
    if "not_found" in statuses:
        return CheckOutcome(
            CheckStatus.PASSED,
            "неизвестная задача: статус `not_found` — вариант зафиксирован",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.FAILED,
        "неизвестная задача: сервер ответил карточкой без статуса `not_found` "
        f"(статусы: {', '.join(statuses) or 'нет прогонов'})",
        evidence,
    )


def _error_format(context: AutomationContext) -> CheckOutcome:
    """TC-SYS-05: формат ошибок валидации согласован (`detail[]` либо строка) (NFR-2)."""
    rows: list[dict[str, Any]] = []
    problems: list[str] = []

    for method, path, query in ERROR_FORMAT_PROBES:
        result = context.probe_request(method, path, query=query)
        body = result.body_json
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, list):
            kind = "HTTPValidationError.detail[]"
            size = len(detail)
            first = str(detail[0]) if detail else ""
        elif isinstance(detail, str):
            kind = "строка"
            size = 1
            first = detail
        else:
            kind = "нет `detail`"
            size = 0
            first = result.body_text[:200]
        rows.append(
            {
                "path": result.path,
                "query": dict(query or {}),
                "status": result.status,
                "detail_kind": kind,
                "detail_size": size,
                "detail_example": first[:200],
                "error": result.error or None,
            }
        )
        if result.status is None:
            problems.append(f"{result.path}: нет ответа ({result.error})")
        elif 500 <= int(result.status) <= 599:
            problems.append(f"{result.path}: {result.status} (5xx) вместо ошибки валидации")
        elif detail is None:
            problems.append(f"{result.path}: {result.status} без разобранного `detail`")

    kinds = sorted({str(row["detail_kind"]) for row in rows})
    evidence: dict[str, Any] = {"probes": rows, "detail_kinds": kinds}
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)

    verdict = "формат ошибок валидации согласован: " + ", ".join(
        f"{row['path']} → {row['status']}/{row['detail_kind']}" for row in rows
    )
    if len(kinds) > 1:
        verdict += " · форматы различаются между эндпоинтами (замечание к API)"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


#: Сценарии проверок модуля «Система»: ключ — `CheckSpec.automation`.
AUTOMATIONS = {
    "system.health": _health,
    "system.version": _version,
    "system.unknown_path": _unknown_path,
    "system.unknown_task": _unknown_task,
    "system.error_format": _error_format,
}
