"""Карточка запуска: подтверждение изменяющих и ресурсоёмких проверок (`FR-P-19`).

Правило испытаний (`FR-P-19`, `NFR-P-5`, `AC-P-13`): проверка класса `live`
(реальные данные) или `heavy` (ресурсоёмкая) **не запускается**, пока оператор не
заполнил карточку запуска — что проверяем, какими данными, кто отвечает, что
делать с артефактами — и не подтвердил расход ресурсов. Проверки класса `tech`
идут без карточки (`runner.needs_confirmation` возвращает `False`).

Заполненная карточка попадает в доказательства результата
(`RunOutcome.check_result.evidence["run_card"]`, пишет `runner.execute_check`),
поэтому из протокола и отчёта видно, на каких данных получен результат.

Модуль чистый: без интерфейса и без обращений к стенду. Перенесён сюда из
`acceptance/ui/components/run_card.py` (`acceptance/ui` удалён) при переходе на тонкий API
(`llm_tasks/31.4` §3): заполнение формы живёт в интерфейсе, а проверка полей и
запись доказательства — в ядре, чтобы она проверялась unit-тестами.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from acceptance.programme import ProgrammeItem
from acceptance.session import now_iso

#: Обязательные поля карточки запуска (без них запуск не подтверждается).
REQUIRED: tuple[str, ...] = ("goal", "data", "responsible")

#: Поля выбора данных и модели для ML-проверок (`train`, `check`, `inference`):
#: обязательны не всегда, а только когда проверке они действительно нужны
#: (`validate(required_extra=…)`, решение заказчика — `llm_tasks/49`, задача 1б).
ML_FIELDS: tuple[str, ...] = ("dataset_id", "model_id")

#: Подписи полей карточки (для формы и для доказательств результата).
FIELD_LABELS: dict[str, str] = {
    "goal": "Цель проверки",
    "data": "Данные, на которых выполняется",
    "dataset_id": "Датасет для проверки",
    "model_id": "Модель",
    "epochs": "Эпох обучения",
    "batch_size": "Размер батча",
    "learning_rate": "Скорость обучения",
    "threshold": "Порог классификации",
    "sample_file_id": "Файл сэмпла (для `inference/single`)",
    "keep_artifact": "Оставить результат обучения артефактом",
    "responsible": "Ответственный",
    "artifacts": "Что делать с артефактами",
    "confirm": "Подтверждаю расход ресурсов",
}

#: Параметры ресурсоёмких проверок: подставляются и в карточку, и в тело вызова
#: (`runner.ML_PARAMS`). Значения по умолчанию совпадают со спецификацией.
ML_DEFAULTS: dict[str, Any] = {
    "epochs": 3,
    "batch_size": 32,
    "learning_rate": 1e-07,
    "threshold": 0.5,
    "sample_file_id": "",
    "keep_artifact": False,
}

#: Варианты действий с артефактами после проверки (макет `docs/16`, `SCR-301`).
ARTIFACT_ACTIONS: tuple[str, ...] = (
    "удалить после проверки",
    "оставить для разбора",
    "выгрузить в доказательства",
)


def defaults(
    item: ProgrammeItem,
    *,
    session_id: str = "",
    author: str = "",
    data_hint: str = "",
) -> dict[str, Any]:
    """Заготовка карточки: цель из пункта, данные — из подсказки или `__TEST__`.

    Подстановка нужна, чтобы оператор правил, а не набирал: пустая форма на
    изменяющей проверке — лишний повод ошибиться в данных. `data_hint` заполняет
    «Данные, на которых выполняется» названием выбранного датасета
    («датасет «skfu-train» (104 файла)»): для ML-проверок это и есть настоящие данные,
    и подпись уходит в доказательства результата (`llm_tasks/49`, вопрос 5 = а).
    """
    data = str(data_hint or "").strip() or "__TEST__ (пульт)"
    if session_id:
        data = f"{data}, сессия {session_id}"
    return {
        "goal": f"{item.check_id}: {item.title}".strip(": "),
        "data": data,
        "dataset_id": "",
        "model_id": "",
        **ML_DEFAULTS,
        "responsible": str(author),
        "artifacts": ARTIFACT_ACTIONS[0],
        "confirm": False,
    }


def validate(values: Mapping[str, Any], *, required_extra: Sequence[str] = ()) -> list[str]:
    """Список незаполненных обязательных полей карточки (пустой — можно запускать).

    `required_extra` — поля, обязательные **для этой проверки** (`dataset_id`, `model_id`).
    Требовать датасет у проверки, которая его не использует, значило бы мешать оператору
    (`llm_tasks/49`, задача 1б).
    """
    missing: list[str] = []
    for field in (*REQUIRED, *required_extra):
        if not str(values.get(field) or "").strip():
            missing.append(FIELD_LABELS.get(field, field))
    if not bool(values.get("confirm")):
        missing.append(FIELD_LABELS["confirm"])
    return missing


def evidence(values: Mapping[str, Any], *, author: str = "", at: str = "") -> dict[str, Any]:
    """Карточка запуска как доказательство результата (идёт в `evidence["run_card"]`).

    Выбранные датасет и модель записываются и идентификаторами, и подписями: по протоколу
    должно быть видно не только «на каком датасете считали», но и как он назывался
    и сколько в нём файлов (`llm_tasks/49`, вопрос 5 = а). Параметры ресурсоёмкой
    операции (`epochs`, `batch_size`, `learning_rate`, `threshold`, файл сэмпла и
    решение об артефакте) тоже попадают в доказательства: по ним воспроизводится прогон.
    """
    payload: dict[str, Any] = {
        "at": at or now_iso(),
        "author": str(author),
        "goal": str(values.get("goal") or "").strip(),
        "data": str(values.get("data") or "").strip(),
        "dataset_id": str(values.get("dataset_id") or "").strip(),
        "dataset_label": str(values.get("dataset_label") or "").strip(),
        "model_id": str(values.get("model_id") or "").strip(),
        "model_label": str(values.get("model_label") or "").strip(),
        "responsible": str(values.get("responsible") or "").strip(),
        "artifacts": str(values.get("artifacts") or "").strip(),
        "confirm": bool(values.get("confirm")),
    }
    payload["ml"] = {
        name: values.get(name)
        for name in ML_DEFAULTS
        if str(values.get(name) or "").strip() or values.get(name) is True
    }
    payload["missing"] = validate(values)
    payload["filled"] = not payload["missing"]
    return payload
