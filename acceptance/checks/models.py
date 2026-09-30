"""Автоматические сценарии проверок модуля «ML models» (TC-MOD, этап T6).

Проверки группы — дешёвые и, кроме проб валидации (`TC-MOD-03`), ничего на стенде
не меняют. Пробы `TC-MOD-03` проверяют, что неизвестная архитектура и привязка
`signals` к несуществующему `device_id` **отклоняются** сервером; если стенд всё-таки
создаст модель, сценарий удаляет её сразу и фиксирует отказ — испытания не оставляют
следов (NFR-T4).

Факты живого стенда, влияющие на вердикты (`llm_tasks/48`, `llm_tasks/50`):

    * `GET /api/ml_models/architectures` отдаёт JSON-массив с `description`,
      `default_config`, `suitable_for_phases` — по ним и проверяется каталог;
    * `signals` у живых моделей **заполнены** (`signal_num` → `device_id`), поэтому
      ожидание «`signals` пусты» из `docs/02` §10 закрыто: это примечание, а не отказ;
    * серверного среза `limit`/`offset` у списка моделей спецификация не описывает —
      проба фиксирует, игнорирует их сервер (200) или отклоняет (4xx), как в `TC-FILE-05`.

Автовыбор модели: сначала обученная (`metrics` заполнены), затем самая свежая
(`acceptance.ml_samples.pick_model`); параметр `model_id` на карточке проверки задаёт
модель явно.
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
from acceptance.endpoints import TEST_PREFIX
from acceptance.exchange import ExchangeResult
from acceptance.ml_samples import model_label, pick_model
from client.errors import ApiError, ClientError, ServerUnavailableError
from client.schemas import ModelArchitectureInfo, ModelMetadata

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: `output_size` проб создания модели: у моделей стенда он равен 2 (`stft_mlp`, чужая фаза).
PROBE_OUTPUT_SIZE = 2

#: Поля каталога архитектур, обязательные по спецификации (`ModelArchitectureInfo`).
ARCHITECTURE_FIELDS: tuple[str, ...] = ("description", "default_config", "suitable_for_phases")

#: Поля карточки модели, обязательные для проверки `TC-MOD-06`.
CARD_FIELDS: tuple[str, ...] = (
    "id",
    "file_id",
    "architecture",
    "version",
    "phase_type",
    "output_size",
    "created_at",
    "updated_at",
)


def _models(context: AutomationContext) -> list[ModelMetadata]:
    """Реестр моделей стенда.

    Raises:
        PreconditionError: реестр недоступен или пуст — проверки моделей неприменимы.
    """
    try:
        rows = list(context.models_api().list_models())
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр моделей не получен: {exc}") from exc
    if not rows:
        raise PreconditionError("реестр моделей пуст: проверки ML models неприменимы")
    return rows


def _picked(context: AutomationContext, *, with_metrics: bool = False) -> ModelMetadata:
    """Модель для проверки: выбранная оператором либо автоматически подобранная."""
    chosen = pick_model(
        _models(context), model_id=context.param("model_id"), with_metrics=with_metrics
    )
    if chosen is None:
        raise PreconditionError(
            "нет подходящей модели: выберите `model_id` на карточке проверки"
            if with_metrics
            else "нет модели в реестре: выберите `model_id` на карточке проверки"
        )
    return chosen


def _detail_text(result: ExchangeResult) -> str:
    """Текст ошибки сервера: строка `detail` или первая ошибка валидации."""
    body = result.body_json
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        return str(detail[0])
    return (result.body_text or result.error or "")[:300]


def _architectures(context: AutomationContext) -> CheckOutcome:
    """TC-MOD-01: каталог архитектур и состав его полей (UC-15).

    Каталог нужен и другим проверкам: из него берётся архитектура для создания
    `__TEST__`-модели (`TC-MOD-02`) и для проб валидации (`TC-MOD-03`).
    """
    try:
        rows: list[ModelArchitectureInfo] = list(context.models_api().list_architectures())
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"каталог архитектур не получен: {exc}") from exc
    if not rows:
        raise PreconditionError("каталог архитектур пуст: проверять нечего")

    problems: list[str] = []
    empty_fields: list[str] = []
    for item in rows:
        for field in ARCHITECTURE_FIELDS:
            if not getattr(item, field, None):
                empty_fields.append(f"{item.architecture}.{field}")
    if empty_fields:
        problems.append("пустые поля каталога: " + ", ".join(sorted(empty_fields)))

    names = [item.architecture for item in rows]
    evidence: dict[str, Any] = {
        "count": len(rows),
        "architectures": names,
        "fields": list(ARCHITECTURE_FIELDS),
        "default_phases": {
            item.architecture: list(item.suitable_for_phases or []) for item in rows
        },
        "empty_fields": empty_fields,
    }
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    return CheckOutcome(
        CheckStatus.PASSED,
        f"каталог архитектур получен: {len(rows)} ({', '.join(names)})",
        evidence,
    )


def _probe_name(context: AutomationContext) -> str:
    """Имя `__TEST__`-модели для проб валидации (по нему видно, чья проба)."""
    session_id = str(context.session.session_id or "session")
    return f"{TEST_PREFIX}{context.spec.check_id.replace('-', '_')}_{session_id}"


def _create_validation(context: AutomationContext) -> CheckOutcome:
    """TC-MOD-03: неизвестная архитектура и `signals` на чужой `device_id` — ошибка.

    Дешёвая проверка контракта (`BR-F4`, `NFR-5`): модель создаваться не должна.
    Если сервер всё-таки создал её (2xx), сценарий немедленно удаляет модель и
    фиксирует отказ: пульт не имеет права оставлять следы на стенде (NFR-T4).
    """
    try:
        catalog_rows = list(context.models_api().list_architectures())
    except (ApiError, ClientError, ServerUnavailableError):
        catalog_rows = []
    architecture = catalog_rows[0].architecture if catalog_rows else "stft_mlp"
    name = _probe_name(context)

    cases: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "неизвестная архитектура",
            {
                "architecture": f"{TEST_PREFIX}no-such-architecture",
                "name": name,
                "output_size": PROBE_OUTPUT_SIZE,
                "phase_type": "single",
            },
        ),
        (
            "signals на несуществующий device_id",
            {
                "architecture": architecture,
                "name": name,
                "output_size": PROBE_OUTPUT_SIZE,
                "phase_type": "single",
                "signals": {"0": [f"{TEST_PREFIX}no-such-device"]},
            },
        ),
        ("без обязательных полей", {"name": name}),
    )

    evidence: dict[str, Any] = {
        "model_name": name,
        "architecture": architecture,
        "catalog_architectures": [item.architecture for item in catalog_rows],
        "cases": [],
    }
    problems: list[str] = []
    for case, body in cases:
        result = context.probe_request("POST", "/api/ml_models/models", json_body=body)
        row: dict[str, Any] = {
            "case": case,
            "status": result.status,
            "detail": _detail_text(result),
        }
        if result.status is None:
            problems.append(f"{case}: запрос не отправлен ({result.error})")
            evidence["cases"].append(row)
            continue
        if 200 <= int(result.status) < 300:
            created_id = ""
            body_json = result.body_json
            if isinstance(body_json, dict):
                created_id = str(body_json.get("id") or "")
            row["created_id"] = created_id
            row["cleanup"] = _delete_created(context, created_id)
            problems.append(f"{case}: принято сервером ({result.status}) — ожидалась ошибка")
        elif int(result.status) >= 500:
            problems.append(f"{case}: серверная ошибка {result.status}")
        evidence["cases"].append(row)

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    codes = ", ".join(str(row["status"]) for row in evidence["cases"])
    return CheckOutcome(
        CheckStatus.PASSED,
        f"пробы создания отклонены ({codes}): неизвестная архитектура и чужой `device_id` "
        "не принимаются, модель не создаётся",
        evidence,
    )


def _delete_created(context: AutomationContext, model_id: str) -> str:
    """Страховочная уборка модели, созданной пробой валидации (`TC-MOD-03`)."""
    if not model_id:
        return "нечего удалять: идентификатор модели в ответе не получен"
    try:
        context.models_api().delete_model(model_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return f"удалить не удалось: {exc}"
    return "модель удалена"


def _matches(row: ModelMetadata, attr: str, value: Any, *, substring: bool = False) -> bool:
    """Подходит ли модель под проверяемый фильтр (для вердикта `TC-MOD-05`).

    `device_id` — не поле карточки, а связь из `signals` (`signal_num` → `device_id`),
    поэтому проверяется по списку привязок.
    """
    if attr == "device_id":
        return any(
            str(signal.get("device_id") or "") == str(value)
            for signal in (row.signals or [])
            if isinstance(signal, dict)
        )
    actual = getattr(row, attr, None)
    if substring:
        return str(value) in str(actual or "")
    if isinstance(value, int):
        return int(actual or 0) == value
    return str(actual) == str(value)


def _device_id(rows: list[ModelMetadata]) -> str:
    """`device_id` из привязок `signals` любой модели (может отсутствовать)."""
    for row in rows:
        for signal in row.signals or []:
            found = str(signal.get("device_id") or "") if isinstance(signal, dict) else ""
            if found:
                return found
    return ""


def _device_from_card(context: AutomationContext, model_id: str) -> str:
    """`device_id` из карточки модели: список `signals` не отдаёт (`TC-MOD-05`).

    Ошибка чтения карточки не отменяет проверку фильтров — она лишь оставляет
    фильтр `device_id` непроверенным, о чём сценарий пишет примечанием.
    """
    try:
        card = context.models_api().get_model(model_id)
    except (ApiError, ClientError, ServerUnavailableError):
        return ""
    return _device_id([card])


def _verify_device_filter(
    context: AutomationContext, device: str, found: list[ModelMetadata]
) -> tuple[dict[str, Any], list[str]]:
    """Проверяет результат фильтра `device_id` по карточкам найденных моделей.

    Список моделей привязки `signals` не отдаёт, поэтому «своя» модель проверяется
    карточкой: если сервер игнорирует фильтр, среди найденных окажутся модели без
    этой привязки — это и есть отказ с доказательством по каждой модели.
    """
    api = context.models_api()
    without: list[str] = []
    unreadable: list[str] = []
    for row in found:
        try:
            card = api.get_model(row.id)
        except (ApiError, ClientError, ServerUnavailableError):
            unreadable.append(str(row.id))
            continue
        if not _matches(card, "device_id", device):
            without.append(str(row.id))

    evidence: dict[str, Any] = {
        "filter": "device_id",
        "value": device,
        "returned": len(found),
        "foreign": len(without),
        "checked_by_card": len(found) - len(unreadable),
    }
    issues: list[str] = []
    if unreadable:
        evidence["unreadable"] = unreadable
        evidence["note"] = "карточки части моделей не прочитаны: результат проверен частично"
    if without:
        issues.append(f"фильтр device_id вернул {len(without)} моделей без привязки к «{device}»")
    return evidence, issues


def _list_filters(context: AutomationContext) -> CheckOutcome:
    """TC-MOD-05: состав списка моделей и работа фильтров (UC-18, NFR-4).

    Фильтры проверяются по одному: каждый должен вернуть **только** подходящие
    модели. Значение для фильтра `device_id` берётся из карточки модели: список
    `signals` не отдаёт (проверено на живом стенде 30.09.2026), а связь
    `signal_num` → `device_id` есть только в `GET /api/ml_models/models/{id}`.
    Серверные срезы `limit`/`offset` спецификацией не описаны — проба фиксирует
    факт (игнорирует или отклоняет) отдельным примечанием.
    """
    rows = _models(context)
    sample = rows[0]
    api = context.models_api()

    cases: list[tuple[str, str, Any, bool]] = [
        ("model_id", "id", str(sample.id), False),
        ("architecture", "architecture", str(sample.architecture), False),
        ("version", "version", str(sample.version), False),
        ("name", "name", str(sample.name), True),
        ("phase_type", "phase_type", str(sample.phase_type), False),
        ("output_size", "output_size", int(sample.output_size), False),
    ]
    device = context.param("device_id") or _device_id(rows)
    if not device:
        device = _device_from_card(context, str(sample.id))
    if device:
        cases.append(("device_id", "device_id", device, False))

    notes: list[str] = []
    evidence: dict[str, Any] = {
        "total_models": len(rows),
        "sample": {"model_id": str(sample.id), "name": sample.name},
        "filters": [],
        "device_id": device,
    }
    if not device:
        notes.append(
            "фильтр `device_id` не проверен: ни у одной модели нет привязок `signals` — "
            "связь класс-сигнал заполняется при обучении"
        )
    problems: list[str] = []
    for param, attr, value, substring in cases:
        try:
            found = list(api.list_models(**{param: value}))
        except (ApiError, ClientError, ServerUnavailableError) as exc:
            status = getattr(exc, "status_code", "")
            problems.append(
                f"фильтр {param} не отвечает: {type(exc).__name__} {status} — {exc}".strip()
            )
            evidence["filters"].append(
                {
                    "filter": param,
                    "value": value,
                    "status": status,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if param == "device_id":
            device_evidence, issues = _verify_device_filter(context, value, found)
            problems.extend(issues)
            evidence["filters"].append(device_evidence)
            continue
        foreign = [row for row in found if not _matches(row, attr, value, substring=substring)]
        row_evidence: dict[str, Any] = {
            "filter": param,
            "value": value,
            "returned": len(found),
            "foreign": len(foreign),
        }
        if not found:
            problems.append(f"фильтр {param}={value} не вернул ни одной модели")
        elif foreign:
            problems.append(
                f"фильтр {param} вернул {len(foreign)} моделей, не подходящих под условие"
            )
        evidence["filters"].append(row_evidence)

    list_result = context.probe_request("GET", "/api/ml_models/models")
    if list_result.ok:
        body = list_result.body_json
        if isinstance(body, dict):
            counted = int(body.get("count") or 0)
            returned = len(body.get("models") or [])
            evidence["count"] = counted
            evidence["returned"] = returned
            if counted != len(rows):
                problems.append(f"`count`={counted} не совпадает с числом моделей {len(rows)}")

    limit_result = context.probe_request("GET", "/api/ml_models/models", query={"limit": 1})
    if limit_result.ok:
        body = limit_result.body_json
        returned = len(body.get("models") or []) if isinstance(body, dict) else 0
        evidence["limit_returned"] = returned
        if returned != 1:
            notes.append(
                f"`limit=1` проигнорирован сервером (вернулось {returned}) — серверного "
                "среза у списка моделей нет (замечание, как в TC-FILE-05)"
            )
    elif limit_result.status is not None:
        evidence["limit_status"] = limit_result.status
        notes.append(f"`limit=1` отклонён сервером ({limit_result.status})")

    if problems:
        return CheckOutcome(
            CheckStatus.FAILED, "; ".join(problems), evidence, note="; ".join(notes)
        )
    working = len([row for row in evidence["filters"] if row.get("foreign") == 0])
    verdict = (
        f"список моделей и фильтры работают: {working} из {len(cases)} проверены, "
        f"моделей в реестре {len(rows)}"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence, note="; ".join(notes))


def _card(context: AutomationContext) -> CheckOutcome:
    """TC-MOD-06: карточка модели, метрики, `signals` и `signal_aliases` (UC-19).

    Ожидание `docs/02` §10 («`signals` пусты») на текущей сборке **закрыто**: у живых
    моделей привязки заполнены. Сценарий не превращает это в отказ, а фиксирует
    примечанием — как поступили с закрытым `TC-LOAD-02` при смене контракта.
    """
    rows = _models(context)
    chosen = pick_model(rows, model_id=context.param("model_id"), with_metrics=True) or pick_model(
        rows, model_id=context.param("model_id")
    )
    if chosen is None:
        raise PreconditionError("нет модели в реестре: выберите `model_id` на карточке проверки")

    try:
        card = context.models_api().get_model(chosen.id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"карточка модели не получена: {exc}") from exc

    missing = [field for field in CARD_FIELDS if not getattr(card, field, None)]
    metrics = dict(card.metrics or {})
    signals = [item for item in (card.signals or []) if isinstance(item, dict)]
    aliases = dict(card.signal_aliases or {})

    evidence: dict[str, Any] = {
        "model_id": str(card.id),
        "model_label": model_label(card),
        "file_id": str(card.file_id or ""),
        "fields": list(CARD_FIELDS),
        "missing": missing,
        "created_at": str(card.created_at or ""),
        "updated_at": str(card.updated_at or ""),
        "metrics_keys": sorted(metrics),
        "metrics_filled": bool(metrics),
        "signals": len(signals),
        "signal_aliases": aliases,
        "config_keys": sorted((card.config or {}).keys()),
    }
    if missing:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"карточка модели неполная: нет полей {', '.join(missing)}",
            evidence,
        )

    notes: list[str] = []
    if metrics:
        notes.append(f"метрики заполнены ({len(metrics)} полей, обучение было)")
    else:
        notes.append("метрики пусты (модель не обучалась)")
    if signals:
        notes.append(
            f"`signals` заполнены ({len(signals)} привязок): ожидание «`signals` пусты» "
            "из чек-листа закрыто"
        )
    else:
        notes.append("`signals` пусты — замечание чек-листа остаётся")
    if not aliases:
        notes.append("`signal_aliases` пусты: классы сигналов не подписаны")

    verdict = (
        f"карточка модели получена: {card.name}; архитектура {card.architecture}; "
        f"версия {card.version}; обновлена {str(card.updated_at)[:19]}"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence, note="; ".join(notes))


#: Реестр сценариев модуля (ключ — `CheckSpec.automation`).
AUTOMATIONS: dict[str, Any] = {
    "models.architectures": _architectures,
    "models.create_validation": _create_validation,
    "models.list_filters": _list_filters,
    "models.card": _card,
}
