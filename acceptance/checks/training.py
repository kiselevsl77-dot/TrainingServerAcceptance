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
from acceptance.exchange import ExchangeResult
from acceptance.ml_samples import model_label, pick_model
from acceptance.session import add_test_entity
from client.errors import ApiError, ClientError, ServerUnavailableError
from client.schemas import ModelMetadata, TaskType

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

#: Тип учёта модели-результата обучения: до решения `TC-TR-04` она «незакрытая» сущность.
ENTITY_TRAINED = "модель (результат обучения)"

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


def _training_start(context: AutomationContext) -> CheckOutcome:
    """TC-TR-02: запуск обучения и наблюдение за задачей `training` (UC-21, BR-F5).

    Проверка **запускает реальное обучение** (`epochs` из карточки, по умолчанию 3),
    поэтому относится к классу `heavy`: её выполняет оператор с подтверждением расхода
    ресурсов. Модель-результат регистрируется в учёте сессии — судьбу артефакта решает
    `TC-TR-04`, а уборку, если модель осталась, доводит `TC-CLEAN-01`.
    """
    model = ml_tasks.trained_model_for(context)
    dataset = ml_tasks.dataset_for(context)
    epochs = ml_tasks.param_int(context, "epochs", ml_tasks.DEFAULT_EPOCHS)
    batch_size = ml_tasks.param_int(context, "batch_size", ml_tasks.DEFAULT_BATCH_SIZE)
    rate = ml_tasks.param_float(context, "learning_rate", ml_tasks.DEFAULT_LEARNING_RATE)
    name = str(context.param("trained_model_name") or ml_tasks.test_name(context, "_trained"))
    evidence: dict[str, Any] = {
        "base_model": ml_tasks.model_snapshot(model),
        "dataset_id": str(dataset.id),
        "dataset_label": str(dataset.name),
        "params": {
            "epochs": epochs,
            "batch_size": batch_size,
            "learning_rate": rate,
            "trained_model_name": name,
        },
        "timeout": ml_tasks.timeout_of(context),
    }
    problems: list[str] = []
    notes: list[str] = []

    try:
        response = context.models_api().train(
            model.id,
            dataset_id=str(dataset.id),
            epochs=epochs,
            batch_size=batch_size,
            learning_rate=rate,
            trained_model_name=name,
        )
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(CheckStatus.FAILED, f"запуск обучения не выполнен: {exc}", evidence)

    evidence["response"] = {
        "task_id": str(response.task_id or ""),
        "status": str(response.status or ""),
        "message": str(response.message or ""),
        "validation_info": response.validation_info,
        "batch_size": response.batch_size,
        "epochs": response.epochs,
        "learning_rate": response.learning_rate,
    }
    try:
        task_id = ml_tasks.register_task(
            context, payload=response, task_type=TaskType.TRAINING.value
        )
    except PreconditionError as exc:
        problems.append(str(exc))
    else:
        watch = ml_tasks.watch_task(context, task_id=task_id, task_type=TaskType.TRAINING.value)
        evidence["watch"] = watch
        problems.extend(ml_tasks.observation_problems(watch))
        if str(watch["task_type"]) != TaskType.TRAINING.value:
            notes.append(
                f"тип задачи на стенде — «{watch['task_type'] or 'не определён'}», "
                "ожидался `training`"
            )
        if not watch["terminal"]:
            notes.append(
                "задача не завершилась: она остаётся на стенде и видна в панели «Задачи» "
                f"(`task_id={response.task_id}`)"
            )

    trained = ml_tasks.wait_for_model(context, name, timeout=30.0)
    if trained is not None:
        evidence["trained_model"] = ml_tasks.model_snapshot(trained)
        add_test_entity(
            context.session,
            entity_id=str(trained.id),
            entity_type=ENTITY_TRAINED,
            check_id=context.spec.check_id,
            note=f"результат обучения {name} (сессия {context.session.session_id})",
        )
    else:
        notes.append(
            "модель-результат не найдена в реестре: её метрики и судьбу проверит `TC-TR-04`"
        )

    verdict = (
        f"обучение запущено: задача {response.task_id} типа `training`, "
        f"финальный статус «{evidence.get('watch', {}).get('final_label') or response.status}»"
    )
    return ml_tasks.async_outcome(
        verdict=verdict, evidence=evidence, problems=problems, notes=notes
    )


def _training_result(context: AutomationContext) -> CheckOutcome:
    """TC-TR-04: результат обучения — метрики, версия и решение по артефакту (UC-21).

    Сценарий работает **после** `TC-TR-02`: модель-результат берётся из записи сессии
    (по `trained_model_name`), карточка сравнивается с состоянием базовой модели, а
    решение оператора по артефакту (`keep_artifact`) исполняется здесь: оставить модель
    артефактом испытаний или удалить её вместе с файлом `.h5` (NFR-T4, `TC-CLEAN-04`).
    """
    record = next(
        (
            dict(item)
            for item in reversed(list(context.session.checks))
            if str(item.get("check_id")) == "TC-TR-02"
        ),
        None,
    )
    name = str(context.param("trained_model_name") or "").strip()
    if not name and record is not None:
        name = str((record.get("evidence") or {}).get("params", {}).get("trained_model_name") or "")
    evidence: dict[str, Any] = {
        "training_record": bool(record),
        "trained_model_name": name,
    }
    trained = ml_tasks.find_model_by_name(context, name) if name else None
    if trained is None:
        raise PreconditionError(
            "модель-результат обучения не найдена: сначала выполните `TC-TR-02` "
            "(запуск обучения) или укажите `trained_model_name` на карточке проверки"
        )

    before = dict((record or {}).get("evidence", {}).get("base_model") or {})
    after = ml_tasks.model_snapshot(trained)
    evidence.update(
        {
            "model_id": str(trained.id),
            "before": before,
            "after": after,
            "version_changed": bool(
                before.get("version") and str(before.get("version")) != after["version"]
            ),
        }
    )
    problems: list[str] = []
    notes: list[str] = []
    if not after["metrics_filled"]:
        problems.append("после обучения у модели пусты `metrics`: результат обучения не записан")
    if not after["version"]:
        problems.append("у модели-результата нет `version`")
    if before and not evidence["version_changed"]:
        notes.append(
            f"`version` не изменилась после обучения ({after['version']}) — "
            "проверьте, обновляет ли сервер версию результата"
        )
    if before and after["updated_at"] == str(before.get("updated_at") or ""):
        notes.append("`updated_at` не изменилась после обучения")
    if ml_tasks.find_model_by_name(context, str(trained.name)) is None:
        problems.append("модель-результат не видна в реестре `GET /api/ml_models/models`")

    decision = ml_tasks.cleanup_model(
        context,
        trained,
        keep=ml_tasks.artifact_decision(context),
        reason="результат обучения (TC-TR-04)",
    )
    evidence["artifact"] = decision
    if decision["action"] == "не удалось удалить":
        problems.append(f"самоочистка не выполнена: {decision['detail']}")

    verdict = (
        f"результат обучения зафиксирован: {trained.name} · {after['version']}; "
        f"метрик: {len(after['metrics'])}; решение по артефакту — {decision['action']}"
    )
    return ml_tasks.async_outcome(
        verdict=verdict, evidence=evidence, problems=problems, notes=notes
    )


def _training_control(context: AutomationContext) -> CheckOutcome:
    """TC-TR-03: управление обучением — `pause`/`resume`/`interrupt` на живой задаче (UC-28).

    Проверка запускает обучение и **ждёт статус `running`**: только на идущей задаче
    переходы FSM-1 имеют смысл. Затем посылаются команды и после каждой ожидается
    ожидаемый статус: `paused` (`pausing`), снова `running`, терминальный после
    `interrupt`. Обучение в итоге прерывается, поэтому модель-результат (если появилась
    недоделанной) удаляется — она не годна как артефакт (NFR-T4).
    """
    model = ml_tasks.trained_model_for(context)
    dataset = ml_tasks.dataset_for(context)
    name = ml_tasks.test_name(context, "_control")
    epochs = ml_tasks.param_int(context, "epochs", ml_tasks.DEFAULT_EPOCHS)
    command_timeout = ml_tasks.param_float(context, "command_timeout", 120.0)
    evidence: dict[str, Any] = {
        "base_model": ml_tasks.model_snapshot(model),
        "dataset_id": str(dataset.id),
        "params": {"epochs": epochs, "trained_model_name": name},
        "commands": [],
        "timeout": ml_tasks.timeout_of(context),
    }
    problems: list[str] = []
    notes: list[str] = []
    state: dict[str, Any] = {}

    def control() -> None:
        """Команды FSM-1 на живой задаче с ожиданием каждого перехода."""
        from lib.polling import poll_until

        monitor = ml_tasks.monitor_of(context)
        session = context.session
        task_id = str(state.get("task_id") or "")

        def wait_for(statuses: tuple[str, ...], label: str) -> str:
            poll = poll_until(
                lambda: monitor.poll_once(session, task_id),
                lambda item: str(item.status) in statuses or not item.ok,
                interval=ml_tasks.POLL_INTERVAL,
                timeout=command_timeout,
            )
            reached = str(poll.status)
            if reached not in statuses:
                problems.append(
                    f"после команды {label} задача не пришла к статусу {', '.join(statuses)}: "
                    f"«{poll.status_label}»"
                )
            return reached

        for action, expected, label in (
            ("pause", ("pausing", "paused"), "pause"),
            ("resume", ("running",), "resume"),
            ("interrupt", ("interrupted", "completed", "failed"), "interrupt"),
        ):
            command = monitor.run_command(session, task_id, action)
            row = {
                "action": action,
                "allowed": bool(command.allowed),
                "sent": bool(command.sent),
                "status": command.status,
                "variant": str(command.variant or ""),
                "note": str(command.note or ""),
                "error": str(command.error or ""),
            }
            if not command.allowed:
                problems.append(
                    f"команда {label} запрещена текущим статусом: "
                    f"{command.variant or command.note or command.error}"
                )
            elif not command.sent:
                problems.append(f"команда {label} не отправлена: {command.error or command.note}")
            row["reached"] = wait_for(expected, label)
            evidence["commands"].append(row)

    try:
        response = context.models_api().train(
            model.id,
            dataset_id=str(dataset.id),
            epochs=epochs,
            trained_model_name=name,
        )
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED, f"запуск обучения для управления не выполнен: {exc}", evidence
        )
    evidence["response"] = {"task_id": str(response.task_id or ""), "status": str(response.status)}
    try:
        task_id = ml_tasks.register_task(
            context,
            payload=response,
            task_type=TaskType.TRAINING.value,
            note="обучение для TC-TR-03",
        )
    except PreconditionError as exc:
        return CheckOutcome(CheckStatus.FAILED, str(exc), evidence)
    state["task_id"] = task_id
    evidence["task_id"] = task_id

    watch = ml_tasks.watch_task(
        context, task_id=task_id, task_type=TaskType.TRAINING.value, on_running=control
    )
    evidence["watch"] = watch
    if not evidence["commands"]:
        problems.append(
            "задача не дошла до статуса `running`: команды FSM-1 отправить не удалось "
            f"(финальный статус «{watch['final_label']}»)"
        )
    else:
        problems.extend(ml_tasks.observation_problems(watch, expected=("interrupted",)))
    if watch["timeout_hit"]:
        notes.append("задача осталась на стенде: прервать её можно из панели «Задачи»")

    partial = ml_tasks.find_model_by_name(context, name)
    if partial is not None:
        evidence["trained_model"] = ml_tasks.model_snapshot(partial)
        cleanup = ml_tasks.cleanup_model(context, partial, keep=False, reason="прерванное обучение")
        evidence["cleanup"] = cleanup
        if cleanup["action"] == "не удалось удалить":
            problems.append(f"самоочистка не выполнена: {cleanup['detail']}")

    reached = ", ".join(str(row["reached"]) for row in evidence["commands"])
    verdict = (
        f"переходы FSM-1 на задаче {task_id}: команд {len(evidence['commands'])}, "
        f"статусы после команд — {reached or 'нет'}; финал «{watch['final_label']}»"
    )
    return ml_tasks.async_outcome(
        verdict=verdict, evidence=evidence, problems=problems, notes=notes
    )


def _training_check(context: AutomationContext) -> CheckOutcome:
    """TC-TR-05: проверка модели — задача `model-testing` и её результаты (UC-22, BR-F5).

    Проверка запускает `POST /api/ml_models/{model_id}/check` на выбранном датасете
    с порогом из карточки, ведёт задачу `model-testing` монитором до терминального
    состояния и фиксирует результат: `validation_info` из ответа и карточку модели
    до/после (метрики и время обновления). Сущностей проверка не создаёт.
    """
    model = ml_tasks.trained_model_for(context)
    dataset = ml_tasks.dataset_for(context)
    threshold = ml_tasks.param_float(context, "threshold", ml_tasks.DEFAULT_THRESHOLD)
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
        response = context.models_api().check_model(
            model.id, dataset_id=str(dataset.id), threshold=threshold
        )
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(CheckStatus.FAILED, f"проверка модели не запущена: {exc}", evidence)

    evidence["response"] = {
        "task_id": str(response.task_id or ""),
        "status": str(response.status or ""),
        "message": str(response.message or ""),
        "validation_info": response.validation_info,
    }
    try:
        task_id = ml_tasks.register_task(
            context, payload=response, task_type=TaskType.MODEL_TESTING.value
        )
    except PreconditionError as exc:
        problems.append(str(exc))
    else:
        watch = ml_tasks.watch_task(
            context, task_id=task_id, task_type=TaskType.MODEL_TESTING.value
        )
        evidence["watch"] = watch
        problems.extend(ml_tasks.observation_problems(watch))
        if str(watch["task_type"]) != TaskType.MODEL_TESTING.value:
            notes.append(
                f"тип задачи на стенде — «{watch['task_type'] or 'не определён'}», "
                "ожидался `model-testing`"
            )

    try:
        card = context.models_api().get_model(model.id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        notes.append(f"карточку модели после проверки прочитать не удалось: {exc}")
    else:
        after = ml_tasks.model_snapshot(card)
        evidence["model_after"] = after
        if after["updated_at"] == str(model.updated_at or ""):
            notes.append(
                "`updated_at` модели не изменилась после проверки: результаты, возможно, "
                "хранятся только в задаче"
            )
        if not after["metrics_filled"]:
            notes.append("`metrics` модели после проверки пусты")

    verdict = (
        f"проверка модели запущена: задача {response.task_id} типа `model-testing`, "
        f"финальный статус «{evidence.get('watch', {}).get('final_label') or response.status}», "
        f"порог {threshold}"
    )
    return ml_tasks.async_outcome(
        verdict=verdict, evidence=evidence, problems=problems, notes=notes
    )


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
    "training.start": _training_start,
    "training.control": _training_control,
    "training.result": _training_result,
    "training.check": _training_check,
    "training.onnx_export": _onnx_export,
}
