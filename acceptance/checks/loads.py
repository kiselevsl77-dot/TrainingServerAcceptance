"""Автоматические сценарии проверок модуля «Loads» (TC-LOAD-01…07, этап T3).

Проверки читают реестр нагрузок и сравнивают его с требованиями постановки (FR-3,
UC-06…UC-08). Контракт 17.09.2026 снял требование фазы подключения, поэтому прежние
ожидаемые дефекты P0 закрыты и проверки стали **фиксацией факта**:

    * `TC-LOAD-02` — состав полей элемента реестра сверяется со схемой `LoadDevice`
      (`phase_connection` из контракта убран; «ожидаемого» дефекта больше нет);
    * `TC-LOAD-03` — проба неизвестного параметра `ph_n` (из контракта убран):
      фиксируется, игнорирует его сервер (200) или отклоняет (400/422).

`TC-LOAD-07` — негативная проба: маршрута удаления нагрузки нет. Проба удаляет
**выбранную** нагрузку, поэтому предпочитает `__TEST__…`-идентификатор, а если такого
нет — первую в реестре (иначе факт «маршрут отсутствует» не доказать); параметр
`load_id` позволяет оператору указать нагрузку явно.
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
from client.errors import ApiError, ClientError, ServerUnavailableError
from client.schemas import LoadItem, LoadsListResponse

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: Предел выдачи при чтении реестра целиком (верхняя граница спецификацией не объявлена).
LIST_LIMIT = 1000

#: Служебная категория, которую завела загрузка данных (замечание №2).
OTHER_CATEGORY = "__OTHER__"

#: Ожидаемый дефект: поля фазы нет в элементе реестра.
MISSING_PHASE = "phase_connection"


def _response(context: AutomationContext) -> LoadsListResponse:
    """Реестр нагрузок целиком (`GET /api/loads/list`).

    Raises:
        PreconditionError: реестр недоступен — проверять нечего.
    """
    try:
        return context.loads_api().list_loads(limit=LIST_LIMIT, offset=0)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр нагрузок не получен: {exc}") from exc


def _loads(context: AutomationContext) -> list[LoadItem]:
    """Элементы реестра нагрузок.

    Raises:
        PreconditionError: реестр пуст — проверки Loads неприменимы.
    """
    items = list(_response(context).loads)
    if not items:
        raise PreconditionError("реестр нагрузок пуст: проверки Loads неприменимы")
    return items


def _registry(context: AutomationContext) -> CheckOutcome:
    """TC-LOAD-01: реестр получен, `result_size` совпадает, срез `limit`/`offset` работает."""
    response = _response(context)
    items = list(response.loads)
    if not items:
        raise PreconditionError("реестр нагрузок пуст: срез limit/offset проверить нельзя")
    evidence: dict[str, Any] = {
        "result_size": response.result_size,
        "returned": len(items),
        "limit": response.limit,
        "offset": response.offset,
        "fields": list(LoadItem.model_fields),
    }
    problems: list[str] = []
    if response.result_size < len(items):
        problems.append(
            f"result_size={response.result_size} меньше числа элементов loads={len(items)}"
        )

    try:
        first = context.loads_api().list_loads(limit=1, offset=0)
        second = context.loads_api().list_loads(limit=1, offset=1)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        problems.append(f"срез limit/offset не отвечает: {exc}")
    else:
        evidence["page_first"] = len(first.loads)
        evidence["page_second"] = len(second.loads)
        if len(first.loads) > 1 or len(second.loads) > 1:
            problems.append("страница limit=1 вернула больше одной нагрузки")
        first_ids = {str(item.load_id) for item in first.loads}
        second_ids = {str(item.load_id) for item in second.loads}
        evidence["offset_works"] = bool(first_ids and second_ids and not (first_ids & second_ids))
        if not evidence["offset_works"]:
            evidence["offset_note"] = (
                "offset=1 вернул ту же нагрузку: серверный срез не различает страницы"
            )

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    verdict = f"реестр нагрузок получен: {len(items)} из {response.result_size}"
    if evidence.get("offset_works"):
        verdict += ", срез limit/offset различает страницы"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _fields(context: AutomationContext) -> CheckOutcome:
    """TC-LOAD-02: состав полей реестра против контракта 17.09.2026 (FR-3).

    Контракт снял требование фазы подключения: `phase_connection` убран из
    `LoadDevice`/`UpdateLoadRequest`, поэтому «поля фазы нет в ответе списка» больше не
    расхождение. Проверка фиксирует фактический состав полей элемента и его
    согласованность с обязательными полями `LoadDevice` (`load_id`, `category`).
    """
    items = _loads(context)
    fields = list(LoadItem.model_fields)
    required = ("load_id", "category")
    missing = [name for name in required if name not in fields]
    evidence: dict[str, Any] = {
        "fields": fields,
        "required": list(required),
        "missing": missing,
        "phase_connection": MISSING_PHASE in fields,
        "contract": "17.09.2026: требование фазы снято (`phase_connection` нет, `ph_n` убран)",
        "sample": {key: str(value) for key, value in items[0].model_dump().items()},
    }
    if missing:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"в элементе реестра нет обязательных полей {missing}: `LoadDevice` требует их",
            evidence,
        )
    verdict = f"состав полей реестра: {', '.join(fields)}"
    if MISSING_PHASE in fields:
        verdict += "; поле фазы присутствует в выдаче — зафиксировано"
    else:
        verdict += "; `phase_connection` в контракте 17.09.2026 нет — расхождение снято"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _unknown_param(context: AutomationContext) -> CheckOutcome:
    """TC-LOAD-03: `ph_n` отсутствует в контракте — фиксация поведения сервера (FR-3).

    Контракт 17.09.2026 убрал параметр `ph_n` из `GET /api/loads/list`. Проверка выясняет,
    как сервер относится к неизвестному параметру: игнорирует его (200 и та же выдача) или
    отклоняет запрос (400/422). Отказом считается только 5xx или сетевая ошибка.
    """
    items = _loads(context)
    probe = context.probe_request("GET", "/api/loads/list", query={"ph_n": "Ph_A"})
    body = probe.body_json if isinstance(probe.body_json, dict) else {}
    rows = body.get("loads") if isinstance(body.get("loads"), list) else None
    evidence: dict[str, Any] = {
        "query": probe.query,
        "status": probe.status,
        "full_size": len(items),
        "filtered_size": len(rows) if rows is not None else None,
        "contract": "параметр `ph_n` в контракте 17.09.2026 отсутствует",
        "error": probe.error or None,
    }
    if probe.status is None:
        return CheckOutcome(
            CheckStatus.FAILED, f"проба `ph_n` не отвечает: {probe.error}", evidence
        )
    status = int(probe.status)
    if status >= 500:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"проба `ph_n` отвечает {status} (5xx): {probe.body_text[:200]}",
            evidence,
        )
    if status in (400, 422):
        return CheckOutcome(
            CheckStatus.PASSED,
            f"сервер отклоняет неизвестный параметр `ph_n` (код {status}) — поведение зафиксировано",
            evidence,
        )
    if rows is None:
        return CheckOutcome(
            CheckStatus.FAILED, "ответ на `?ph_n=Ph_A` не содержит списка `loads`", evidence
        )
    if len(rows) < len(items):
        return CheckOutcome(
            CheckStatus.PASSED,
            f"сервер фильтрует по `ph_n`: {len(rows)} из {len(items)} нагрузок — параметр "
            "вернулся в сборку, сверить с контрактом 17.09.2026",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        "неизвестный параметр `ph_n` игнорируется: выдача та же, что без фильтра "
        "(параметра нет в контракте 17.09.2026) — зафиксировано",
        evidence,
    )


def _exact_filters(context: AutomationContext) -> CheckOutcome:
    """TC-LOAD-04: `load_id`/`category` фильтруются точно и регистрозависимо (UC-06, NFR-4)."""
    items = _loads(context)
    sample = items[0]
    load_id = str(sample.load_id)
    category = str(sample.category or "")
    evidence: dict[str, Any] = {
        "load_id": load_id,
        "category": category,
        "load_id_filter": None,
        "category_filter": None,
        "case_sensitive": {},
    }
    problems: list[str] = []

    try:
        by_id = context.loads_api().list_loads(load_id=load_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        problems.append(f"фильтр load_id не отвечает: {exc}")
    else:
        rows = list(by_id.loads)
        evidence["load_id_filter"] = len(rows)
        wrong = [str(item.load_id) for item in rows if str(item.load_id) != load_id]
        if not rows:
            problems.append(f"фильтр load_id={load_id} не вернул нагрузку, хотя она в реестре")
        if wrong:
            problems.append(f"фильтр load_id вернул другие идентификаторы: {wrong[:3]}")

        upper = load_id.upper()
        if upper != load_id:
            try:
                variant = list(context.loads_api().list_loads(load_id=upper).loads)
            except (ApiError, ClientError, ServerUnavailableError) as exc:
                variant = []
                evidence["case_sensitive"]["error_load_id"] = str(exc)
            evidence["case_sensitive"]["load_id"] = (
                "регистр влияет" if not variant else f"регистр не влияет ({len(variant)} строк)"
            )

    if category:
        try:
            by_category = list(context.loads_api().list_loads(category=category).loads)
        except (ApiError, ClientError, ServerUnavailableError) as exc:
            problems.append(f"фильтр category не отвечает: {exc}")
        else:
            evidence["category_filter"] = len(by_category)
            wrong = [str(item.category) for item in by_category if str(item.category) != category]
            if wrong:
                problems.append(f"фильтр category вернул другие категории: {wrong[:3]}")
            lowered = category.lower()
            if lowered != category:
                try:
                    variant = list(context.loads_api().list_loads(category=lowered).loads)
                except (ApiError, ClientError, ServerUnavailableError) as exc:
                    variant = []
                    evidence["case_sensitive"]["error_category"] = str(exc)
                evidence["case_sensitive"]["category"] = (
                    "регистр влияет" if not variant else f"регистр не влияет ({len(variant)} строк)"
                )

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    verdict = f"точные фильтры: load_id → {evidence['load_id_filter']}"
    if category:
        verdict += f", category → {evidence['category_filter']}"
    facts = [
        f"{key}: {value}"
        for key, value in evidence["case_sensitive"].items()
        if key in ("load_id", "category")
    ]
    if facts:
        verdict += " · " + "; ".join(facts)
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _description_search(context: AutomationContext) -> CheckOutcome:
    """TC-LOAD-05: `description_search` ищет по подстроке (UC-06, NFR-4)."""
    items = _loads(context)
    described = [item for item in items if str(item.description or "").strip()]
    if not described:
        raise PreconditionError("в реестре нет нагрузок с описанием: поиск проверить нельзя")

    description = str(described[0].description or "")
    token = next(
        (word for word in description.replace(",", " ").split() if len(word) >= 4),
        description[:6],
    )
    try:
        rows = list(context.loads_api().list_loads(description_search=token).loads)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"поиск по описанию не отвечает: {exc}",
            {"token": token, "error": str(exc)},
        )

    evidence: dict[str, Any] = {
        "token": token,
        "found": len(rows),
        "of_total": len(items),
        "examples": [str(item.description or "")[:80] for item in rows[:3]],
    }
    if not rows:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"подстрока «{token}» есть в описании нагрузки, но поиск вернул пустую выдачу",
            evidence,
        )
    wrong = [
        str(item.load_id)
        for item in rows
        if token.lower() not in str(item.description or "").lower()
    ]
    if wrong:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"поиск по описанию вернул нагрузки без подстроки: {wrong[:3]}",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"поиск по описанию: «{token}» → {len(rows)} из {len(items)} нагрузок",
        evidence,
    )


def _categories(context: AutomationContext) -> CheckOutcome:
    """TC-LOAD-06: качество справочника категорий — регистровые дубли и `__OTHER__` (замечание №2)."""
    items = _loads(context)
    raw = sorted({str(item.category) for item in items if str(item.category or "").strip()})
    grouped: dict[str, list[str]] = {}
    for category in raw:
        grouped.setdefault(category.casefold(), []).append(category)
    duplicates = [values for values in grouped.values() if len(values) > 1]
    other = [category for category in raw if category.strip().upper() == OTHER_CATEGORY]

    evidence: dict[str, Any] = {
        "categories_total": len(raw),
        "categories": raw[:20],
        "case_duplicates": duplicates[:10],
        "other_category": other,
        "loads": len(items),
    }
    facts: list[str] = []
    if duplicates:
        facts.append(
            "регистровые дубли категорий: " + "; ".join("/".join(item) for item in duplicates[:3])
        )
    if other:
        facts.append(f"служебная категория `{OTHER_CATEGORY}` присутствует")
    verdict = (
        "справочник категорий: " + " · ".join(facts)
        if facts
        else f"справочник категорий: {len(raw)} значений, дублей и служебных записей нет"
    )
    if facts:
        verdict += " (замечание о справочнике категорий)"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _missing_delete(context: AutomationContext) -> CheckOutcome:
    """TC-LOAD-07: маршрута удаления нагрузки нет — 404/405 (замечание к API P2)."""
    items = _loads(context)
    chosen = context.param("load_id")
    if not chosen:
        preferred = next(
            (item for item in items if str(item.load_id).startswith(TEST_PREFIX)), items[0]
        )
        chosen = str(preferred.load_id)

    known = next((item for item in items if str(item.load_id) == chosen), None)
    probe = context.probe_request("DELETE", f"/api/loads/{chosen}")
    detail = probe.body_text[:300]
    evidence: dict[str, Any] = {
        "load_id": chosen,
        "status": probe.status,
        "detail": detail,
        "was_in_registry": known is not None,
        "note": "удаления нагрузок в API нет: ошибочную нагрузку убрать нельзя (замечание P2)",
        "error": probe.error or None,
    }
    if probe.status is None:
        return CheckOutcome(
            CheckStatus.FAILED, f"проба удаления не отвечает: {probe.error}", evidence
        )
    if 200 <= int(probe.status) <= 299:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"DELETE /api/loads/{chosen} вернул {probe.status}: нагрузка удалена, "
            "ожидалось отсутствие маршрута — проверьте реестр нагрузок",
            evidence,
        )
    if 500 <= int(probe.status) <= 599:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"DELETE /api/loads/{chosen} отвечает {probe.status} (5xx)",
            evidence,
        )
    if int(probe.status) in (404, 405):
        return CheckOutcome(
            CheckStatus.PASSED,
            f"удаления нагрузки нет: {probe.status} — нагрузку через API убрать нельзя "
            "(замечание к API P2)",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"DELETE /api/loads/{chosen} отвечает {probe.status} — вариант зафиксирован",
        evidence,
    )


#: Сценарии проверок модуля «Loads»: ключ — `CheckSpec.automation`.
AUTOMATIONS = {
    "loads.registry": _registry,
    "loads.fields": _fields,
    "loads.unknown_param": _unknown_param,
    "loads.exact_filters": _exact_filters,
    "loads.description_search": _description_search,
    "loads.categories": _categories,
    "loads.missing_delete": _missing_delete,
}
