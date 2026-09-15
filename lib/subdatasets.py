"""Формирование субдатасетов (пар RAW+markup) из реестра файлов (FR-2, замечание №1).

В API сервера обучения субдатасетов нет: `GET /api/data/files` возвращает плоский
список файлов, RAW и markup не связаны между собой, а повторные загрузки попадают
в хранилище как отдельные файлы с тем же именем (различаются `size` и `import_date`).
Поэтому пары строит UI:

    1) файл классифицируется по имени (маркер `.raw`/`.markup` перед расширением);
    2) файлы группируются по базовому имени (`Antminer_S19.raw.csv` + `.markup.csv`);
    3) RAW и markup сопоставляются **по времени импорта**: для каждого RAW берётся
       свободный markup с минимальной неотрицательной разницей `import_date`
       (разметка загружается сразу после осциллограммы); если такого нет — ближайший
       по модулю разницы с пометкой «сопоставление по времени не подтверждено»;
    4) RAW без разметки и разметка без RAW попадают в блоки «непарные»;
    5) файлы без маркеров (модели `.h5`, отчёты `.zip`) — «прочие файлы».

Факт клиентской группировки выводится оператору как замечание к API.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from client.schemas import FileMetadataResponse

#: Порог подтверждения пары по времени (RAW → markup), секунды.
PAIR_WINDOW_SECONDS = 3600.0

_FILE_KIND_RE = re.compile(r"\.(?P<kind>raw|markup)(?=\.|$)", re.IGNORECASE)

FILE_TYPE_LABELS: dict[str, str] = {
    "RAW": "RAW · сигналы",
    "LOADS": "LOADS · разметка",
    "ONNX": "ONNX · модель",
    "H5": "H5 · модель",
    "REPORT_ZIP": "REPORT_ZIP · отчёт",
}

_SIZE_UNITS = ("Б", "КБ", "МБ", "ГБ", "ТБ")


class FileKind(StrEnum):
    """Вид файла данных, определённый по имени."""

    RAW = "raw"
    MARKUP = "markup"
    OTHER = "other"


@dataclass(frozen=True)
class SubdatasetPair:
    """Сопоставленная пара RAW+markup (одна «версия» субдатасета)."""

    raw: FileMetadataResponse
    markup: FileMetadataResponse | None
    delta_seconds: float | None
    confident: bool

    @property
    def size_bytes(self) -> int:
        """Суммарный размер файлов пары."""
        return self.raw.size + (self.markup.size if self.markup is not None else 0)

    @property
    def delta_note(self) -> str:
        """Человекочитаемая разница времени импорта RAW → markup."""
        if self.delta_seconds is None:
            return "разметка не найдена"
        sign = "позже" if self.delta_seconds >= 0 else "раньше"
        return f"markup {sign} на {format_duration(self.delta_seconds)}"


@dataclass(frozen=True)
class Subdataset:
    """Субдатасет = все файлы одной базы имени (пары + непарные)."""

    name: str
    pairs: tuple[SubdatasetPair, ...]
    unpaired_raw: tuple[FileMetadataResponse, ...]
    unpaired_markup: tuple[FileMetadataResponse, ...]
    warnings: tuple[str, ...]

    @property
    def all_files(self) -> tuple[FileMetadataResponse, ...]:
        """Все файлы субдатасета (пары + непарные)."""
        files: list[FileMetadataResponse] = []
        for pair in self.pairs:
            files.append(pair.raw)
            if pair.markup is not None:
                files.append(pair.markup)
        files.extend(self.unpaired_raw)
        files.extend(self.unpaired_markup)
        return tuple(files)

    @property
    def file_ids(self) -> tuple[UUID, ...]:
        """Идентификаторы всех файлов субдатасета (для скачивания/удаления)."""
        return tuple(file.id for file in self.all_files)

    @property
    def size_bytes(self) -> int:
        """Суммарный размер файлов субдатасета."""
        return sum(file.size for file in self.all_files)

    @property
    def is_complete(self) -> bool:
        """True, если все RAW имеют разметку и лишних файлов нет."""
        return bool(self.pairs) and not self.unpaired_raw and not self.unpaired_markup

    @property
    def is_duplicated(self) -> bool:
        """True, если у базы несколько пар (признак повторной загрузки)."""
        return len(self.pairs) > 1

    @property
    def has_issues(self) -> bool:
        """True, если субдатасет требует внимания оператора."""
        return bool(self.warnings)


@dataclass(frozen=True)
class SubdatasetOverview:
    """Результат группировки реестра файлов."""

    subdatasets: tuple[Subdataset, ...]
    other_files: tuple[FileMetadataResponse, ...]

    @property
    def pairs(self) -> int:
        """Общее число сопоставленных пар (версий) субдатасетов."""
        return sum(len(subdataset.pairs) for subdataset in self.subdatasets)

    @property
    def unpaired(self) -> int:
        """Общее число непарных файлов."""
        return sum(
            len(subdataset.unpaired_raw) + len(subdataset.unpaired_markup)
            for subdataset in self.subdatasets
        )

    @property
    def with_issues(self) -> int:
        """Число субдатасетов с замечаниями."""
        return sum(1 for subdataset in self.subdatasets if subdataset.has_issues)


@dataclass(frozen=True)
class FileStats:
    """Сводные показатели по реестру файлов (KPI экрана)."""

    total: int
    raw: int
    markup: int
    other: int
    subdatasets: int
    pairs: int
    with_issues: int
    unpaired: int


def classify(file_name: str) -> tuple[FileKind, str]:
    """Определяет вид файла и базовое имя субдатасета по имени файла."""
    match = _FILE_KIND_RE.search(file_name)
    if match is None:
        return FileKind.OTHER, file_name

    return FileKind(match.group("kind").lower()), file_name[: match.start()]


def pair_files(
    raw_files: Sequence[FileMetadataResponse],
    markup_files: Sequence[FileMetadataResponse],
    *,
    window_seconds: float = PAIR_WINDOW_SECONDS,
) -> tuple[list[SubdatasetPair], list[FileMetadataResponse], list[FileMetadataResponse]]:
    """Сопоставляет RAW и markup одной базы по времени импорта.

    Returns:
        Кортеж (пары, непарные RAW, непарные markup).
    """
    available = sorted(markup_files, key=lambda file: file.import_date)
    pairs: list[SubdatasetPair] = []
    unpaired_raw: list[FileMetadataResponse] = []

    for raw in sorted(raw_files, key=lambda file: file.import_date):
        if not available:
            unpaired_raw.append(raw)
            continue

        later = [markup for markup in available if markup.import_date >= raw.import_date]
        if later:
            chosen = min(later, key=lambda markup: markup.import_date - raw.import_date)
        else:
            chosen = min(available, key=lambda markup: abs(raw.import_date - markup.import_date))

        available.remove(chosen)
        delta = (chosen.import_date - raw.import_date).total_seconds()
        pairs.append(
            SubdatasetPair(
                raw=raw,
                markup=chosen,
                delta_seconds=delta,
                confident=abs(delta) <= window_seconds,
            )
        )

    return pairs, unpaired_raw, available


def build_subdatasets(
    files: Iterable[FileMetadataResponse],
    *,
    window_seconds: float = PAIR_WINDOW_SECONDS,
) -> SubdatasetOverview:
    """Строит субдатасеты (пары RAW+markup) из плоского реестра файлов."""
    raw_by_base: dict[str, list[FileMetadataResponse]] = defaultdict(list)
    markup_by_base: dict[str, list[FileMetadataResponse]] = defaultdict(list)
    other_files: list[FileMetadataResponse] = []

    for file in files:
        kind, base = classify(file.file_name)
        if kind is FileKind.RAW:
            raw_by_base[base].append(file)
        elif kind is FileKind.MARKUP:
            markup_by_base[base].append(file)
        else:
            other_files.append(file)

    subdatasets: list[Subdataset] = []
    for base in sorted({*raw_by_base, *markup_by_base}, key=str.lower):
        matched, unpaired_raw, unpaired_markup = pair_files(
            raw_by_base.get(base, []),
            markup_by_base.get(base, []),
            window_seconds=window_seconds,
        )
        pairs = tuple(sorted(matched, key=lambda pair: pair.raw.import_date, reverse=True))
        subdatasets.append(
            Subdataset(
                name=base,
                pairs=pairs,
                unpaired_raw=tuple(unpaired_raw),
                unpaired_markup=tuple(unpaired_markup),
                warnings=_build_warnings(pairs, unpaired_raw, unpaired_markup, window_seconds),
            )
        )

    other_files.sort(key=lambda file: file.file_name.lower())
    return SubdatasetOverview(subdatasets=tuple(subdatasets), other_files=tuple(other_files))


def compute_stats(
    files: Iterable[FileMetadataResponse],
    overview: SubdatasetOverview | None = None,
) -> FileStats:
    """Считает сводные показатели (при наличии переиспользует готовый overview)."""
    file_list = list(files)
    resolved = overview if overview is not None else build_subdatasets(file_list)
    kinds = Counter(classify(file.file_name)[0] for file in file_list)

    return FileStats(
        total=len(file_list),
        raw=kinds[FileKind.RAW],
        markup=kinds[FileKind.MARKUP],
        other=kinds[FileKind.OTHER],
        subdatasets=len(resolved.subdatasets),
        pairs=resolved.pairs,
        with_issues=resolved.with_issues,
        unpaired=resolved.unpaired,
    )


def file_type_label(file_type: str) -> str:
    """Человекочитаемая подпись типа файла (включая значения вне enum)."""
    return FILE_TYPE_LABELS.get(file_type.upper(), f"{file_type} · прочее")


def format_size(num_bytes: int) -> str:
    """Форматирует размер в байтах (Б/КБ/МБ/ГБ/ТБ)."""
    value = float(max(0, num_bytes))
    unit = _SIZE_UNITS[0]

    for candidate in _SIZE_UNITS:
        unit = candidate
        if value < 1024 or candidate == _SIZE_UNITS[-1]:
            break
        value /= 1024

    if unit == _SIZE_UNITS[0] or value >= 10:
        return f"{value:.0f} {unit}"
    return f"{value:.1f} {unit}"


def format_duration(seconds: float) -> str:
    """Форматирует интервал в секундах (с/мин/ч/дн)."""
    value = abs(seconds)
    if value < 60:
        return f"{value:.2f} с"

    minutes, rest = divmod(int(value), 60)
    if minutes < 60:
        return f"{minutes} мин {rest} с"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин"

    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч"


def _build_warnings(
    pairs: Sequence[SubdatasetPair],
    unpaired_raw: Sequence[FileMetadataResponse],
    unpaired_markup: Sequence[FileMetadataResponse],
    window_seconds: float,
) -> tuple[str, ...]:
    """Формулирует замечания по субдатасету для оператора."""
    warnings: list[str] = []

    if len(pairs) > 1:
        warnings.append(
            f"повторные загрузки: {len(pairs)} пар RAW+markup с одинаковым базовым именем"
        )

    weak = [pair for pair in pairs if not pair.confident]
    if weak:
        warnings.append(
            f"сопоставление по времени не подтверждено (Δ > {format_duration(window_seconds)}): "
            f"{len(weak)} пар"
        )

    if unpaired_raw:
        warnings.append(f"без разметки: {len(unpaired_raw)} RAW-файл(ов)")
    if unpaired_markup:
        warnings.append(f"разметка без RAW: {len(unpaired_markup)} файл(ов)")

    return tuple(warnings)
