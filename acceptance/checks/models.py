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
from acceptance.session import add_test_entity, mark_test_entity_deleted
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


#: Созданная сценарием сущность: тип для учёта сессии (`TC-CLEAN-01/02`).
ENTITY_MODEL = "модель"

#: `output_size` для создаваемых `__TEST__`-моделей (у моделей стенда он равен 2).
DEFAULT_OUTPUT_SIZE = 2

#: Фаза `__TEST__`-модели: у моделей стенда она `single` (две фазы, а не три).
DEFAULT_PHASE = "single"


def _test_model_name(context: AutomationContext) -> str:
    """Имя `__TEST__`-модели: `__TEST__<проверка>_<сессия>` (§9 ТЗ, NFR-T4)."""
    override = context.param("model_name")
    if override:
        return override
    session_id = str(context.session.session_id or "session")
    return f"{TEST_PREFIX}{context.spec.check_id.replace('-', '_')}_{session_id}"


def _pick_architecture(catalog_rows: list[ModelArchitectureInfo], phase: str) -> str:
    """Архитектура для создания модели: первая, поддерживающая нужную фазу."""
    for item in catalog_rows:
        phases = [str(value) for value in (item.suitable_for_phases or [])]
        if not phases or phase in phases:
            return item.architecture
    return catalog_rows[0].architecture if catalog_rows else "stft_mlp"


def _register_model(context: AutomationContext, model_id: str, *, note: str = "") -> None:
    """Регистрирует созданную `__TEST__`-модель в учёте сессии (NFR-T4)."""
    session_id = str(context.session.session_id)
    add_test_entity(
        context.session,
        entity_id=model_id,
        entity_type=ENTITY_MODEL,
        check_id=context.spec.check_id,
        note=note or f"создан проверкой {context.spec.check_id} в сессии {session_id}",
    )


def _delete_model(context: AutomationContext, model_id: str, *, reason: str = "") -> str:
    """Удаляет `__TEST__`-модель и отмечает это в учёте сессии (самоочистка).

    Returns:
        Текст для доказательств: «модель удалена» или причина неудачи.
    """
    try:
        context.models_api().delete_model(model_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return f"модель не удалена: {exc}"
    mark_test_entity_deleted(
        context.session, model_id, check_id=context.spec.check_id, note=reason or "самоочистка"
    )
    return "модель удалена"


def _delete_file(context: AutomationContext, file_id: str) -> str:
    """Удаляет файл, созданный пробой (`DELETE /api/{file_id}`), и отмечает в сессии."""
    try:
        context.files_api().delete(file_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return f"файл не удалён: {exc}"
    mark_test_entity_deleted(
        context.session, file_id, check_id=context.spec.check_id, note="самоочистка файла модели"
    )
    return "файл удалён"


def _file_exists(context: AutomationContext, file_id: str) -> bool:
    """Есть ли файл в реестре (`GET /api/data/files` с фильтром по `id`)."""
    try:
        rows = context.files_api().list_files(file_id=file_id).files
    except (ApiError, ClientError, ServerUnavailableError):
        return False
    return bool(rows)


def _model_exists(context: AutomationContext, model_id: str) -> bool:
    """Есть ли модель в реестре (`GET /api/ml_models/models` с фильтром по `model_id`)."""
    try:
        rows = context.models_api().list_models(model_id=model_id)
    except (ApiError, ClientError, ServerUnavailableError):
        return False
    return bool(rows)


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
    name = _test_model_name(context)

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


def _create(context: AutomationContext) -> CheckOutcome:
    """TC-MOD-02: создание `__TEST__`-модели с самоочисткой (UC-16, BR-F4, NFR-T4).

    Архитектура берётся из каталога и должна поддерживать фазу стенда; ответ
    проверяется по полям (`id`, `version`, `architecture`, `phase_type`,
    `output_size`), затем модель подтверждается реестром. Созданная модель удаляется
    в `finally`: испытания не оставляют следов, а неудачная уборка — отказ проверки.
    """
    try:
        catalog_rows = list(context.models_api().list_architectures())
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"каталог архитектур не получен: {exc}") from exc
    if not catalog_rows:
        raise PreconditionError("каталог архитектур пуст: архитектуру выбрать нечем")

    phase = str(context.param("phase_type") or DEFAULT_PHASE)
    architecture = str(context.param("architecture") or _pick_architecture(catalog_rows, phase))
    name = _test_model_name(context)
    evidence: dict[str, Any] = {
        "name": name,
        "architecture": architecture,
        "phase_type": phase,
        "output_size": int(context.param("output_size") or DEFAULT_OUTPUT_SIZE),
        "catalog_architectures": [item.architecture for item in catalog_rows],
    }
    problems: list[str] = []
    model_id = ""

    try:
        created = context.models_api().create_model(
            architecture=architecture,
            name=name,
            output_size=int(evidence["output_size"]),
            phase_type=phase,
            description=f"проверка {context.spec.check_id}",
        )
        model_id = str(created.id)
        _register_model(context, model_id)
        evidence.update(
            {
                "model_id": model_id,
                "version": str(created.version or ""),
                "created_architecture": str(created.architecture or ""),
                "created_phase_type": str(created.phase_type or ""),
                "created_output_size": int(created.output_size or 0),
            }
        )
        missing = [
            field
            for field in ("id", "version", "architecture", "phase_type", "output_size")
            if not getattr(created, field, None)
        ]
        if missing:
            problems.append(f"в ответе нет полей: {', '.join(missing)}")
        if str(created.architecture) != architecture:
            problems.append(
                f"архитектура модели «{created.architecture}» не совпала с выбранной "
                f"«{architecture}»"
            )
        if architecture not in {item.architecture for item in catalog_rows}:
            problems.append(f"архитектура {architecture} отсутствует в каталоге")
        if not _model_exists(context, model_id):
            problems.append("созданная модель не видна в реестре `GET /api/ml_models/models`")
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        problems.append(f"создание модели не выполнено: {exc}")
    finally:
        evidence["cleanup"] = (
            _delete_model(context, model_id) if model_id else "нечего удалять: модель не создана"
        )
        if model_id:
            left = _model_exists(context, model_id)
            evidence["left_in_registry"] = left
            if not evidence["cleanup"].startswith("модель удалена") or left:
                problems.append(
                    "самоочистка не выполнена: `__TEST__`-модель осталась на стенде (NFR-T4)"
                )

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    verdict = (
        f"`__TEST__`-модель создана и удалена: {name} · {evidence['version']} "
        f"(архитектура {architecture}, фаза {phase})"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _upload(context: AutomationContext) -> CheckOutcome:
    """TC-MOD-04: загрузка готовой `.h5` с метаданными и самоочисткой (UC-17, NFR-T4).

    Файл `.h5` берётся со стенда (у модели-источника есть `file_id` в реестре
    файлов): так проверяется реальный контракт загрузки, а не синтетический файл.
    Модель-результат создаётся с именем `__TEST__…` и удаляется вместе со своим
    файлом: если удаление модели не убирает `.h5`, сценарий чистит файл отдельно.
    """
    source = _picked(context)
    file_id = str(source.file_id or "")
    if not file_id:
        raise PreconditionError(
            f"у модели «{source.name}» нет файла `.h5` в реестре: загружать нечего"
        )
    evidence: dict[str, Any] = {
        "source_model": model_label(source),
        "source_model_id": str(source.id),
        "source_file_id": file_id,
    }
    try:
        payload = context.files_api().download(file_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"файл модели не скачан: {exc}") from exc

    name = _test_model_name(context)
    file_name = payload.file_name or f"{TEST_PREFIX}model.h5"
    evidence.update(
        {
            "name": name,
            "file_name": file_name,
            "uploaded_bytes": payload.size,
        }
    )
    problems: list[str] = []
    model_id = ""
    new_file_id = ""

    try:
        created = context.models_api().upload_model(
            file_name=file_name,
            content=payload.content,
            name=name,
            output_size=int(
                context.param("output_size") or source.output_size or DEFAULT_OUTPUT_SIZE
            ),
            architecture=str(context.param("architecture") or source.architecture),
            phase_type=str(source.phase_type or DEFAULT_PHASE),
            description=f"проверка {context.spec.check_id} (копия {source.name})",
        )
        model_id = str(created.id)
        new_file_id = str(created.file_id or "")
        _register_model(context, model_id, note="создан проверкой TC-MOD-04 загрузкой `.h5`")
        evidence.update(
            {
                "model_id": model_id,
                "model_file_id": new_file_id,
                "version": str(created.version or ""),
                "created_architecture": str(created.architecture or ""),
                "in_registry": _model_exists(context, model_id),
            }
        )
        if not new_file_id:
            problems.append("в ответе загрузки нет `file_id`: модель без файла модели")
        if not evidence["in_registry"]:
            problems.append("загруженная модель не видна в реестре `GET /api/ml_models/models`")
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        problems.append(f"загрузка модели не выполнена: {exc}")
    finally:
        evidence["cleanup"] = (
            _delete_model(context, model_id) if model_id else "нечего удалять: модель не создана"
        )
        if model_id and _model_exists(context, model_id):
            problems.append("самоочистка не выполнена: загруженная модель осталась на стенде")
        if new_file_id and _file_exists(context, new_file_id):
            # удаление модели файл не убрало — чистим файл отдельно (каскада нет)
            evidence["file_cleanup"] = _delete_file(context, new_file_id)
            evidence["file_left_in_registry"] = _file_exists(context, new_file_id)
            if evidence["file_left_in_registry"]:
                problems.append(
                    "самоочистка не выполнена: файл `.h5` загруженной модели остался в реестре"
                )

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    verdict = (
        f"`.h5` ({evidence['uploaded_bytes']} Б) загружен и удалён: {name} · "
        f"{evidence['version']}; файл модели {evidence.get('file_cleanup', 'убран вместе с моделью')}"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _delete_model_scenario(context: AutomationContext) -> CheckOutcome:
    """TC-MOD-07: удаление модели и судьба её файла `.h5` (UC-20, NFR-T4).

    Проверка создаёт свою `__TEST__`-модель и удаляет именно её: удалять чужую
    модель нельзя. Фиксируется поведение после удаления — исчезновение из реестра,
    ответ `GET /api/ml_models/models/{id}` и судьба связанного файла в реестре
    файлов (каскад или отдельное удаление), а также ответ на повторное удаление.
    """
    name = _test_model_name(context)
    evidence: dict[str, Any] = {"name": name, "models_before": len(_models(context))}
    problems: list[str] = []
    model_id = ""
    file_id = ""

    try:
        try:
            catalog_rows = list(context.models_api().list_architectures())
        except (ApiError, ClientError, ServerUnavailableError):
            catalog_rows = []
        phase = str(context.param("phase_type") or DEFAULT_PHASE)
        created = context.models_api().create_model(
            architecture=str(
                context.param("architecture") or _pick_architecture(catalog_rows, phase)
            ),
            name=name,
            output_size=int(context.param("output_size") or DEFAULT_OUTPUT_SIZE),
            phase_type=phase,
            description=f"проверка {context.spec.check_id}",
        )
        model_id = str(created.id)
        file_id = str(created.file_id or "")
        _register_model(context, model_id, note="создан проверкой TC-MOD-07 для удаления")
        evidence.update({"model_id": model_id, "model_file_id": file_id})
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"подготовить `__TEST__`-модель для удаления не удалось: {exc}",
            evidence,
        )

    delete_result = context.probe_request("DELETE", f"/api/ml_models/models/{model_id}")
    evidence["delete_status"] = delete_result.status
    evidence["delete_detail"] = _detail_text(delete_result)
    if delete_result.status is None or not 200 <= int(delete_result.status) < 300:
        problems.append(f"удаление модели отклонено сервером ({delete_result.status})")

    evidence["gone_from_registry"] = not _model_exists(context, model_id)
    if not evidence["gone_from_registry"]:
        problems.append("модель осталась в реестре `GET /api/ml_models/models` после удаления")
    else:
        mark_test_entity_deleted(
            context.session, model_id, check_id=context.spec.check_id, note="удалён проверкой"
        )

    card_result = context.probe_request("GET", f"/api/ml_models/models/{model_id}")
    evidence["card_after_delete"] = card_result.status
    if card_result.status is not None and 200 <= int(card_result.status) < 300:
        problems.append("карточка удалённой модели всё ещё отдаётся сервером (ожидался 404)")

    # судьба связанного файла: каскад (файла нет) или отдельное удаление (файл остался)
    evidence["file_id"] = file_id
    evidence["file_cascade"] = bool(file_id) and not _file_exists(context, file_id)
    if file_id and not evidence["file_cascade"]:
        evidence["file_cleanup"] = _delete_file(context, file_id)
        evidence["file_left_in_registry"] = _file_exists(context, file_id)
        if evidence["file_left_in_registry"]:
            problems.append("связанный файл `.h5` остался в реестре файлов: нужна отдельная уборка")

    second = context.probe_request("DELETE", f"/api/ml_models/models/{model_id}")
    evidence["second_delete_status"] = second.status
    if second.status is not None and 200 <= int(second.status) < 300:
        evidence["second_delete_note"] = (
            "повторное удаление отвечает успехом: идемпотентность не подтверждена явно"
        )

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    cascade = (
        "файл `.h5` удалён вместе с моделью"
        if evidence["file_cascade"]
        else (f"файл `.h5` удаляется отдельно ({evidence.get('file_cleanup', 'убран сценарием')})")
    )
    verdict = (
        f"`__TEST__`-модель удалена (ответ {evidence['delete_status']}, карточка "
        f"{evidence['card_after_delete']}); {cascade}"
    )
    return CheckOutcome(
        CheckStatus.PASSED, verdict, evidence, note=str(evidence.get("second_delete_note") or "")
    )


#: Реестр сценариев модуля (ключ — `CheckSpec.automation`).
AUTOMATIONS: dict[str, Any] = {
    "models.architectures": _architectures,
    "models.create": _create,
    "models.create_validation": _create_validation,
    "models.upload": _upload,
    "models.list_filters": _list_filters,
    "models.card": _card,
    "models.delete": _delete_model_scenario,
}
