"""Автоматические сценарии проверок инференса (TC-INF, этап T8).

Проверки работают на живых данных: модель берётся из реестра (`ml_samples.pick_model`
с требованием обученной модели), а сэмпл — из строки RAW-файла
(`ml_samples.parse_raw_sample`): `inference/single` принимает шесть Base64-сигналов и
параметры шкал, как в колонках raw_data CSV.

    * `TC-INF-03` — границы `threshold`: 0.0 и 1.0 допустимы, выход за диапазон даёт
      ошибку валидации. Проверка класса `tech`: одно предсказание дешевле обучения и
      ничего на стенде не создаёт;
    * `TC-INF-04` — повторяемость: один и тот же сэмпл предсказывается дважды.
      Различие состава классов — отказ, различие только `confidence` — примечание
      (замечание к API, как требует чек-лист `BR-R9`).

Скачивание RAW идёт файлом целиком (`Range` сервер не поддерживает), поэтому
выбирается самый маленький RAW с ASCII-именем: скачивание не-ASCII имён на стенде
падает (дефект P0). Если обученной модели или RAW-файла нет — проверка «пропущена»
(`PreconditionError`), оператор видит, что нужно подготовить на стенде.
"""

from __future__ import annotations

from typing import Any

from acceptance.checks import ml_tasks
from acceptance.checks.registry import CheckStatus
from acceptance.checks.runner import (
    AutomationContext,
    CheckOutcome,
    PreconditionError,
    automate,
    evaluate,
    scenario,
)
from acceptance.ml_samples import (
    RawSample,
    model_label,
    parse_raw_sample,
    pick_model,
    pick_raw_file,
)
from client.errors import ApiError, ClientError, ServerUnavailableError
from client.schemas import InferenceSingleResponse, ModelMetadata, TaskType

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: Порог по умолчанию для проверок (`ModelCheckRequest`, `InferenceStartRequest`).
DEFAULT_THRESHOLD = 0.5

#: Границы `threshold`: внутри диапазона — допустимо, снаружи — ошибка валидации.
ALLOWED_THRESHOLDS: tuple[float, ...] = (0.0, 1.0)

#: Значения вне диапазона (пробы `TC-INF-03`).
OUT_OF_RANGE_THRESHOLDS: tuple[float, ...] = (-0.1, 1.1)

#: Допуск сравнения `confidence` при проверке повторяемости (`TC-INF-04`).
CONFIDENCE_TOLERANCE = 1e-9


def _picked_model(context: AutomationContext) -> ModelMetadata:
    """Обученная модель для инференса.

    Raises:
        PreconditionError: подходящей модели нет — оператору нужно выбрать `model_id`.
    """
    try:
        rows = list(context.models_api().list_models())
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр моделей не получен: {exc}") from exc
    chosen = pick_model(rows, model_id=context.param("model_id"), with_metrics=True)
    if chosen is None:
        raise PreconditionError(
            "нет обученной модели (`metrics` пусты): выберите `model_id` на карточке проверки"
        )
    return chosen


def _read_sample(context: AutomationContext) -> RawSample:
    """Сэмпл для `inference/single` из строки RAW-файла стенда.

    Raises:
        PreconditionError: нет подходящего RAW-файла или строка не разбирается.
    """
    try:
        files = list(context.files_api().list_files().files)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр файлов не получен: {exc}") from exc

    chosen = pick_raw_file(
        files, file_id=context.param("sample_file_id") or context.param("file_id")
    )
    if chosen is None:
        raise PreconditionError(
            "в реестре нет RAW-файла с ASCII-именем: укажите `file_id` на карточке проверки"
        )
    try:
        payload = context.files_api().download(chosen.id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"RAW-файл {chosen.file_name} не скачан: {exc}") from exc
    try:
        return parse_raw_sample(
            payload.content.decode("utf-8", errors="replace"),
            row=int(context.param("row") or 1),
            file_name=chosen.file_name,
        )
    except ValueError as exc:
        raise PreconditionError(f"сэмпл из {chosen.file_name} не разобран: {exc}") from exc


def _classes_key(response: InferenceSingleResponse) -> list[tuple[int, tuple[str, ...], bool]]:
    """Структурный отпечаток ответа: классы без `confidence` (для сравнения прогонов)."""
    return [
        (int(item.signal_num), tuple(str(value) for value in item.device_ids), bool(item.detected))
        for item in response.classes
    ]


def _single(
    context: AutomationContext, model_id: str, sample: RawSample, threshold: float
) -> InferenceSingleResponse:
    """Одно предсказание клиентом API (в журнал обмена идёт с меткой проверки)."""
    return context.models_api().inference_single(
        model_id, sample=sample.request_fields(), threshold=threshold
    )


def _threshold(context: AutomationContext) -> float:
    """Порог из параметров проверки (по умолчанию 0.5, как в спецификации)."""
    raw = context.param("threshold")
    if not raw:
        return DEFAULT_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_THRESHOLD


def _threshold_bounds(context: AutomationContext) -> CheckOutcome:
    """TC-INF-03: границы `threshold` — 0.0 и 1.0 допустимы, выход за диапазон отвергнут.

    Значения внутри диапазона проверяются клиентом API (ожидается 200 и непустые
    `classes`), значения снаружи — «сырой» пробой: иначе схема клиента не дала бы
    отправить недопустимый порог, и поведение сервера осталось бы невыясненным.
    """
    model = _picked_model(context)
    sample = _read_sample(context)
    evidence: dict[str, Any] = {
        "model_id": str(model.id),
        "model_label": model_label(model),
        "sample": sample.as_evidence(),
        "allowed": [],
        "out_of_range": [],
    }
    problems: list[str] = []

    for threshold in ALLOWED_THRESHOLDS:
        try:
            response = _single(context, str(model.id), sample, threshold)
        except (ApiError, ClientError, ServerUnavailableError) as exc:
            problems.append(f"threshold={threshold}: вызов не выполнен ({exc})")
            evidence["allowed"].append({"threshold": threshold, "error": str(exc)})
            continue
        evidence["allowed"].append(
            {
                "threshold": threshold,
                "classes": len(response.classes),
                "signal_nums": [item.signal_num for item in response.classes],
            }
        )
        if not response.classes:
            problems.append(f"threshold={threshold}: сервер вернул пустой список классов")

    path = f"/api/ml_models/models/{model.id}/inference/single"
    for threshold in OUT_OF_RANGE_THRESHOLDS:
        result = context.probe_request(
            "POST", path, json_body={**sample.request_fields(), "threshold": threshold}
        )
        row: dict[str, Any] = {"threshold": threshold, "status": result.status}
        if result.status is None:
            problems.append(f"threshold={threshold}: проба не отправлена ({result.error})")
        elif 200 <= int(result.status) < 300:
            problems.append(
                f"threshold={threshold}: принят сервером ({result.status}) — ожидалась ошибка"
            )
        elif int(result.status) >= 500:
            problems.append(f"threshold={threshold}: серверная ошибка {result.status}")
        evidence["out_of_range"].append(row)

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    codes = ", ".join(str(row["status"]) for row in evidence["out_of_range"])
    return CheckOutcome(
        CheckStatus.PASSED,
        "границы `threshold` подтверждены: 0.0 и 1.0 дают предсказание, значения вне "
        f"диапазона отклонены ({codes})",
        evidence,
    )


def _repeatability(context: AutomationContext) -> CheckOutcome:
    """TC-INF-04: два одинаковых вызова `inference/single` дают один и тот же результат.

    Состав классов (`signal_num`, `device_ids`, `detected`) должен совпасть точно:
    это признак детерминированного предсказания. Различие `confidence` — расхождение,
    которое чек-лист (`BR-R9`) требует фиксировать **замечанием**, а не отказом.
    """
    model = _picked_model(context)
    sample = _read_sample(context)
    threshold = _threshold(context)

    try:
        first = _single(context, str(model.id), sample, threshold)
        second = _single(context, str(model.id), sample, threshold)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"повторный инференс не выполнен: {exc}",
            {"model_id": str(model.id), "sample": sample.as_evidence()},
        )

    evidence: dict[str, Any] = {
        "model_id": str(model.id),
        "model_label": model_label(model),
        "sample": sample.as_evidence(),
        "threshold": threshold,
        "first": _classes_key(first),
        "second": _classes_key(second),
        "confidence_first": [float(item.confidence) for item in first.classes],
        "confidence_second": [float(item.confidence) for item in second.classes],
    }

    if _classes_key(first) != _classes_key(second):
        return CheckOutcome(
            CheckStatus.FAILED,
            "результат не повторяем: состав классов двух вызовов различается",
            evidence,
        )

    diffs = [
        abs(a - b)
        for a, b in zip(evidence["confidence_first"], evidence["confidence_second"], strict=True)
    ]
    evidence["confidence_diffs"] = diffs
    note = ""
    if any(diff > CONFIDENCE_TOLERANCE for diff in diffs):
        note = (
            "расхождение `confidence` между вызовами "
            f"(максимум {max(diffs):.6g}) — замечание к повторяемости инференса"
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"результат повторяем: {len(first.classes)} классов совпали в двух вызовах",
        evidence,
        note=note,
    )


def _inference_start(context: AutomationContext) -> CheckOutcome:
    """TC-INF-01: асинхронный инференс по датасету и наблюдение за задачей (UC-24, BR-F6).

    Проверка запускает инференс на выбранной модели и датасете, ставит задачу типа
    `inference` на наблюдение и ведёт её монитором до терминального состояния.
    Результат связывается с проверкой номером записи журнала — по нему в протоколе
    видно, какие обмены относятся к этой задаче. Сущностей проверка не создаёт.
    """
    model = ml_tasks.trained_model_for(context)
    dataset = ml_tasks.dataset_for(context)
    threshold = ml_tasks.param_float(context, "threshold", DEFAULT_THRESHOLD)
    evidence: dict[str, Any] = {
        "model": ml_tasks.model_snapshot(model),
        "dataset_id": str(dataset.id),
        "dataset_label": str(dataset.name),
        "threshold": threshold,
        "timeout": ml_tasks.timeout_of(context),
    }
    problems: list[str] = []
    notes: list[str] = []

    try:
        response = context.models_api().inference(
            model.id, dataset_id=str(dataset.id), threshold=threshold
        )
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(CheckStatus.FAILED, f"инференс не запущен: {exc}", evidence)

    evidence["response"] = {
        "task_id": str(response.task_id or ""),
        "status": str(response.status or ""),
        "message": str(response.message or ""),
        "model_id": str(response.model_id or ""),
        "dataset_id": str(response.dataset_id or ""),
    }
    try:
        task_id = ml_tasks.register_task(
            context, payload=response, task_type=TaskType.INFERENCE.value
        )
    except PreconditionError as exc:
        problems.append(str(exc))
    else:
        watch = ml_tasks.watch_task(context, task_id=task_id, task_type=TaskType.INFERENCE.value)
        evidence["watch"] = watch
        problems.extend(ml_tasks.observation_problems(watch))
        evidence["journal"] = {
            "from": watch["journal_from"],
            "to": watch["journal_seq"],
            "range": f"#{watch['journal_from']}…#{watch['journal_seq']}"
            if watch["journal_seq"]
            else "",
        }
        if str(watch["task_type"]) != TaskType.INFERENCE.value:
            notes.append(
                f"тип задачи на стенде — «{watch['task_type'] or 'не определён'}», "
                "ожидался `inference`"
            )
        if not watch["terminal"]:
            notes.append(
                "задача не завершилась: результат можно посмотреть в панели «Задачи» "
                f"(`task_id={task_id}`)"
            )

    verdict = (
        f"инференс запущен: задача {response.task_id} типа `inference`, финальный статус "
        f"«{evidence.get('watch', {}).get('final_label') or response.status}»; "
        f"журнал {evidence.get('journal', {}).get('range') or '—'}"
    )
    return ml_tasks.async_outcome(
        verdict=verdict, evidence=evidence, problems=problems, notes=notes
    )


def _single_check(context: AutomationContext) -> CheckOutcome:
    """TC-INF-02: синхронный инференс одного сэмпла — `classes[]` и пределы (UC-25, BR-R9).

    Сэмпл берётся из строки RAW-файла стенда (шесть Base64-сигналов и шкалы), модель —
    обученная. Проверяются состав `classes[]` (`signal_num`, `device_ids`,
    `confidence`, `detected`) и границы `confidence` — значение вне 0…1 означает, что
    ответ не соответствует ожиданию.
    """
    model = _picked_model(context)
    sample = _read_sample(context)
    threshold = _threshold(context)
    evidence: dict[str, Any] = {
        "model_id": str(model.id),
        "model_label": model_label(model),
        "sample": sample.as_evidence(),
        "threshold": threshold,
    }
    problems: list[str] = []

    try:
        response = _single(context, str(model.id), sample, threshold)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(CheckStatus.FAILED, f"инференс сэмпла не выполнен: {exc}", evidence)

    classes: list[dict[str, Any]] = [
        {
            "signal_num": int(item.signal_num),
            "device_ids": [str(value) for value in item.device_ids],
            "confidence": float(item.confidence),
            "detected": bool(item.detected),
        }
        for item in response.classes
    ]
    evidence["classes"] = classes
    evidence["model_id_in_response"] = str(response.model_id or "")
    evidence["threshold_in_response"] = response.threshold
    if not classes:
        problems.append("сервер вернул пустой список классов: предсказание не получено")
    for item in classes:
        confidence = float(item["confidence"])
        if not 0.0 <= confidence <= 1.0:
            problems.append(
                f"класс {item['signal_num']}: `confidence`={confidence} вне диапазона 0…1"
            )
        if item["detected"] and not item["device_ids"]:
            problems.append(
                f"класс {item['signal_num']}: обнаружена нагрузка, но `device_ids` пуст"
            )
    if str(response.model_id or "") and str(response.model_id) != str(model.id):
        problems.append(
            f"в ответе другая модель: {response.model_id} вместо запрошенной {model.id}"
        )

    verdict = (
        f"инференс сэмпла выполнен: классов {len(classes)}, порог {threshold}; "
        f"обнаружено нагрузок — "
        f"{sum(1 for item in classes if item['detected'])}"
    )
    return CheckOutcome(
        CheckStatus.PASSED if not problems else CheckStatus.FAILED,
        "; ".join(problems) or verdict,
        evidence,
    )


#: Реестр сценариев модуля (ключ — `CheckSpec.automation`).
AUTOMATIONS: dict[str, Any] = {
    "inference.single": _single_check,
    "inference.start": _inference_start,
    "inference.threshold_bounds": _threshold_bounds,
    "inference.repeatability": _repeatability,
}
