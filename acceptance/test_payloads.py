"""«Что будет отправлено»: `__TEST__`-данные, которые создают сценарии проверок.

Пожелание заказчика (21.09.2026, п. 5): оператор (и приёмная комиссия) должны видеть
**до запуска**, какие данные пульт отправит на стенд — имена, содержимое и то, уберёт
ли он их за собой. Дело в том, что тела и имена `__TEST__`-сущностей генерирует код
сценариев (`acceptance/checks/files.py`, `datasets.py`), а не оператор, поэтому раньше
их можно было увидеть только в журнале **после** прогона.

Модуль — единый источник описаний: он не дублирует правила сценариев, а берёт
константы тел из `acceptance.checks.files` и правила имён из тех же модулей, поэтому
предпросмотр не разойдётся с фактом. Если сценарий меняет тело или имя, тест
`tests/unit/test_test_payloads.py` это заметит.

Правила вывода — по операциям проверки из каталога:

    * `post /api/data/file` → CSV-файл `__TEST__<проверка>_<сессия>.csv` (`file_type=RAW`);
    * `post /api/datasets/` → датасет `__TEST__dataset-<8 hex>`, тип `direct_fill`;
    * `post /api/datasets/fill/{dataset_id}` → тело наполнения (список `file_ids`).

Проверки только для чтения payload не создают — блок честно об этом сообщает.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from acceptance.checks import files as files_checks
from acceptance.checks.registry import CheckSpec
from acceptance.endpoints import TEST_PREFIX

#: Пометка «имя/идентификатор создаётся при выполнении» (случайный суффикс или id сессии).
GENERATED = "генерируется при выполнении"

#: Операции, по которым видно, какие `__TEST__`-данные создаёт проверка.
UPLOAD_FILE_OPERATION = "post /api/data/file"
CREATE_DATASET_OPERATION = "post /api/datasets/"
FILL_DATASET_OPERATION = "post /api/datasets/fill/{dataset_id}"

#: Ресурсоёмкие ML-операции: обучение, проверка и инференс по датасету (`T6–T9`).
#: Они ничего не «создают» в смысле `__TEST__`, но отправляют на стенд данные и модель,
#: поэтому оператор обязан видеть это до запуска (`llm_tasks/48` §1б).
ML_OPERATIONS: tuple[str, ...] = (
    "post /api/ml_models/models/{model_id}/train",
    "post /api/ml_models/{model_id}/check",
    "post /api/ml_models/models/{model_id}/inference",
)

#: Как называется ML-операция для оператора.
ML_KIND_LABELS: dict[str, str] = {
    "post /api/ml_models/models/{model_id}/train": "обучение модели",
    "post /api/ml_models/{model_id}/check": "проверка модели на датасете",
    "post /api/ml_models/models/{model_id}/inference": "инференс по датасету",
}

#: Кто убирает за собой `__TEST__`-данные (уборка проверяется отдельными проверками).
CLEANUP_NOTE = "уборка — TC-CLEAN-01/TC-CLEAN-02 (учёт `__TEST__`-сущностей в сессии)"


@dataclass(frozen=True)
class PayloadPreview:
    """Один payload, который уйдёт на стенд при выполнении проверки."""

    kind: str
    target: str
    name: str
    content: str
    removed_after: bool
    note: str = ""

    @property
    def is_generated(self) -> bool:
        """True, если имя или идентификатор создаёт сервер/сценарий во время прогона."""
        return GENERATED in self.name

    def to_dict(self) -> dict[str, Any]:
        """Представление для сессии, отчёта и интерфейса."""
        return {
            "kind": self.kind,
            "target": self.target,
            "name": self.name,
            "content": self.content,
            "removed_after": self.removed_after,
            "note": self.note,
        }


def payload_previews(
    spec: CheckSpec,
    session: Any = None,
    *,
    params: dict[str, Any] | None = None,
) -> list[PayloadPreview]:
    """Возвращает список payload'ов, которые создаст проверка (правила — из каталога).

    Args:
        spec: описание проверки каталога.
        session: сессия испытаний (нужна для имени файла и для `__TEST__`-сущностей).
        params: параметры запуска проверки (переопределяют `csv_name`, `csv_body`).
    """
    values = dict(params or {})
    session_id = str(getattr(session, "session_id", "") or "session")
    operations = {operation.lower() for operation in spec.endpoints}
    previews: list[PayloadPreview] = []

    if UPLOAD_FILE_OPERATION in operations:
        previews.append(_file_payload(spec, session_id=session_id, values=values))
    if CREATE_DATASET_OPERATION in operations:
        previews.append(_dataset_payload(spec))
    if FILL_DATASET_OPERATION in operations:
        previews.append(_fill_payload(spec))
    ml_operation = next((target for target in spec.endpoints if target in ML_OPERATIONS), "")
    if ml_operation:
        previews.append(_ml_payload(ml_operation, values=values))
    return previews


def preview_lines(previews: list[PayloadPreview]) -> list[str]:
    """Строки для markdown-блока «что будет отправлено»."""
    if not previews:
        return [
            "Проверка ничего не создаёт: все её вызовы только читают данные "
            "(имена и тела `__TEST__`-данных не формируются)."
        ]
    lines: list[str] = []
    for item in previews:
        cleanup = "да, за собой убирает" if item.removed_after else "нет — проверить вручную"
        lines.append(f"* **{item.kind}** → `{item.target}`")
        lines.append(f"  * имя/идентификатор: `{item.name}`")
        lines.append(f"  * содержимое: {item.content}")
        lines.append(f"  * удаляется после проверки: {cleanup}")
        if item.note:
            lines.append(f"  * примечание: {item.note}")
    return lines


def summary(previews: list[PayloadPreview]) -> str:
    """Короткая сводка: сколько и каких payload'ов уйдёт на стенд."""
    if not previews:
        return "payload не создаётся (только чтение)"
    kinds = ", ".join(item.kind for item in previews)
    return f"{len(previews)}: {kinds}"


def _file_payload(spec: CheckSpec, *, session_id: str, values: dict[str, Any]) -> PayloadPreview:
    """CSV-файл, который загружает проверка (`POST /api/data/file`, multipart)."""
    default_name = f"{TEST_PREFIX}{spec.check_id.replace('-', '_')}_{session_id}.csv"
    name = str(values.get("csv_name") or default_name)
    body = str(values.get("csv_body") or files_checks.ROUND_TRIP_BODY)
    if body == files_checks.INVALID_BODY:
        content = (
            "CSV без обязательных колонок (негативная проба): "
            f"`{files_checks.INVALID_BODY.strip().replace(chr(10), ' | ')}`"
        )
    else:
        content = (
            f"CSV `{files_checks.MIME_CSV}`, колонки "
            f"{files_checks.ROUND_TRIP_BODY.splitlines()[0]}: "
            f"`{files_checks.ROUND_TRIP_BODY.strip().replace(chr(10), ' | ')}`"
        )
    return PayloadPreview(
        kind="файл",
        target="POST /api/data/file (multipart)",
        name=name,
        content=f"{content}; поле `file_type=RAW`, описание «проверка {spec.check_id}»",
        removed_after=True,
        note=CLEANUP_NOTE,
    )


def _dataset_payload(spec: CheckSpec) -> PayloadPreview:
    """Датасет, который создаёт проверка (`POST /api/datasets/`)."""
    return PayloadPreview(
        kind="датасет",
        target="POST /api/datasets/",
        name=f"{TEST_PREFIX}dataset-<8 hex, {GENERATED}>",
        content=f"имя `{TEST_PREFIX}…`, тип `direct_fill`, описание «создан проверкой {spec.check_id}»",
        removed_after=True,
        note=CLEANUP_NOTE,
    )


def _fill_payload(spec: CheckSpec) -> PayloadPreview:
    """Тело наполнения датасета (`POST /api/datasets/fill/{dataset_id}`)."""
    return PayloadPreview(
        kind="наполнение датасета",
        target="POST /api/datasets/fill/{dataset_id}",
        name=f"id датасета — {GENERATED}",
        content="JSON-тело со списком `file_ids` (файлы реестра или загруженный "
        f"`{TEST_PREFIX}…csv`)",
        removed_after=False,
        note=f"проверка {spec.check_id}; {CLEANUP_NOTE}",
    )


def _ml_payload(operation: str, *, values: dict[str, Any]) -> PayloadPreview:
    """Обучение/проверка/инференс: какая модель, на каком датасете и с какими параметрами.

    До этой правки ML-проверки выглядели как «ничего не создаёт», хотя отправляют
    ресурсоёмкий запрос с датасетом и моделью: оператор не видел ни того, ни другого
    до запуска (`llm_tasks/48` §1б).
    """
    dataset_id = str(values.get("dataset_id") or "").strip()
    dataset_label = str(values.get("dataset_label") or "").strip()
    model_id = str(values.get("model_id") or "").strip()
    model_label = str(values.get("model_label") or "").strip()
    dataset_text = (
        f"датасет «{dataset_label or dataset_id}» (`{dataset_id}`)"
        if dataset_id
        else "датасет не выбран: пока он не указан в карточке запуска, вызов не отправляется"
    )
    model_text = (
        f"модель «{model_label or model_id}» (`{model_id}`)"
        if model_id
        else f"модель — {GENERATED} (если моделей на стенде нет, сначала `TC-MOD-02`)"
    )
    method, _, path = operation.partition(" ")
    numbers = [
        f"{name}={values[name]}"
        for name in ("epochs", "batch_size", "learning_rate", "threshold")
        if str(values.get(name) or "").strip()
    ]
    sample = str(values.get("sample_file_id") or "").strip()
    extra = f"; параметры оператора: {', '.join(numbers)}" if numbers else ""
    if sample:
        extra += (
            f"; файл сэмпла: {TEST_PREFIX}… ({len(sample)} симв.)"
            if len(sample) > 12
            else f"; файл сэмпла: `{sample}`"
        )
    return PayloadPreview(
        kind=ML_KIND_LABELS.get(operation, "ML-операция"),
        target=f"{method.upper()} {path}",
        name=model_text,
        content=(
            f"{dataset_text}; параметры — из заготовки операции, выбранные значения "
            "подставляются в тело (`runner.substitute_placeholders`)" + extra
        ),
        removed_after=False,
        note=(
            "ресурсоёмкая операция: точные поля видны в журнале обмена после вызова, а "
            f"результат ведёт монитор `$Задач`; {CLEANUP_NOTE}"
        ),
    )
