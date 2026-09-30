"""Общие механики ресурсоёмких ML-проверок: запуск задачи и наблюдение (T7–T8).

Проверки `TC-TR-02…05` и `TC-INF-01` устроены одинаково: они запускают
асинхронную операцию стенда (`train`, `check`, `inference`), получают `task_id`,
ведут задачу монитором до терминального состояния и фиксируют параметры, статусы
FSM-1, длительность и результат. Отличаются только операция и её последствия,
поэтому общая часть живёт здесь.

Почему так, а не в каждом сценарии отдельно:

    * правило «монитор или нет» одно: без монитора проверку нельзя выполнить —
      это предусловие (`PreconditionError`), а не отказ;
    * длительность задачи неизвестна (обучение идёт минутами), поэтому таймаут —
      параметр карточки запуска (`timeout`), а его истечение — **не** отказ сам по
      себе: сценарий фиксирует «задача не дошла до терминального состояния» и
      оставляет задачу на стенде, а оператор видит это в вердикте (`llm_tasks/52`);
    * результат обучения — новая модель с именем `__TEST__…`: её судьбу решает
      оператор (`keep_artifact`), а по умолчанию сценарий убирает её за собой
      вместе с файлом `.h5` (NFR-T4).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from acceptance.checks.registry import CheckStatus
from acceptance.checks.runner import AutomationContext, CheckOutcome, PreconditionError
from acceptance.endpoints import TEST_PREFIX
from acceptance.ml_samples import pick_dataset, pick_model
from acceptance.session import add_test_entity, mark_test_entity_deleted
from acceptance.tasks_monitor import task_id_from_payload
from client.errors import ApiError, ClientError, ServerUnavailableError
from client.schemas import ModelMetadata

#: Значения по умолчанию для ресурсоёмких операций (совпадают со спецификацией).
DEFAULT_EPOCHS = 3
DEFAULT_BATCH_SIZE = 32
DEFAULT_LEARNING_RATE = 1e-7
DEFAULT_THRESHOLD = 0.5

#: Таймаут наблюдения за задачей по умолчанию, секунды (обучение идёт минутами).
DEFAULT_TIMEOUT = 600.0

#: Период опроса задачи, секунды.
POLL_INTERVAL = 2.0

#: Тип учёта модели, оставленной артефактом испытаний (`TC-CLEAN-04`).
ENTITY_ARTIFACT = "модель (артефакт)"


def param_int(context: AutomationContext, name: str, default: int) -> int:
    """Целое значение параметра проверки (карточка запуска главнее умолчания)."""
    raw = str(context.param(name) or "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def param_float(context: AutomationContext, name: str, default: float) -> float:
    """Дробное значение параметра проверки (запятая как разделитель принимается)."""
    raw = str(context.param(name) or "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def timeout_of(context: AutomationContext) -> float:
    """Таймаут наблюдения за задачей из параметров проверки (секунды)."""
    return max(1.0, param_float(context, "timeout", DEFAULT_TIMEOUT))


def test_name(context: AutomationContext, suffix: str = "") -> str:
    """Имя `__TEST__`-объекта проверки: `__TEST__<проверка>_<сессия><суффикс>`."""
    session_id = str(context.session.session_id or "session")
    base = f"{TEST_PREFIX}{context.spec.check_id.replace('-', '_')}_{session_id}"
    return f"{base}{suffix}"


def monitor_of(context: AutomationContext) -> Any:
    """Монитор задач.

    Raises:
        PreconditionError: монитора нет (проверка запускается вне пульта) — вести
            асинхронную задачу нечем.
    """
    return context.require_monitor()


def dataset_for(context: AutomationContext) -> Any:
    """Датасет для ресурсоёмкой проверки: из карточки/параметров либо первый в реестре.

    Raises:
        PreconditionError: датасета нет ни в параметрах, ни в реестре стенда.
    """
    try:
        rows = list(context.datasets_api().list_datasets().datasets)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр датасетов не получен: {exc}") from exc
    chosen = pick_dataset(rows, dataset_id=context.param("dataset_id"))
    if chosen is None:
        raise PreconditionError(
            "датасет не выбран: укажите `dataset_id` в карточке запуска "
            "(в реестре стенда датасетов нет)"
        )
    return chosen


def trained_model_for(context: AutomationContext) -> ModelMetadata:
    """Обученная модель (с `metrics`) либо явно выбранная оператором.

    Raises:
        PreconditionError: подходящей модели нет — обучать/проверять нечего.
    """
    try:
        rows = list(context.models_api().list_models())
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр моделей не получен: {exc}") from exc
    chosen = pick_model(rows, model_id=context.param("model_id"), with_metrics=True) or pick_model(
        rows, model_id=context.param("model_id")
    )
    if chosen is None:
        raise PreconditionError("модель не выбрана: укажите `model_id` в карточке запуска")
    return chosen


def register_task(
    context: AutomationContext, *, payload: Any, task_type: str, note: str = ""
) -> str:
    """Ставит задачу из ответа на наблюдение и возвращает её идентификатор.

    Raises:
        PreconditionError: ответ не содержит `task_id` — вести задачу нечем.
    """
    data = _payload_mapping(payload)
    task_id = task_id_from_payload(data)
    if not task_id:
        raise PreconditionError(
            f"сервер не вернул `task_id` в ответе: {str(payload)[:200]} — "
            "наблюдение за задачей невозможно"
        )
    monitor_of(context).register_from_response(
        context.session,
        data,
        check_id=context.spec.check_id,
        task_type=task_type,
        note=note or f"задача создана проверкой {context.spec.check_id}",
    )
    return task_id


def _payload_mapping(payload: Any) -> Any:
    """Ответ API как словарь: схемы клиента (`ModelTrainingResponse`) тоже принимаются."""
    if isinstance(payload, dict) or payload is None:
        return payload
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:  # pragma: no cover - старые версии pydantic
            return dump()
    return payload


def watch_task(
    context: AutomationContext,
    *,
    task_id: str,
    task_type: str = "",
    timeout: float | None = None,
    on_running: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Ведёт задачу монитором до терминального состояния и собирает доказательства.

    Args:
        task_id: задача, поставленная на наблюдение.
        task_type: ожидаемый тип (`training`, `model-testing`, `inference`) — идёт
            в доказательства.
        timeout: предел ожидания, секунды (по умолчанию — из параметров проверки).
        on_running: действие, выполняемое **один раз** при первом статусе `running`
            (нужно `TC-TR-03`: команды FSM-1 шлются на живой задаче).

    Returns:
        Доказательства наблюдения: `statuses`, `final`, `terminal`, `polls`,
        `duration_ms`, `journal_seq`, `errors`, `note`, `timeout_hit`.
    """
    from acceptance.tasks_monitor import status_label
    from lib.polling import poll_until

    monitor = monitor_of(context)
    session = context.session
    limit = timeout if timeout is not None else timeout_of(context)
    hook_done = False

    def fetch() -> Any:
        nonlocal hook_done
        poll = monitor.poll_once(session, str(task_id))
        if on_running is not None and not hook_done and poll.status == "running":
            hook_done = True
            on_running()
        return poll

    last = poll_until(
        fetch,
        lambda poll: bool(poll.terminal) or not poll.ok,
        interval=POLL_INTERVAL,
        timeout=limit,
    )
    record = next(
        (dict(item) for item in session.tasks if str(item.get("task_id")) == str(task_id)), {}
    )
    history = [dict(step) for step in (record.get("history") or [])]
    poll = dict(record.get("poll") or {})
    final = str(record.get("status") or "")
    terminal = bool(last.terminal)
    return {
        "task_id": str(task_id),
        "task_type": task_type or str(record.get("type") or ""),
        "statuses": [str(step.get("status") or "") for step in history],
        "history": history[-10:],
        "final": final,
        "final_label": status_label(final),
        "terminal": terminal,
        "timeout_hit": not terminal and bool(last.ok),
        "ok": bool(last.ok),
        "error": str(last.error or ""),
        "polls": int(poll.get("polls") or 0),
        "duration_ms": poll.get("duration_ms"),
        "journal_from": record.get("journal_from"),
        "journal_seq": record.get("journal_seq"),
        "errors": list(record.get("errors") or []),
        "note": str(record.get("note") or ""),
    }


def find_model_by_name(context: AutomationContext, name: str) -> ModelMetadata | None:
    """Модель реестра по имени (результат обучения ищется по `trained_model_name`)."""
    try:
        rows = list(context.models_api().list_models())
    except (ApiError, ClientError, ServerUnavailableError):
        return None
    return next((row for row in rows if str(row.name) == str(name)), None)


def wait_for_model(
    context: AutomationContext, name: str, *, timeout: float | None = None
) -> ModelMetadata | None:
    """Ждёт появления модели с именем результата обучения (реестр читается опросом)."""
    from lib.polling import poll_until

    limit = min(60.0, timeout if timeout is not None else timeout_of(context))
    found = poll_until(
        lambda: find_model_by_name(context, name),
        lambda model: model is not None,
        interval=POLL_INTERVAL,
        timeout=limit,
    )
    return found if isinstance(found, ModelMetadata) else None


def model_snapshot(model: ModelMetadata) -> dict[str, Any]:
    """Слепок модели до/после обучения: версия, метрики, время обновления."""
    return {
        "model_id": str(model.id),
        "name": str(model.name),
        "version": str(model.version or ""),
        "metrics": dict(model.metrics or {}),
        "metrics_filled": bool(model.metrics),
        "updated_at": str(model.updated_at or ""),
        "file_id": str(model.file_id or ""),
    }


def artifact_decision(context: AutomationContext) -> bool:
    """Решение оператора по артефакту: `keep_artifact` из карточки (по умолчанию — нет)."""
    raw = str(context.param("keep_artifact") or "").strip().lower()
    return raw in ("1", "true", "да", "yes", "оставить", "оставить артефактом")


def cleanup_model(
    context: AutomationContext, model: ModelMetadata | None, *, keep: bool, reason: str = ""
) -> dict[str, Any]:
    """Решает судьбу модели-результата: оставить артефактом или удалить (NFR-T4).

    Returns:
        `{"action": ..., "detail": ...}` — «оставлена артефактом», «удалена»,
        «не удалось удалить» или «нечего убирать».
    """
    if model is None:
        return {"action": "нечего убирать", "detail": "модель-результат не найдена"}

    model_id = str(model.id)
    if keep:
        session_id = str(context.session.session_id)
        add_test_entity(
            context.session,
            entity_id=model_id,
            entity_type=ENTITY_ARTIFACT,
            check_id=context.spec.check_id,
            note=f"{reason or 'результат проверки'} оставлен артефактом (сессия {session_id})",
        )
        return {
            "action": "оставлена артефактом",
            "detail": f"{model.name} · {model.version} остаётся на стенде как артефакт испытаний",
        }

    try:
        context.models_api().delete_model(model_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return {"action": "не удалось удалить", "detail": f"{type(exc).__name__}: {exc}"}
    mark_test_entity_deleted(
        context.session, model_id, check_id=context.spec.check_id, note="самоочистка результата"
    )
    return {"action": "удалена", "detail": _cleanup_file(context, model)}


def _cleanup_file(context: AutomationContext, model: ModelMetadata) -> str:
    """Убирает файл `.h5` модели-результата, если он остался в реестре файлов."""
    file_id = str(model.file_id or "")
    if not file_id:
        return "модель-результат удалена"
    try:
        rows = context.files_api().list_files(file_id=file_id).files
    except (ApiError, ClientError, ServerUnavailableError):
        rows = []
    if not rows:
        return "модель-результат удалена (файл `.h5` убран каскадом)"
    try:
        context.files_api().delete(file_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return f"модель удалена, файл `.h5` остался: {exc}"
    mark_test_entity_deleted(
        context.session,
        file_id,
        check_id=context.spec.check_id,
        note="самоочистка файла модели-результата",
    )
    return "модель-результат и её файл `.h5` удалены"


def async_outcome(
    *, verdict: str, evidence: dict[str, Any], problems: list[str], notes: list[str]
) -> CheckOutcome:
    """Итог асинхронной проверки: отказ при проблемах, иначе успех с примечаниями."""
    if problems:
        return CheckOutcome(
            CheckStatus.FAILED, "; ".join(problems), evidence, note="; ".join(notes)
        )
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence, note="; ".join(notes))


def observation_problems(
    watch: dict[str, Any], *, expected: tuple[str, ...] = ("completed",)
) -> list[str]:
    """Проблемы наблюдения: задача не пошла, не дошла до конца или завершилась ошибкой."""
    problems: list[str] = []
    if not watch["ok"]:
        problems.append(f"наблюдение за задачей не выполнено: {watch['error'] or 'нет ответа'}")
        return problems
    if watch["timeout_hit"]:
        problems.append(
            f"задача не дошла до терминального состояния за отведённое время "
            f"(последний статус «{watch['final_label']}»)"
        )
        return problems
    if expected and str(watch["final"]) not in expected:
        problems.append(
            f"задача завершилась статусом «{watch['final_label']}» вместо {', '.join(expected)}"
        )
    return problems
