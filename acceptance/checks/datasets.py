"""Автоматические сценарии проверок модуля «Datasets» (TC-DS-01…07, этап T5).

Проверки работают с живым реестром датасетов и всегда убирают за собой: изменяющие
сценарии создают только `__TEST__…`-датасеты, регистрируют их в сессии
(`session.add_test_entity`, NFR-T4) и удаляют в конце (перечень созданного/удалённого
попадает в отчёт).

Ключевые особенности контракта 17.09.2026 (замечания к API):

    * `GET /api/datasets/{id}` возвращает **только метаданные** — ни состава, ни
      агрегатов (записи/чанки), поэтому `TC-DS-03` работает через провайдер состава
      (`acceptance.dataset_composition`): если операция состава появится в реестре
      операций, проверка сама перейдёт в строгий режим и сверит состав с фактом
      наполнения пультом; пока её нет — фиксируется факт «состав недоступен (P1)»;
    * `POST /api/datasets/fill/{id}` отвечает 202 **без `task_id`**, поэтому
      `TC-DS-05` фиксирует состав до вызова (факт пульта), а задачу `dataset-fill`
      ищет в `GET /api/tasks/` (обходной путь монитора T4) и доводит её до
      терминального статуса;
    * `TC-DS-06` — дешёвая предпроверка валидации `fill` на **несуществующих**
      идентификаторах: боевой датасет в пробе не участвует.

Окна ожидания `TC-DS-05` настраиваются параметрами запуска (`task_wait`,
`task_timeout`) — в тестах они нулевые, в UI оператор видит значения по умолчанию.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from acceptance.checks.registry import CheckStatus
from acceptance.checks.runner import (
    AutomationContext,
    CheckOutcome,
    PreconditionError,
    automate,
    evaluate,
    scenario,
)
from acceptance.dataset_composition import (
    COMPOSITION_MODES,
    MODE_AUTO,
    SOURCE_HEURISTIC,
    SOURCE_LOCAL,
    SOURCE_SERVER,
    compare,
    composition_of,
    read_composition,
    record_composition,
    record_fill,
    server_available,
    server_summary_key,
)
from acceptance.endpoints import TEST_PREFIX
from acceptance.records import build_records
from acceptance.session import add_test_entity, mark_test_entity_deleted
from client.errors import ApiError, ClientError
from client.schemas import DatasetResponse, FileMetadataResponse
from lib.polling import poll_until
from lib.task_status import TaskStatus

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: Тип датасета по контракту (`DatasetType` — единственное значение).
DATASET_TYPE = "direct_fill"

#: Тип задачи, которую создаёт наполнение датасета (`dataset-fill` в FSM-1).
FILL_TASK_TYPE = "dataset-fill"

#: Сколько секунд искать задачу `dataset-fill` в списке задач (обходной путь P1).
FILL_TASK_WAIT = 30.0

#: Сколько секунд ждать терминального статуса задачи наполнения.
FILL_TASK_TIMEOUT = 600.0

#: Пауза между опросами задачи наполнения, секунды.
FILL_POLL_INTERVAL = 5.0

#: Состав полей метаданных датасета (контракт 17.09.2026).
DATASET_FIELDS: tuple[str, ...] = tuple(DatasetResponse.model_fields)


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------
def _api(context: AutomationContext) -> Any:
    """API датасетов (`client.datasets.DatasetsApi`).

    Raises:
        PreconditionError: стенд не настроен — проверять нечего.
    """
    return context.datasets_api()


def _all_datasets(context: AutomationContext) -> list[DatasetResponse]:
    """Реестр датасетов целиком (`GET /api/datasets/`).

    Raises:
        PreconditionError: реестр недоступен.
    """
    try:
        return list(_api(context).list_datasets().datasets)
    except (ApiError, ClientError) as exc:
        raise PreconditionError(f"реестр датасетов не получен: {exc}") from exc


def _datasets(context: AutomationContext) -> list[DatasetResponse]:
    """Реестр датасетов, непустой (иначе проверка «пропущена»).

    Raises:
        PreconditionError: реестр пуст — проверять нечего.
    """
    items = _all_datasets(context)
    if not items:
        raise PreconditionError("реестр датасетов пуст: проверки Datasets неприменимы")
    return items


def _pick(context: AutomationContext, items: list[DatasetResponse]) -> DatasetResponse:
    """Датасет для проверки: выбранный оператором (`dataset_id`) или первый в реестре."""
    chosen = context.param("dataset_id")
    if chosen:
        found = next((item for item in items if str(item.id) == chosen), None)
        if found is None:
            raise PreconditionError(f"датасет {chosen} не найден в реестре: обновите список")
        return found
    return items[0]


def _new_name(prefix: str = "dataset") -> str:
    """Имя `__TEST__`-датасета (уникальное, распознаётся уборкой TC-CLEAN-01)."""
    return f"{TEST_PREFIX}{prefix}-{uuid4().hex[:8]}"


def _create(
    context: AutomationContext, *, check_id: str, description: str = "", prefix: str = "dataset"
) -> DatasetResponse:
    """Создаёт `__TEST__`-датасет и регистрирует создание в сессии (NFR-T4)."""
    name = _new_name(prefix)
    created = _api(context).create_dataset(
        name=name,
        description=description or f"создан проверкой {check_id}",
        dataset_type=DATASET_TYPE,
    )
    add_test_entity(
        context.session,
        entity_id=created.id,
        entity_type="dataset",
        check_id=check_id,
        note=f"имя {name}",
    )
    return created


def _delete(context: AutomationContext, dataset_id: Any, *, check_id: str, note: str = "") -> str:
    """Удаляет датасет и закрывает учёт в сессии; возвращает описание результата."""
    try:
        _api(context).delete_dataset(dataset_id)
    except ApiError as exc:
        return f"HTTP {exc.status_code}: {exc}"
    except ClientError as exc:
        return f"ошибка удаления: {exc}"
    mark = mark_test_entity_deleted(
        context.session, dataset_id, check_id=check_id, note=note or "самоочистка проверки"
    )
    return "удалён" if mark is not None else "удалён (в учёте сессии не значился)"


def _float_param(context: AutomationContext, name: str, default: float) -> float:
    """Числовой параметр запуска (значение по умолчанию — при отсутствии или ошибке)."""
    raw = context.param(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_param(context: AutomationContext, name: str, default: int) -> int:
    """Целочисленный параметр запуска (значение по умолчанию — при ошибке ввода)."""
    return max(1, int(_float_param(context, name, float(default))))


def _composition_mode(context: AutomationContext) -> str:
    """Режим чтения состава из карточки запуска (`auto` по умолчанию)."""
    mode = context.param("composition_mode") or MODE_AUTO
    return mode if mode in COMPOSITION_MODES else MODE_AUTO


def _registry_files(context: AutomationContext) -> list[FileMetadataResponse]:
    """Реестр файлов для состава и подбора пары наполнения (ошибка — пустой список)."""
    try:
        return list(context.files_api().list_files().files)
    except (ApiError, ClientError):
        return []


def _pair_candidates(
    files: list[FileMetadataResponse], count: int
) -> list[tuple[FileMetadataResponse, FileMetadataResponse]]:
    """Пары RAW+markup для наполнения: сначала с ASCII-именами и меньшим размером.

    Пары собираются тем же правилом, что и записи на экране «Записи» (имя + время
    импорта): наполнение тестового датасета должно использовать реальную пару, иначе
    задача `dataset-fill` не отработает сценарий, как его видит заказчик.
    """
    overview = build_records(files)
    candidates = [
        record
        for record in overview.records
        if record.has_markup and record.markup is not None and not record.excluded
    ]
    candidates.sort(key=lambda record: record.size_bytes)
    ascii_first = [record for record in candidates if _ascii_pair(record)]
    ordered = [*ascii_first, *(record for record in candidates if record not in ascii_first)]
    return [(record.raw, record.markup) for record in ordered[:count]]  # type: ignore[misc]


def _ascii_pair(record: Any) -> bool:
    """True, если имена RAW и markup — ASCII (без известного дефекта кодировки)."""
    names = [str(record.raw.file_name)]
    if record.markup is not None:
        names.append(str(record.markup.file_name))
    return all(name.isascii() for name in names)


def _dataset_label(item: DatasetResponse) -> str:
    """Краткая подпись датасета для вердиктов и доказательств."""
    return f"{item.name} ({item.id})"


# ---------------------------------------------------------------------------
# TC-DS-01: создание датасета
# ---------------------------------------------------------------------------
def _create_dataset(context: AutomationContext) -> CheckOutcome:
    """TC-DS-01: `POST /api/datasets/` — 201, `type = direct_fill`, `id` и `creation_date`.

    Датасет удаляется сразу после проверки: результат фиксируется в доказательствах,
    а на стенде не остаётся `__TEST__`-мусора (NFR-T4).
    """
    created = _create(context, check_id="TC-DS-01", prefix="create")
    evidence: dict[str, Any] = {
        "name": created.name,
        "dataset_id": str(created.id),
        "type": str(created.type),
        "description": created.description,
        "creation_date": created.creation_date.isoformat() if created.creation_date else None,
        "fields": list(DATASET_FIELDS),
        "updated_at_field": "updated_at" in DATASET_FIELDS,
    }
    problems: list[str] = []
    if str(created.type) != DATASET_TYPE:
        problems.append(f"type={created.type}, ожидалось {DATASET_TYPE}")
    if not created.creation_date:
        problems.append("в ответе нет `creation_date`")
    if not str(created.name).startswith(TEST_PREFIX):
        problems.append("имя созданного датасета не начинается с `__TEST__`")

    try:
        fetched = _api(context).get_dataset(created.id)
    except (ApiError, ClientError) as exc:
        problems.append(f"GET /api/datasets/{{dataset_id}} не подтвердил создание: {exc}")
    else:
        evidence["fetched"] = {
            "name": fetched.name,
            "type": str(fetched.type),
            "creation_date": fetched.creation_date.isoformat() if fetched.creation_date else None,
        }
        if str(fetched.name) != str(created.name):
            problems.append(f"имя в GET (`{fetched.name}`) не совпало с ответом создания")
        if str(fetched.id) != str(created.id):
            problems.append("GET вернул другой `id` датасета")

    evidence["cleanup"] = _delete(context, created.id, check_id="TC-DS-01")
    if "удалён" not in str(evidence["cleanup"]):
        problems.append(f"самоочистка не выполнена: {evidence['cleanup']}")
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    verdict = (
        f"датасет создан и подтверждён: {created.name}, type={created.type}, "
        f"creation_date={evidence['creation_date']}; удалён после проверки"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


# ---------------------------------------------------------------------------
# TC-DS-02: реестр и фильтры
# ---------------------------------------------------------------------------
def _filter_rows(
    api: Any,
    evidence: dict[str, Any],
    problems: list[str],
    label: str,
    **kwargs: Any,
) -> list[DatasetResponse] | None:
    """Выполняет один фильтр реестра и складывает результат в доказательства."""
    try:
        rows = list(api.list_datasets(**kwargs).datasets)
    except (ApiError, ClientError) as exc:
        problems.append(f"фильтр `{label}` не отвечает: {exc}")
        evidence["filters"][label] = {
            "error": str(exc),
            "requested": {key: str(value) for key, value in kwargs.items()},
        }
        return None
    evidence["filters"][label] = {
        "requested": {key: str(value) for key, value in kwargs.items()},
        "returned": len(rows),
        "names": [item.name for item in rows[:5]],
    }
    return rows


def _list_filters(context: AutomationContext) -> CheckOutcome:
    """TC-DS-02: `datasets` + `count`, фильтры `name`/`description`/`type`/`creation_date`."""
    api = _api(context)
    try:
        response = api.list_datasets()
    except (ApiError, ClientError) as exc:
        raise PreconditionError(f"реестр датасетов не получен: {exc}") from exc
    items = list(response.datasets)
    if not items:
        raise PreconditionError("реестр датасетов пуст: фильтры проверить нельзя")

    sample = _pick(context, items)
    evidence: dict[str, Any] = {
        "count": response.count,
        "returned": len(items),
        "fields": list(DATASET_FIELDS),
        "sample": _dataset_label(sample),
        "filters": {},
    }
    problems: list[str] = []
    if int(response.count) != len(items):
        problems.append(
            f"count={response.count} не совпал с числом элементов datasets={len(items)}"
        )

    by_name = _filter_rows(api, evidence, problems, "name", name=sample.name)
    if by_name is not None:
        if not by_name:
            problems.append(f"фильтр `name={sample.name}` не вернул сам датасет")
        wrong = [item.name for item in by_name if str(item.name) != str(sample.name)]
        if wrong:
            problems.append(f"фильтр `name` вернул другие датасеты: {wrong[:3]}")

    by_type = _filter_rows(api, evidence, problems, "type", dataset_type=DATASET_TYPE)
    if by_type is not None:
        if not by_type:
            problems.append(f"фильтр `type={DATASET_TYPE}` не вернул ни одного датасета")
        wrong_types = sorted({str(item.type) for item in by_type if str(item.type) != DATASET_TYPE})
        if wrong_types:
            problems.append(f"фильтр `type` вернул другие типы: {wrong_types}")

    date_value = sample.creation_date.date().isoformat() if sample.creation_date else ""
    if date_value:
        by_date = _filter_rows(api, evidence, problems, "creation_date", creation_date=date_value)
        if by_date is not None:
            if not by_date:
                problems.append(f"фильтр `creation_date={date_value}` не вернул датасет выборки")
            wrong_dates = sorted(
                {
                    item.creation_date.date().isoformat()
                    for item in by_date
                    if item.creation_date and item.creation_date.date().isoformat() != date_value
                }
            )
            if wrong_dates:
                problems.append(f"фильтр `creation_date` вернул другие даты: {wrong_dates}")
    else:
        evidence["filters"]["creation_date"] = "у датасета выборки нет `creation_date`"

    described = next((item for item in items if str(item.description or "").strip()), None)
    if described is not None:
        description = str(described.description or "")
        by_description = _filter_rows(
            api, evidence, problems, "description", description=description
        )
        if by_description is not None:
            if not by_description:
                problems.append("фильтр `description` не вернул датасет с этим описанием")
            wrong_descriptions = [
                item.name for item in by_description if str(item.description or "") != description
            ]
            if wrong_descriptions:
                problems.append(
                    f"фильтр `description` вернул другие описания: {wrong_descriptions[:3]}"
                )
    else:
        evidence["filters"]["description"] = "нет датасетов с описанием — фильтр не проверялся"

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    facts = ", ".join(
        f"{label} → {value.get('returned')}"
        for label, value in evidence["filters"].items()
        if isinstance(value, dict) and "returned" in value
    )
    verdict = f"реестр: {len(items)} датасетов (count={response.count}); фильтры: {facts}"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


# ---------------------------------------------------------------------------
# TC-DS-03: метаданные и состав датасета
# ---------------------------------------------------------------------------
def _meta(context: AutomationContext) -> CheckOutcome:
    """TC-DS-03: метаданные датасета; состав — серверный, иначе локальный учёт (P1).

    Проверка **capability-aware**: провайдер состава сам находит серверную операцию в
    реестре. Пока её нет, факт «состав и агрегаты отсутствуют» фиксируется как
    результат с замечанием P1; когда операция появится, проверка сверит серверный
    состав с фактом наполнения пультом и потребует их совпадения.
    """
    items = _datasets(context)
    sample = _pick(context, items)
    try:
        meta = _api(context).get_dataset(sample.id)
    except (ApiError, ClientError) as exc:
        raise PreconditionError(f"метаданные датасета {sample.id} не получены: {exc}") from exc

    files = _registry_files(context)
    composition = read_composition(
        context.session,
        context.apis,
        meta.id,
        mode=_composition_mode(context),
        name=meta.name,
        dataset_type=str(meta.type),
        files=files,
    )
    aggregates = [
        name for name in ("records", "chunks", "files", "files_count") if name in DATASET_FIELDS
    ]
    evidence: dict[str, Any] = {
        "dataset_id": str(meta.id),
        "name": meta.name,
        "type": str(meta.type),
        "description": meta.description,
        "creation_date": meta.creation_date.isoformat() if meta.creation_date else None,
        "fields": list(DATASET_FIELDS),
        "server_operation": server_summary_key() or None,
        "server_composition_expected": server_available(),
        "composition_source": composition.source,
        "composition_source_label": composition.source_label,
        "composition_files": composition.files_count,
        "composition_raw": len(composition.raw_ids),
        "composition_markup": len(composition.markup_ids),
        "composition_counts": dict(composition.counts),
        "composition_confidence": composition.confidence,
        "aggregates_in_metadata": aggregates,
        "registry_files": len(files),
    }
    evidence["note"] = (
        "состав получен от сервера (операция состава есть в реестре)"
        if composition.source == SOURCE_SERVER
        else "состав датасета и агрегаты (записи/чанки) в API отсутствуют (P1) → замечание к API"
    )

    if composition.source == SOURCE_SERVER:
        record_composition(context.session, composition)
        local = composition_of(context.session, meta.id, source=SOURCE_LOCAL)
        if local is not None:
            diff = compare(composition, local)
            evidence["comparison"] = diff
            if not diff["match"]:
                return CheckOutcome(
                    CheckStatus.FAILED,
                    "состав сервера не совпал с отправленным пультом: "
                    f"RAW {diff['raw']['only_server']} / {diff['raw']['only_local']}, "
                    f"markup {diff['markup']['only_server']} / {diff['markup']['only_local']}",
                    evidence,
                )
            verdict = (
                f"состав получен от сервера и совпал с фактом пульта: "
                f"{composition.files_count} файлов"
            )
        else:
            verdict = (
                f"состав получен от сервера: {composition.files_count} файлов "
                f"(RAW {len(composition.raw_ids)}, markup {len(composition.markup_ids)})"
            )
        if composition.counts_label:
            verdict += f"; агрегаты: {composition.counts_label}"
        return CheckOutcome(CheckStatus.PASSED, verdict, evidence)

    if composition.source == SOURCE_LOCAL:
        if composition_of(context.session, meta.id, source=SOURCE_LOCAL) is None:
            # состав найден в sidecar-файле: копируем в сессию, чтобы отчёт был самодостаточен
            record_composition(context.session, composition)
        verdict = (
            f"метаданные получены: {meta.name}; состав — локальный факт пульта "
            f"({composition.files_count} файлов); состава и агрегатов в API нет (P1) → "
            "замечание к API"
        )
    elif composition.source == SOURCE_HEURISTIC:
        record_composition(context.session, composition)
        verdict = (
            f"метаданные получены: {meta.name}; состав предполагается по реестру файлов "
            f"({composition.files_count} файлов, {composition.confidence}); состава и "
            "агрегатов в API нет (P1) → замечание к API"
        )
    else:
        verdict = (
            f"метаданные получены: {meta.name}; состав и агрегаты в API отсутствуют (P1), "
            "локальной записи о наполнении нет → замечание к API"
        )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


# ---------------------------------------------------------------------------
# TC-DS-04: правка датасета
# ---------------------------------------------------------------------------
def _update(context: AutomationContext) -> CheckOutcome:
    """TC-DS-04: `PUT /api/datasets/{id}` — правка `name`/`description` видна в `GET`.

    Правка выполняется на своём `__TEST__`-датасете (создан и удалён внутри проверки),
    а отсутствие `updated_at` в схеме фиксируется как замечание к API.
    """
    created = _create(context, check_id="TC-DS-04", prefix="update")
    new_name = f"{created.name}-upd"
    new_description = "правка проверкой TC-DS-04"
    evidence: dict[str, Any] = {
        "dataset_id": str(created.id),
        "before": {"name": created.name, "description": created.description},
        "requested": {"name": new_name, "description": new_description},
        "fields": list(DATASET_FIELDS),
        "updated_at_field": "updated_at" in DATASET_FIELDS,
    }
    problems: list[str] = []
    try:
        updated = _api(context).update_dataset(
            created.id, name=new_name, description=new_description
        )
    except (ApiError, ClientError) as exc:
        problems.append(f"PUT /api/datasets/{{dataset_id}} не выполнен: {exc}")
    else:
        evidence["updated"] = {"name": updated.name, "description": updated.description}
        if str(updated.name) != new_name:
            problems.append(f"ответ PUT вернул имя `{updated.name}`, ожидалось `{new_name}`")
        if str(updated.description or "") != new_description:
            problems.append("ответ PUT вернул другое описание")

    try:
        fetched = _api(context).get_dataset(created.id)
    except (ApiError, ClientError) as exc:
        problems.append(f"GET после правки не выполнен: {exc}")
    else:
        evidence["after"] = {"name": fetched.name, "description": fetched.description}
        if str(fetched.name) != new_name:
            problems.append(f"GET вернул старое имя `{fetched.name}`: правка не сохранена")
        if str(fetched.description or "") != new_description:
            problems.append("GET вернул старое описание: правка не сохранена")

    evidence["cleanup"] = _delete(context, created.id, check_id="TC-DS-04")
    if "удалён" not in str(evidence["cleanup"]):
        problems.append(f"самоочистка не выполнена: {evidence['cleanup']}")
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    verdict = f"правка применена и подтверждена GET: `{new_name}`; "
    verdict += (
        "`updated_at` присутствует в схеме"
        if evidence["updated_at_field"]
        else "`updated_at` в схеме отсутствует (замечание к API)"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


# ---------------------------------------------------------------------------
# TC-DS-05: наполнение датасета (тяжёлая проверка)
# ---------------------------------------------------------------------------
def _newest_task(tasks: list[Any], since: str) -> Any | None:
    """Самая свежая задача наполнения, созданная не раньше подачи запроса.

    `fill` не возвращает `task_id` (P1), поэтому задача ищется в списке задач: берётся
    новейшая задача типа `dataset-fill`, созданная после подачи запроса (допуск 60 с —
    расхождение часов пульта и сервера).
    """
    try:
        moment = datetime.fromisoformat(since) - timedelta(seconds=60)
    except ValueError:
        moment = datetime.min
    candidates = [
        task
        for task in tasks
        if task.created_at is not None and task.created_at.replace(tzinfo=None) >= moment
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda task: task.created_at)
    return candidates[-1]


def _fill(context: AutomationContext) -> CheckOutcome:
    """TC-DS-05: `fill` → 202 без `task_id` → задача `dataset-fill` найдена и отслежена.

    Наполняется **только собственный** `__TEST__`-датасет: состав датасета сервер не
    отдаёт (P1), а удалить файлы из датасета нельзя — наполнение боевого датасета было
    бы необратимым изменением стенда. Состав фиксируется до вызова API, задача ищется
    в `GET /api/tasks/` (обходной путь монитора T4) и доводится до терминального статуса.
    """
    monitor = context.require_monitor()
    files = _registry_files(context)
    if not files:
        raise PreconditionError("реестр файлов не получен: наполнять нечем")
    pairs = _pair_candidates(files, _int_param(context, "fill_pairs", 1))
    if not pairs:
        raise PreconditionError("в реестре нет пар RAW+markup: наполнение проверить нельзя")

    wait = _float_param(context, "task_wait", FILL_TASK_WAIT)
    timeout = _float_param(context, "task_timeout", FILL_TASK_TIMEOUT)
    dataset = _create(context, check_id="TC-DS-05", prefix="fill")
    raw_ids = [str(raw.id) for raw, _markup in pairs]
    markup_ids = [str(markup.id) for _raw, markup in pairs]

    composition = record_fill(
        context.session,
        dataset_id=dataset.id,
        raw_file_ids=raw_ids,
        markup_file_ids=markup_ids,
        name=dataset.name,
        dataset_type=str(dataset.type),
        check_id="TC-DS-05",
        note="состав наполнения зафиксирован до вызова POST /api/datasets/fill/{dataset_id}",
        files=files,
    )
    evidence: dict[str, Any] = {
        "dataset_id": str(dataset.id),
        "dataset_name": dataset.name,
        "pairs": len(pairs),
        "raw_file_ids": raw_ids,
        "markup_file_ids": markup_ids,
        "file_names": [raw.file_name for raw, _markup in pairs]
        + [markup.file_name for _raw, markup in pairs],
        "composition_source": composition.source,
        "composition_files": composition.files_count,
        "task_type": FILL_TASK_TYPE,
        "task_wait": wait,
        "task_timeout": timeout,
        "found": False,
        "task_id": "",
        "status": "",
        "terminal": False,
        "note": "ответ `fill` не содержит `task_id` (P1): задача найдена в GET /api/tasks/",
    }

    try:
        body = _api(context).fill_dataset(
            dataset.id, raw_file_ids=raw_ids, markup_file_ids=markup_ids
        )
    except ApiError as exc:
        evidence["cleanup"] = _delete(context, dataset.id, check_id="TC-DS-05")
        return CheckOutcome(
            CheckStatus.FAILED,
            f"наполнение не принято: HTTP {exc.status_code}: {exc}",
            evidence,
        )
    except ClientError as exc:
        evidence["cleanup"] = _delete(context, dataset.id, check_id="TC-DS-05")
        return CheckOutcome(CheckStatus.FAILED, f"наполнение не принято: {exc}", evidence)
    evidence["response_body"] = body if isinstance(body, (dict, list, str)) else None

    started = time.monotonic()
    task = None
    recent: list[Any] = []
    attempts = 0
    while True:
        attempts += 1
        try:
            recent = monitor.find_recent(task_type=FILL_TASK_TYPE)
        except (ApiError, ClientError) as exc:
            evidence["task_lookup_error"] = str(exc)
            recent = []
        task = _newest_task(recent, composition.captured_at)
        if task is not None or wait <= 0 or (time.monotonic() - started) >= wait:
            break
        time.sleep(min(FILL_POLL_INTERVAL, max(0.0, wait)))
    evidence["lookup_attempts"] = attempts
    evidence["tasks_in_list"] = len(recent)

    if task is None:
        evidence["cleanup"] = _delete(context, dataset.id, check_id="TC-DS-05")
        return CheckOutcome(
            CheckStatus.FAILED,
            f"наполнение принято, но задача `{FILL_TASK_TYPE}` не найдена в GET /api/tasks/ "
            f"за {wait:.0f} с: обходной путь (P1) не работает",
            evidence,
        )

    task_id = str(task.id)
    evidence["task_id"] = task_id
    evidence["found"] = True
    evidence["task_created_at"] = task.created_at.isoformat() if task.created_at else None
    monitor.register(
        context.session,
        task_id=task_id,
        task_type=str(task.type),
        name=str(task.name),
        check_id="TC-DS-05",
        note="задача наполнения найдена обходным поиском: `fill` не возвращает `task_id` (P1)",
    )

    poll = poll_until(
        lambda: monitor.poll_once(context.session, task_id, label="TC-DS-05"),
        lambda item: bool(item.terminal or item.unknown or item.error),
        interval=FILL_POLL_INTERVAL,
        timeout=timeout,
    )
    evidence["status"] = poll.status
    evidence["terminal"] = poll.terminal
    evidence["poll"] = poll.as_dict()
    if poll.task is not None:
        evidence["task_card"] = monitor.card_snapshot(poll.task)

    composition = replace(composition, task_id=task_id, task_status=str(poll.status))
    record_composition(context.session, composition)

    evidence["cleanup"] = _delete(context, dataset.id, check_id="TC-DS-05")
    problems: list[str] = []
    if "удалён" not in str(evidence["cleanup"]):
        problems.append(f"самоочистка не выполнена: {evidence['cleanup']}")
    if poll.error:
        problems.append(f"опрос задачи не выполнен: {poll.error}")
    if poll.unknown:
        problems.append("сервер сообщает, что задача не найдена (BR-R7)")
    if not poll.terminal:
        problems.append(
            f"задача не дошла до терминального статуса за {timeout:.0f} с (статус: {poll.status})"
        )
    elif str(poll.status) != str(TaskStatus.COMPLETED):
        problems.append(f"задача завершилась со статусом `{poll.status}` (ожидался completed)")
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)

    verdict = (
        f"датасет {dataset.name} наполнен: {len(pairs)} пар RAW+markup; 202 без `task_id` (P1); "
        f"задача {task_id} найдена обходным поиском и отслежена до статуса `{poll.status}`; "
        "состав зафиксирован локально, датасет удалён"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


# ---------------------------------------------------------------------------
# TC-DS-06: контракт `fill` на несуществующих идентификаторах
# ---------------------------------------------------------------------------
def _fill_contract(context: AutomationContext) -> CheckOutcome:
    """TC-DS-06: валидация `fill` — понятная ошибка, без 5xx и без запуска задачи.

    Пробы идут на **несуществующий** `dataset_id`, поэтому боевой датасет в проверке не
    участвует и наполнение не запускается: это дешёвая предпроверка тяжёлой операции.
    """
    files = _registry_files(context)
    if not files:
        raise PreconditionError("реестр файлов не получен: тело пробы нечем заполнить")
    missing_id = str(uuid4())
    file_id = str(files[0].id)
    probes: dict[str, dict[str, Any]] = {
        "unknown_dataset": {"raw_file_ids": [file_id]},
        "missing_raw_file_ids": {"markup_file_ids": [file_id]},
        "empty_raw_file_ids": {"raw_file_ids": []},
    }
    evidence: dict[str, Any] = {"dataset_id": missing_id, "probes": {}, "error_format": {}}
    problems: list[str] = []
    for label, body in probes.items():
        probe = context.probe_request("POST", f"/api/datasets/fill/{missing_id}", json_body=body)
        status = probe.status
        evidence["probes"][label] = {
            "body": body,
            "status": status,
            "detail": probe.body_text[:200],
            "error": probe.error or None,
        }
        body_json = probe.body_json if isinstance(probe.body_json, dict) else {}
        detail = body_json.get("detail")
        evidence["error_format"][label] = (
            "HTTPValidationError.detail[]"
            if isinstance(detail, list)
            else ("detail: строка" if "detail" in body_json else "иное")
        )
        if status is None:
            problems.append(f"проба `{label}` не отвечает: {probe.error}")
            continue
        code = int(status)
        if code >= 500:
            problems.append(f"проба `{label}` отвечает {code} (5xx): {probe.body_text[:120]}")
        elif 200 <= code <= 299:
            problems.append(
                f"проба `{label}` принята ({code}): для несуществующего датасета ожидалась ошибка"
            )
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    facts = ", ".join(f"{label} → {value['status']}" for label, value in evidence["probes"].items())
    return CheckOutcome(
        CheckStatus.PASSED,
        f"валидация `fill` на несуществующем датасете отвечает ошибкой: {facts}",
        evidence,
    )


# ---------------------------------------------------------------------------
# TC-DS-07: удаление датасета и самоочистка
# ---------------------------------------------------------------------------
def _delete_dataset(context: AutomationContext) -> CheckOutcome:
    """TC-DS-07: `DELETE /api/datasets/{id}` → отсутствие подтверждено `GET` и реестром."""
    api = _api(context)
    try:
        before = list(api.list_datasets().datasets)
    except (ApiError, ClientError) as exc:
        raise PreconditionError(f"реестр датасетов не получен: {exc}") from exc

    created = _create(context, check_id="TC-DS-07", prefix="delete")
    evidence: dict[str, Any] = {
        "dataset_id": str(created.id),
        "name": created.name,
        "count_before": len(before),
        "fields": list(DATASET_FIELDS),
    }
    problems: list[str] = []

    try:
        registered = [
            item for item in api.list_datasets().datasets if str(item.id) == str(created.id)
        ]
    except (ApiError, ClientError) as exc:
        problems.append(f"реестр после создания не отвечает: {exc}")
    else:
        evidence["visible_in_registry"] = bool(registered)
        if not registered:
            problems.append("созданный датасет не виден в `GET /api/datasets/`")

    evidence["delete_result"] = _delete(context, created.id, check_id="TC-DS-07")
    if "удалён" not in str(evidence["delete_result"]):
        problems.append(f"удаление не выполнено: {evidence['delete_result']}")

    try:
        api.get_dataset(created.id)
    except ApiError as exc:
        evidence["get_after_delete"] = f"HTTP {exc.status_code}"
        if int(exc.status_code or 0) not in (400, 404):
            problems.append(f"после удаления `GET` отвечает {exc.status_code} (ожидалось 404)")
    except ClientError as exc:
        evidence["get_after_delete"] = f"ошибка: {exc}"
        problems.append(f"после удаления `GET` не отвечает: {exc}")
    else:
        evidence["get_after_delete"] = "HTTP 200: датасет всё ещё доступен"
        problems.append("после удаления `GET /api/datasets/{dataset_id}` отдаёт датасет")

    try:
        after = [str(item.id) for item in api.list_datasets().datasets]
    except (ApiError, ClientError) as exc:
        problems.append(f"реестр после удаления не отвечает: {exc}")
    else:
        evidence["count_after"] = len(after)
        evidence["absent_in_registry"] = str(created.id) not in after
        if str(created.id) in after:
            problems.append("удалённый датасет остался в реестре")
        if len(after) != len(before):
            problems.append(f"размер реестра не вернулся к исходному: {len(before)} → {len(after)}")

    try:
        api.delete_dataset(created.id)
    except ApiError as exc:
        evidence["repeat_delete"] = f"HTTP {exc.status_code}"
    except ClientError as exc:
        evidence["repeat_delete"] = f"ошибка: {exc}"
    else:
        evidence["repeat_delete"] = "принято повторно (вариант зафиксирован)"

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    verdict = (
        f"датасет {created.name} удалён; отсутствие подтверждено `GET` "
        f"({evidence['get_after_delete']}) и реестром "
        f"({evidence['count_before']} → {evidence.get('count_after')}); "
        f"повторное удаление: {evidence['repeat_delete']}"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


#: Сценарии проверок модуля «Datasets»: ключ — `CheckSpec.automation`.
AUTOMATIONS = {
    "datasets.create": _create_dataset,
    "datasets.list": _list_filters,
    "datasets.meta": _meta,
    "datasets.update": _update,
    "datasets.fill": _fill,
    "datasets.fill_contract": _fill_contract,
    "datasets.delete": _delete_dataset,
}
