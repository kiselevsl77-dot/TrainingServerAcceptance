"""Подбор данных стенда для ML-проверок: модель, датасет, сэмпл RAW (T6–T8).

Сценарии `TC-MOD`, `TC-TR`, `TC-INF` не готовят данные заново, а берут то, что уже
есть на стенде: модель и датасет из реестров, строку RAW-файла — как сэмпл для
`inference/single`. Здесь собраны **чистые** помощники подбора и разбора (без клиента,
журнала и сессии), поэтому они проверяются unit-тестами без стенда.

Почему подбор осторожный — факты живого стенда (`llm_tasks/48`, `llm_tasks/50`):

    * скачивание файла с не-ASCII именем падает на стенде (`404`, ошибка кодирования
      latin-1, дефект P0) — для любых проб с `download` выбирается файл **с ASCII-именем**
      (`is_ascii`, `ascii_named`);
    * raw_data CSV разделён `;`, а шесть Base64-сигналов — последние колонки
      `U_A_signal`…`I_C_signal`; параметры шкал (`U_mult`, `U_dev`, `I_mult`, `I_dev`,
      `byte_order`, `signal_bytes`) лежат в тех же строках и уходят в `inference/single`
      вместе с сигналами (`RawSample.request_fields`);
    * у моделей стенда `phase_type=single`, часть — с заполненными `metrics` (обученные):
      для инференса и проверки модели нужна обученная модель (`with_metrics=True`).

Отсутствие подходящих данных — не отказ проверки, а предусловие: сценарий поднимает
`PreconditionError` (статус «пропущена»), и оператор видит, что именно нужно на стенде.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from client.schemas import DatasetResponse, FileMetadataResponse, ModelMetadata

#: Разделитель колонок в raw_data CSV (проверено на стенде: `;`, а не `,`).
RAW_SEPARATOR = ";"

#: Шесть Base64-сигналов сэмпла — обязательные поля `inference/single`.
SAMPLE_SIGNAL_FIELDS: tuple[str, ...] = (
    "U_A_signal",
    "U_B_signal",
    "U_C_signal",
    "I_A_signal",
    "I_B_signal",
    "I_C_signal",
)

#: Параметры шкал из строки RAW: уходят в запрос, если разбираются.
SAMPLE_PARAM_FIELDS: tuple[str, ...] = (
    "U_mult",
    "U_dev",
    "I_mult",
    "I_dev",
    "byte_order",
    "signal_bytes",
)

#: Поля строки RAW, которые переносятся в доказательства (`RawSample.identity`).
IDENTITY_FIELDS: tuple[str, ...] = ("chunkID", "timestamp")


def is_ascii(value: str) -> bool:
    """True, если имя состоит только из ASCII-символов (по нему работает скачивание)."""
    try:
        str(value).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def ascii_named(files: Iterable[FileMetadataResponse]) -> list[FileMetadataResponse]:
    """Файлы реестра с ASCII-именами: только их можно скачать (дефект P0)."""
    return [file for file in files if is_ascii(file.file_name)]


def pick_raw_file(
    files: Sequence[FileMetadataResponse],
    *,
    file_id: str = "",
    smallest: bool = True,
) -> FileMetadataResponse | None:
    """Выбирает RAW-файл для сэмпла: заданный оператором либо самый маленький ASCII.

    Args:
        files: реестр файлов (`GET /api/data/files`).
        file_id: выбранный оператором файл; пусто — выбор автоматический.
        smallest: при автоматическом выборе брать самый маленький файл: сэмпл не
            требует всей записи, а `Range` сервер не поддерживает — файл качается целиком.
    """
    raws = [file for file in files if str(file.file_type).upper() == "RAW"]
    if file_id:
        chosen = next((file for file in raws if str(file.id) == str(file_id)), None)
        if chosen is not None:
            return chosen
    candidates = ascii_named(raws)
    if not candidates:
        return None
    if smallest:
        candidates = sorted(candidates, key=lambda file: int(file.size))
    return candidates[0]


def pick_model(
    models: Sequence[ModelMetadata],
    *,
    model_id: str = "",
    phase_type: str = "",
    with_metrics: bool = False,
) -> ModelMetadata | None:
    """Выбирает модель из реестра: заданную оператором либо самую свежую подходящую.

    Args:
        models: реестр моделей (`GET /api/ml_models/models`).
        model_id: выбранная оператором модель (пусто — выбор автоматический).
        phase_type: требуемая фаза (`single`/`three`); пусто — любая.
        with_metrics: требовать заполненные `metrics` — нужно для инференса и проверки
            модели: у необученной модели предсказывать нечего.
    """
    rows = list(models)
    if model_id:
        chosen = next((model for model in rows if str(model.id) == str(model_id)), None)
        if chosen is not None:
            return chosen
    suitable = [
        model
        for model in rows
        if (not phase_type or str(model.phase_type) == phase_type)
        and (not with_metrics or bool(model.metrics))
    ]
    if not suitable:
        return None
    suitable.sort(key=lambda model: model.updated_at, reverse=True)
    return suitable[0]


def pick_dataset(
    datasets: Sequence[DatasetResponse],
    *,
    dataset_id: str = "",
) -> DatasetResponse | None:
    """Выбирает датасет: заданный оператором либо первый в реестре."""
    rows = list(datasets)
    if dataset_id:
        chosen = next((item for item in rows if str(item.id) == str(dataset_id)), None)
        if chosen is not None:
            return chosen
    return rows[0] if rows else None


def model_label(model: ModelMetadata) -> str:
    """Подпись модели для интерфейса, доказательств и протокола."""
    return f"{model.name} · {model.architecture} · {model.version}"


def dataset_label(dataset: DatasetResponse, *, files_count: int | None = None) -> str:
    """Подпись датасета; `files_count` добавляет число файлов состава, если известно."""
    suffix = f" · файлов {files_count}" if files_count is not None else ""
    return f"{dataset.name}{suffix}"


@dataclass(frozen=True)
class RawSample:
    """Строка RAW-файла как сэмпл для `inference/single` (UC-25).

    Атрибуты:
        file_name: имя файла-источника (для доказательств).
        row: номер строки данных (1 — первая запись, заголовок не считается).
        signals: шесть Base64-сигналов (`U_A_signal`…`I_C_signal`).
        params: параметры шкал (`U_mult`, `U_dev`, `I_mult`, `I_dev`, `byte_order`,
            `signal_bytes`) — для `inference/single`.
        identity: `chunkID`/`timestamp` строки (доказательства: какой сэмпл взят).
        fields: все колонки строки (для отчёта о неожиданной структуре файла).
    """

    file_name: str
    row: int
    signals: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)

    def request_fields(self) -> dict[str, Any]:
        """Поля запроса `inference/single`: сигналы и параметры шкал."""
        return {**self.signals, **self.params}

    def as_evidence(self) -> dict[str, Any]:
        """Доказательства сэмпла без Base64 (иначе протокол распухает)."""
        return {
            "file_name": self.file_name,
            "row": self.row,
            **self.identity,
            "params": dict(self.params),
            "signal_lengths": {name: len(value) for name, value in self.signals.items()},
        }


def parse_raw_sample(text: str, *, row: int = 1, file_name: str = "") -> RawSample:
    """Разбирает строку raw_data CSV в сэмпл для `inference/single`.

    Args:
        text: содержимое RAW-файла (или его начало): CSV с разделителем `;`,
            первая непустая строка — заголовок.
        row: номер строки данных (1 — первая запись).
        file_name: имя файла-источника (для доказательств).

    Raises:
        ValueError: файл пуст, строки данных нет или в ней нет обязательных сигналов.
    """
    lines = [line for line in str(text).splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("в RAW-файле нет строк данных: сэмпл взять не из чего")

    header = [name.strip() for name in lines[0].split(RAW_SEPARATOR)]
    if row < 1 or row >= len(lines):
        raise ValueError(f"в RAW-файле нет строки данных №{row}: строк данных {len(lines) - 1}")

    values = [value.strip() for value in lines[row].split(RAW_SEPARATOR)]
    if len(values) != len(header):
        raise ValueError(
            f"строка данных №{row} не совпадает с заголовком: "
            f"колонок {len(values)} вместо {len(header)}"
        )
    fields = dict(zip(header, values, strict=True))

    signals = {name: fields.get(name, "") for name in SAMPLE_SIGNAL_FIELDS}
    missing = [name for name, value in signals.items() if not value]
    if missing:
        raise ValueError(f"в строке нет сигналов: {', '.join(missing)}")

    params: dict[str, Any] = {}
    for name in SAMPLE_PARAM_FIELDS:
        raw = fields.get(name, "")
        if not raw:
            continue
        params[name] = raw if name == "byte_order" else _number(raw)

    return RawSample(
        file_name=str(file_name),
        row=row,
        signals=signals,
        params=params,
        identity={name: fields.get(name, "") for name in IDENTITY_FIELDS},
        fields=fields,
    )


def _number(raw: str) -> Any:
    """Приводит значение колонки RAW к числу (или оставляет строкой, если не число)."""
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw
