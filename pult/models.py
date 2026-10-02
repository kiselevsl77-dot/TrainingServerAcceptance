"""Карточка модели стенда: свойства для выбора **конкретной** модели (контекст К2).

Реальная тренировка (`llm_tasks/59`) начинается с выбора модели не «первой из реестра»,
а той, у которой подходящие архитектура, фаза, группы сигналов и качество. До этой
правки свойства модели пульт не показывал вовсе: в таблице были только имя, архитектура,
версия, фаза и даты, а метрики и сигналы приходилось смотреть, открывая ответ
`GET /api/ml_models/models` в ленте обмена.

Модуль чистый: стенд читает сервер (`pult.server.api_model`), здесь — только правила.
Структуры взяты с живого стенда (01.10.2026), а не выдуманы:

    * `signals` — плоский список `{signal_num, device_id, device_info{load_id, category}}`,
      где групп столько же, сколько `output_size` (иначе `train` отвечает `400`);
    * `signal_aliases` — классы (`{"0": "miner", "1": "not_miner"}`);
    * `metrics.training_history.loss` — список значений по эпохам;
    * `metrics.testing_history.tests` — список `{accuracy, report_file_id, dataset_id,
      tested_at}` (по нему видно качество, а в Э3 — сравнение «до/после»);
    * `metrics.dataset_stats` — числа состава датасета обучения;
    * `metrics.memory_usage` — `rss_mb`, `vms_mb`, `percent`;
    * скаляры обучения: `epochs`, `batch_size`, `learning_rate`,
      `preprocessing_converter` (`stft`/`direct`), `use_streaming`, `augmentation_enabled`,
      `original_model_id`, `dataset_id`.

Отбор моделей пульт делает **на своей стороне**: фильтр `device_id` у списка моделей на
стенде отвечает `500` (подтверждённый дефект `TC-MOD-05`), поэтому фильтровать запросом
нельзя — реестр и так читается целиком.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from acceptance.endpoints import TEST_PREFIX

#: Ограничения вывода: карточка — для человека, а не для выгрузки истории целиком.
HISTORY_LIMIT = 30
LAYER_LIMIT = 40
STATS_LIMIT = 24

#: Подписи признаков модели: по ним оператор отбирает «свою» модель.
TRAINED_LABEL = "обучена"
NOT_TRAINED_LABEL = "не обучена"
BASE_LABEL = "базовая (результат обучения неизвестен)"
RESULT_LABEL = "результат обучения"

#: Поля карточки, которые показываем всегда (даже пустыми) — иначе «нет данных» и
#: «пустое значение» выглядят одинаково.
SCALAR_FIELDS: tuple[tuple[str, str], ...] = (
    ("architecture", "Архитектура"),
    ("phase_type", "Фаза"),
    ("output_size", "Выход (классов)"),
    ("version", "Версия"),
    ("file_id", "Файл модели"),
)


def _payload(model: Any) -> dict[str, Any]:
    """Словарь модели: принимает и pydantic-модель стенда, и обычный словарь."""
    if isinstance(model, Mapping):
        return dict(model)
    if hasattr(model, "model_dump"):
        return dict(model.model_dump())
    return dict(getattr(model, "__dict__", {}) or {})


def model_name(model: Any) -> str:
    """Имя модели (пустое — идентификатор, чтобы строка не выглядела «без имени»)."""
    data = _payload(model)
    return str(data.get("name") or data.get("id") or "")


def is_test_model(model: Any) -> bool:
    """True, если модель создана испытаниями (`__TEST__`) — её нельзя терять без решения."""
    return model_name(model).startswith(TEST_PREFIX)


def is_trained(model: Any) -> bool:
    """True, если у модели есть метрики: она обучалась (`UC-19`)."""
    return bool(_payload(model).get("metrics"))


def signal_groups(model: Any) -> list[dict[str, Any]]:
    """Группы сигналов модели: `signal_num` → устройства с категориями нагрузки.

    Число групп обязано совпадать с `output_size` (проверено живьём: иначе обучение
    отвечает «Number of signal groups (0) does not match output_size (2)»), поэтому
    карточка печатает и число групп, и список устройств.
    """
    data = _payload(model)
    grouped: dict[str, list[str]] = {}
    categories: dict[str, list[str]] = {}
    for item in data.get("signals") or []:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("signal_num")
        group = "" if raw is None else str(raw)
        device = str(item.get("device_id") or "")
        if not device:
            continue
        grouped.setdefault(group, []).append(device)
        info = item.get("device_info")
        category = ""
        if isinstance(info, Mapping):
            category = str(info.get("category") or "")
        if category:
            categories.setdefault(group, []).append(category)
    rows: list[dict[str, Any]] = []
    for group in sorted(grouped, key=lambda value: (len(value), value)):
        devices = sorted(set(grouped[group]))
        rows.append(
            {
                "signal_num": group,
                "devices": devices,
                "devices_text": ", ".join(devices),
                "categories": sorted(set(categories.get(group, []))),
                "count": len(devices),
            }
        )
    return rows


def class_rows(model: Any) -> list[dict[str, str]]:
    """Классы модели (`signal_aliases`): номер группы → понятное имя класса."""
    data = _payload(model)
    aliases = data.get("signal_aliases") or {}
    if not isinstance(aliases, Mapping):
        return []
    rows = [
        {"signal_num": str(number), "alias": str(alias or "")}
        for number, alias in aliases.items()
        if str(alias or "").strip()
    ]
    rows.sort(key=lambda item: (len(item["signal_num"]), item["signal_num"]))
    return rows


def layer_rows(model: Any) -> list[dict[str, str]]:
    """Слои модели из `config.layers` (архитектура «как есть» — для сверки с боевой)."""
    data = _payload(model)
    config = data.get("config") or {}
    layers = config.get("layers") if isinstance(config, Mapping) else None
    rows: list[dict[str, str]] = []
    for index, layer in enumerate(layers or [], start=1):
        if not isinstance(layer, Mapping):
            continue
        parameters = layer.get("parameters") or {}
        if isinstance(parameters, Mapping):
            text = ", ".join(
                f"{key}={value}" for key, value in parameters.items() if str(value or "").strip()
            )
        else:
            text = ""
        rows.append(
            {
                "order": str(index),
                "layer_type": str(layer.get("layer_type") or ""),
                "parameters": text,
            }
        )
    return rows[:LAYER_LIMIT]


def metrics(model: Any) -> dict[str, Any]:
    """Метрики модели (пусто — модель не обучалась)."""
    value = _payload(model).get("metrics")
    return dict(value) if isinstance(value, Mapping) else {}


def _series(value: Any) -> list[Any]:
    """Значения ряда: список/кортеж — как есть, всё остальное — пусто."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def training_rows(model: Any) -> list[dict[str, str]]:
    """История обучения (`loss` по эпохам) — строками «эпоха → значение»."""
    history = metrics(model).get("training_history")
    loss = _series(history.get("loss")) if isinstance(history, Mapping) else []
    rows: list[dict[str, str]] = []
    for index, value in enumerate(loss, start=1):
        if isinstance(value, int | float):
            text = f"{float(value):.6g}"
        else:
            text = str(value)
        rows.append({"epoch": str(index), "loss": text})
    return rows[-HISTORY_LIMIT:]


def testing_rows(model: Any) -> list[dict[str, str]]:
    """История проверок модели (`testing_history.tests`): точность, данные, время."""
    history = metrics(model).get("testing_history")
    tests = _series(history.get("tests")) if isinstance(history, Mapping) else []
    rows: list[dict[str, str]] = []
    for item in tests:
        if not isinstance(item, Mapping):
            continue
        accuracy = item.get("accuracy")
        rows.append(
            {
                "accuracy": (
                    f"{float(accuracy):.4f}" if isinstance(accuracy, int | float) else str(accuracy)
                ),
                "dataset_id": str(item.get("dataset_id") or ""),
                "tested_at": str(item.get("tested_at") or ""),
                "report_file_id": str(item.get("report_file_id") or ""),
            }
        )
    return rows[-HISTORY_LIMIT:]


def dataset_stat_rows(model: Any) -> list[dict[str, str]]:
    """Статистика датасета обучения: только скаляры, вложенные структуры пропускаем.

    `dataset_stats` с живого стенда содержит и числа (`total_chunks`, `loads_count`), и
    вложенные объекты (`loads`, `unknown_loads`) — печатать их в карточке нельзя, а
    важные числа (сколько чанков дошло до обучения) нужны инженеру.
    """
    stats = metrics(model).get("dataset_stats")
    if not isinstance(stats, Mapping):
        return []
    rows: list[dict[str, str]] = []
    for key, value in stats.items():
        if isinstance(value, (Mapping, list)) or value is None:
            continue
        rows.append({"label": str(key), "value": str(value)})
    return rows[:STATS_LIMIT]


def metrics_summary(model: Any) -> dict[str, Any]:
    """Сводка обучения: чем обучали, на каких данных и с какой памятью.

    Возвращает готовые строки «подпись → значение» и числа: интерфейс ничего не считает
    (правило `llm_tasks/31.4` §3), а тесты проверяют один и тот же расчёт.
    """
    data = metrics(model)
    raw_memory = data.get("memory_usage")
    memory: Mapping[str, Any] = raw_memory if isinstance(raw_memory, Mapping) else {}
    tests = testing_rows(model)
    accuracy = tests[-1]["accuracy"] if tests else ""
    rows: list[dict[str, str]] = []

    def add(label: str, value: Any) -> None:
        text = "" if value is None else str(value)
        if text.strip():
            rows.append({"label": label, "value": text})

    add("Эпох", data.get("epochs"))
    add("Размер батча", data.get("batch_size"))
    add("Скорость обучения", data.get("learning_rate"))
    add("Препроцессинг", data.get("preprocessing_converter"))
    add("Аугментация", "включена" if data.get("augmentation_enabled") else "")
    add("Потоковая загрузка", "включена" if data.get("use_streaming") else "")
    add("Датасет обучения", data.get("dataset_id"))
    add("Обучена на базе", data.get("original_model_id"))
    add("Память (RSS, МБ)", memory.get("rss_mb"))
    if tests:
        add("Проверок выполнено", len(tests))
    return {
        "trained": bool(data),
        "label": TRAINED_LABEL if data else NOT_TRAINED_LABEL,
        "rows": rows,
        "accuracy": accuracy,
        "accuracy_label": f"{float(accuracy):.4f}" if str(accuracy).strip() else "",
        "dataset_stats": dataset_stat_rows(model),
        "origin_label": RESULT_LABEL if data.get("original_model_id") else BASE_LABEL,
    }


def card(model: Any, *, file: Any = None, session: Any = None) -> dict[str, Any]:
    """Полная карточка модели: свойства, сигналы, слои, метрики и признак выбора.

    `file` — запись реестра файлов стенда для `file_id` модели (размер `.h5`); `session` —
    сессия испытаний: по её профилю видно, выбрана ли модель для прогона (`llm_tasks/59`).
    """
    data = _payload(model)
    file_row = _payload(file) if file is not None else {}
    profile = dict(getattr(session, "ml_profile", None) or {}) if session is not None else {}
    model_id = str(data.get("id") or "")
    rows = [{"label": label, "value": str(data.get(field) or "")} for field, label in SCALAR_FIELDS]
    rows.insert(1, {"label": "Имя", "value": model_name(model)})
    description = str(data.get("description") or "")
    if description:
        rows.append({"label": "Описание", "value": description})
    groups = signal_groups(model)
    summary = metrics_summary(model)
    return {
        "id": model_id,
        "name": model_name(model),
        "test_model": is_test_model(model),
        "trained": is_trained(model),
        "trained_label": summary["label"],
        "rows": rows,
        "signals": groups,
        "signal_count": len(groups),
        "classes": class_rows(model),
        "layers": layer_rows(model),
        "metrics": summary,
        "training": training_rows(model),
        "testing": testing_rows(model),
        "file": {
            "id": str(file_row.get("id") or data.get("file_id") or ""),
            "name": str(file_row.get("file_name") or ""),
            "size": int(file_row.get("size") or 0),
            "known": bool(file_row),
        },
        "chosen": bool(model_id) and str(profile.get("model_id") or "") == model_id,
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def row(model: Any) -> dict[str, Any]:
    """Компактная строка таблицы моделей (без метрик: они читаются карточкой по запросу)."""
    data = _payload(model)
    summary = metrics_summary(model)
    return {
        "id": str(data.get("id") or ""),
        "name": model_name(model),
        "architecture": str(data.get("architecture") or ""),
        "version": str(data.get("version") or ""),
        "phase_type": str(data.get("phase_type") or ""),
        "output_size": int(data.get("output_size") or 0),
        "description": str(data.get("description") or ""),
        "trained": bool(summary["trained"]),
        "trained_label": summary["label"],
        "accuracy": summary["accuracy_label"],
        "signal_count": len(signal_groups(model)),
        "test_model": is_test_model(model),
        "owner": "испытания" if is_test_model(model) else "боевая",
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


#: Признаки отбора в интерфейсе: (ключ запроса, подпись) — «конкретную» модель находят
#: по свойствам, а не пролистыванием сотни строк реестра.
FILTER_FLAGS: tuple[tuple[str, str], ...] = (
    ("only_trained", "только обученные"),
    ("only_test", "только `__TEST__`"),
    ("only_battle", "только боевые"),
)


def filter_options(models: Iterable[Any]) -> dict[str, list[dict[str, str]]]:
    """Варианты фильтров «Реальной тренировки»: архитектуры, фазы, размеры выхода.

    Строится по реестру, а не по каталогу архитектур: оператор отбирает из того, что
    на стенде **есть**, а не из того, что теоретически возможно.
    """
    rows = [row(model) for model in models]
    architectures = sorted({item["architecture"] for item in rows if item["architecture"]})
    phases = sorted({item["phase_type"] for item in rows if item["phase_type"]})
    sizes = sorted({str(item["output_size"]) for item in rows if item["output_size"]})
    return {
        "architectures": [{"value": value, "label": value} for value in architectures],
        "phases": [{"value": value, "label": value} for value in phases],
        "output_sizes": [{"value": value, "label": value} for value in sizes],
    }


def filter_models(
    models: Iterable[Any],
    *,
    query: str = "",
    architecture: str = "",
    phase_type: str = "",
    output_size: str = "",
    only_trained: bool = False,
    only_test: bool = False,
    only_battle: bool = False,
) -> list[Any]:
    """Отбор «конкретной» модели: поиск, архитектура, фаза, выход и признаки.

    Фильтр `device_id` у стенда падает (`TC-MOD-05`), поэтому отбор по сигналам идёт
    здесь: реестр читается целиком, а пульт фильтрует список сам.
    """
    needle = str(query or "").strip().lower()
    chosen: list[Any] = []
    for model in models:
        data = row(model)
        haystack = (
            f"{data['name']} {data['description']} {data['architecture']} "
            f"{data['version']} {data['id']}"
        ).lower()
        if needle and needle not in haystack:
            continue
        if architecture and data["architecture"] != architecture:
            continue
        if phase_type and data["phase_type"] != phase_type:
            continue
        if output_size and str(data["output_size"]) != str(output_size):
            continue
        if only_trained and not data["trained"]:
            continue
        if only_test and not data["test_model"]:
            continue
        if only_battle and data["test_model"]:
            continue
        chosen.append(model)

    def sort_key(model: Any) -> tuple[int, int, str]:
        """Сначала обученные и свои (`__TEST__`), затем — по имени: реже ошибаются."""
        data = row(model)
        return (0 if data["trained"] else 1, 0 if data["test_model"] else 1, data["name"].lower())

    chosen.sort(key=sort_key)
    return chosen


def chosen_model(models: Sequence[Any], profile: Mapping[str, Any] | None = None) -> Any:
    """Модель, выбранная в профиле прогона (None — не выбрана или её больше нет)."""
    model_id = str(dict(profile or {}).get("model_id") or "")
    if not model_id:
        return None
    return next(
        (model for model in models if str(_payload(model).get("id") or "") == model_id), None
    )


# ---------------------------------------------------------------------------
# Данные «Реальной тренировки» (Э2.1, `llm_tasks/64`): загруженные файлы и датасеты
# ---------------------------------------------------------------------------
#: Роли файлов данных стенда: измерения (`RAW`) и разметка (`LOADS`). Роль определяет,
#: в какой список попадёт файл при наполнении датасета (`POST /api/datasets/fill/{id}`).
FILE_ROLES: tuple[tuple[str, str], ...] = (
    ("RAW", "измерения (RAW)"),
    ("LOADS", "разметка (LOADS)"),
)

#: Шаги журнала К2, которые показывает экран (порядок — порядок работы оператора).
FLOW_STEPS: tuple[tuple[str, str], ...] = (
    ("files", "Загрузка файлов"),
    ("dataset", "Датасет создан"),
    ("fill", "Датасет наполнен"),
    ("validated", "Проверен вне пульта"),
)


def human_size(value: Any) -> str:
    """Размер файла для интерфейса: «2,44 ГБ» вместо «2621440000».

    Боевые RAW доходят до 2,18 ГБ, поэтому «байты в столбик» читать нельзя: оператор
    должен видеть порядок величины с одного взгляда.
    """
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return ""
    for unit, limit in (("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if number >= limit:
            return f"{number / limit:.2f} {unit}".replace(".", ",")
    return f"{int(number)} Б"


def role_label(value: Any) -> str:
    """Подпись роли файла: `RAW` → «измерения (RAW)», `LOADS` → «разметка (LOADS)»."""
    lookup = {str(key).upper(): label for key, label in FILE_ROLES}
    return lookup.get(str(value or "").upper(), str(value or ""))


def upload_rows(files: Iterable[Any]) -> list[dict[str, Any]]:
    """Паспорта загруженных файлов для таблицы: имя, роль, размер, `sha256`, `file_id`."""
    rows: list[dict[str, Any]] = []
    for item in files:
        data = _payload(item)
        size = data.get("size")
        digest = str(data.get("sha256") or "")
        role = data.get("role") or data.get("file_type")
        rows.append(
            {
                "name": str(data.get("name") or ""),
                "role": str(role or ""),
                "role_label": role_label(role),
                "size": "" if size is None else str(size),
                "size_text": human_size(size),
                "sha256": digest,
                "sha256_short": digest[:12],
                "file_id": str(data.get("file_id") or ""),
                "at": str(data.get("at") or ""),
                "author": str(data.get("author") or ""),
            }
        )
    return rows


def dataset_rows(datasets: Iterable[Any]) -> list[dict[str, Any]]:
    """Датасеты стенда для выбора: `id`, имя, тип, описание и дата создания."""
    rows: list[dict[str, Any]] = []
    for item in datasets:
        data = _payload(item)
        rows.append(
            {
                "id": str(data.get("id") or ""),
                "name": str(data.get("name") or ""),
                "type": str(data.get("type") or ""),
                "description": str(data.get("description") or ""),
                "created_at": str(data.get("created_at") or ""),
            }
        )
    return rows


def latest_steps(steps: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Последняя запись по каждому шагу К2: `ключ → {status, at, note, author}`.

    Журнал сессии хранит **все** записи (это история решений для отчёта), а экрану нужен
    текущий статус шага — поэтому берётся последняя запись, а не первая.
    """
    result: dict[str, dict[str, Any]] = {}
    for item in steps:
        data = _payload(item)
        key = str(data.get("step") or "")
        if not key:
            continue
        result[key] = {
            "status": str(data.get("status") or ""),
            "at": str(data.get("at") or ""),
            "note": str(data.get("note") or ""),
            "author": str(data.get("author") or ""),
        }
    return result


def manifest_summary(manifest: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Подписи паспорта датасета: когда собран, сколько файлов, общий размер, кто собрал."""
    data = dict(manifest or {})
    if not data:
        return []
    rows = [
        {"label": "Собран", "value": str(data.get("at") or "")},
        {"label": "Файлов", "value": str(data.get("file_count") or 0)},
        {"label": "Общий размер", "value": human_size(data.get("size_total"))},
        {"label": "Собрал", "value": str(data.get("author") or "")},
    ]
    note = str(data.get("note") or "")
    if note:
        rows.append({"label": "Примечание", "value": note})
    return rows
