"""Автоматические сценарии проверок обучения (TC-TR, этап T7).

Основная часть группы — ресурсоёмкие проверки: запуск обучения (`TC-TR-02`),
управление им (`TC-TR-03`), результат (`TC-TR-04`) и проверка модели (`TC-TR-05`).
Они выполняются в фоне с подтверждением оператора (карточка запуска, `FR-P-19`).

Самая дешёвая проверка группы — `TC-TR-01`: **контракт обучения без расхода
ресурсов**. Она проверяет валидацию тела `train` и не запускает обучение. Чтобы
обучение не началось даже при дефекте сервера (например, диапазоны «прощают»),
все пробы идут с **несуществующим `dataset_id`**: датасет не найден — обучение
невозможно, ресурсы стенда не расходуются. Если сервер всё же принял запрос (2xx),
сценарий фиксирует отказ и сохраняет `task_id`, чтобы оператор мог прервать задачу
(`POST /api/tasks/{task_id}`): молча оставить её нельзя.

Границы взяты из схемы `ModelTrainingRequest` (`client/schemas.py`): `epochs` 1…1000,
`batch_size` 1…256, `learning_rate` (0…1], `augmentation_noise_percentage` 0.001…0.05,
`augmentation_num_samples` 1…10.
"""

from __future__ import annotations

import hashlib
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
from acceptance.exchange import ExchangeResult
from acceptance.ml_samples import model_label, pick_model
from client.errors import ApiError, ClientError, ServerUnavailableError
from client.schemas import ModelMetadata

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: Датасет, которого нет на стенде: используется во всех пробах `TC-TR-01`.
UNKNOWN_DATASET = "00000000-0000-4000-8000-000000000000"

#: Валидная основа тела `train` (кроме поля, которое проверяет проба).
BASE_TRAIN_BODY: dict[str, Any] = {
    "batch_size": 32,
    "epochs": 1,
    "learning_rate": 1e-06,
    "preprocessing_converter": "direct",
}

#: Пробы границ: (поле, подпись значения, значение).
RANGE_CASES: tuple[tuple[str, str, Any], ...] = (
    ("epochs", "0", 0),
    ("epochs", "1001", 1001),
    ("batch_size", "0", 0),
    ("batch_size", "257", 257),
    ("learning_rate", "0", 0),
    ("learning_rate", "1.1", 1.1),
    ("augmentation_noise_percentage", "0.0001", 0.0001),
    ("augmentation_noise_percentage", "0.06", 0.06),
    ("augmentation_num_samples", "0", 0),
    ("augmentation_num_samples", "11", 11),
)


def _detail_text(result: ExchangeResult) -> str:
    """Текст ошибки сервера: строка `detail` или первая ошибка валидации."""
    body = result.body_json
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        return str(detail[0])
    return (result.body_text or result.error or "")[:300]


def _model(context: AutomationContext) -> ModelMetadata:
    """Модель для проб контракта обучения (обучение не запускается — подойдёт любая).

    Raises:
        PreconditionError: реестр моделей пуст или недоступен.
    """
    try:
        rows = list(context.models_api().list_models())
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр моделей не получен: {exc}") from exc
    chosen = pick_model(rows, model_id=context.param("model_id"))
    if chosen is None:
        raise PreconditionError("нет модели для проб обучения: выберите `model_id`")
    return chosen


def _trained_model(context: AutomationContext) -> ModelMetadata:
    """Обученная модель, если она есть; иначе — первая в реестре (для экспорта ONNX)."""
    try:
        rows = list(context.models_api().list_models())
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр моделей не получен: {exc}") from exc
    chosen = pick_model(rows, model_id=context.param("model_id"), with_metrics=True) or pick_model(
        rows, model_id=context.param("model_id")
    )
    if chosen is None:
        raise PreconditionError("нет модели для экспорта ONNX: выберите `model_id`")
    return chosen


def _onnx_export(context: AutomationContext) -> CheckOutcome:
    """TC-TR-06: экспорт ONNX — имя, размер, содержимое и заголовки ответа (UC-23).

    Файл скачивается дважды: клиентом API (нужно содержимое и его контрольная сумма)
    и «сырой» пробой — нужны заголовки ответа (`Content-Length`, `Accept-Ranges`),
    которые клиент не сохраняет. Отсутствие `Range`/`Accept-Ranges` — примечание
    (замечание к потоковой отдаче файлов, как в `TC-FILE-09`), а пустой файл или
    ответ с кодом ошибки — отказ.
    """
    model = _trained_model(context)
    evidence: dict[str, Any] = {"model_id": str(model.id), "model_label": model_label(model)}
    problems: list[str] = []

    try:
        payload = context.models_api().download_onnx(model.id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(CheckStatus.FAILED, f"экспорт ONNX не выполнен: {exc}", evidence)

    evidence.update(
        {
            "file_name": payload.file_name or "",
            "media_type": payload.media_type or "",
            "size": payload.size,
            "sha256": hashlib.sha256(payload.content).hexdigest(),
            "onnx_marker": b"onnx" in payload.content[:4096],
        }
    )
    if payload.size == 0:
        problems.append("сервер вернул пустой файл ONNX")
    if payload.file_name and not payload.file_name.lower().endswith(".onnx"):
        problems.append(f"имя файла не ONNX: {payload.file_name}")
    if not payload.file_name:
        problems.append("сервер не передал имя файла в `Content-Disposition`")

    headers_result = context.probe_request(
        "GET", f"/api/ml_models/models/{model.id}/download_onnx", binary=True
    )
    headers = {str(key).lower(): value for key, value in (headers_result.headers or {}).items()}
    evidence["status"] = headers_result.status
    evidence["content_length"] = headers.get("content-length")
    evidence["accept_ranges"] = headers.get("accept-ranges")
    notes: list[str] = []
    if headers_result.status is None:
        problems.append("проба заголовков не отправлена")
    elif not 200 <= int(headers_result.status) < 300:
        problems.append(f"повторное скачивание ONNX ответило {headers_result.status}")
    else:
        if "content-length" not in headers:
            notes.append("в ответе нет `Content-Length`: размер узнаётся только по факту")
        if "accept-ranges" not in headers:
            notes.append(
                "сервер не поддерживает `Range`/`Accept-Ranges`: файл отдаётся целиком "
                "(замечание, как в TC-FILE-09)"
            )
    if not evidence["onnx_marker"]:
        notes.append("в начале файла нет строки `onnx`: формат стоит проверить вручную")

    if problems:
        return CheckOutcome(
            CheckStatus.FAILED, "; ".join(problems), evidence, note="; ".join(notes)
        )
    verdict = (
        f"ONNX получен: {evidence['file_name'] or evidence['media_type'] or 'файл'} · "
        f"{payload.size} Б (sha256 {evidence['sha256'][:12]}…)"
    )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence, note="; ".join(notes))


def _contract(context: AutomationContext) -> CheckOutcome:
    """TC-TR-01: несуществующий датасет и выход за диапазоны — ошибка, обучения нет.

    Пробы идут «сырым» клиентом: нужен код ответа сервера, а не исключение клиента
    (схема `ModelTrainingRequest` не дала бы отправить заведомо неверное тело).
    """
    model = _model(context)
    path = f"/api/ml_models/models/{model.id}/train"
    cases: list[tuple[str, dict[str, Any]]] = [
        ("несуществующий dataset_id", {**BASE_TRAIN_BODY, "dataset_id": UNKNOWN_DATASET})
    ]
    cases.extend(
        (
            f"{field}={label}",
            {**BASE_TRAIN_BODY, "dataset_id": UNKNOWN_DATASET, field: value},
        )
        for field, label, value in RANGE_CASES
    )

    evidence: dict[str, Any] = {
        "model_id": str(model.id),
        "model_label": model_label(model),
        "dataset_id": UNKNOWN_DATASET,
        "range_cases": [f"{field}={label}" for field, label, _ in RANGE_CASES],
        "cases": [],
    }
    problems: list[str] = []
    started: list[str] = []

    for case, body in cases:
        result = context.probe_request("POST", path, json_body=body)
        row: dict[str, Any] = {
            "case": case,
            "status": result.status,
            "detail": _detail_text(result),
        }
        if result.status is None:
            problems.append(f"{case}: запрос не отправлен ({result.error})")
        elif 200 <= int(result.status) < 300:
            body_json = result.body_json if isinstance(result.body_json, dict) else {}
            task_id = str(body_json.get("task_id") or "")
            row["task_id"] = task_id
            started.append(task_id or case)
            problems.append(
                f"{case}: принято сервером ({result.status}) — ожидалась ошибка валидации"
            )
        elif int(result.status) >= 500:
            problems.append(f"{case}: серверная ошибка {result.status}")
        evidence["cases"].append(row)

    note = ""
    if started:
        note = (
            "обучение всё же стартовало: задачи "
            + ", ".join(task or "без task_id" for task in started)
            + " — прервите их на экране «Задачи»"
        )
        evidence["started_tasks"] = started
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence, note=note)
    codes = sorted({str(row["status"]) for row in evidence["cases"]})
    return CheckOutcome(
        CheckStatus.PASSED,
        f"контракт обучения отклоняет неверные тела: {len(cases)} проб, коды "
        f"{', '.join(codes)}; задача обучения не создана",
        evidence,
    )


#: Реестр сценариев модуля (ключ — `CheckSpec.automation`).
AUTOMATIONS: dict[str, Any] = {
    "training.contract": _contract,
    "training.onnx_export": _onnx_export,
}
