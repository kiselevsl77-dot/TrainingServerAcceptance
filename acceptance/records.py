"""Записи (RAW + markup): объединение, дубли, несопоставленные файлы (FR-T2, этап T1).

Что делает модуль (и почему он нужен):

    * в API сервера обучения **записи нет**: `GET /api/data/files` возвращает плоский
      реестр, RAW и markup одной записи не связаны, а повторные загрузки приходят
      файлами с тем же именем (различаются `id`/`size`/`import_date`/`s3_path`);
    * «запись» = пара файлов, поставляемых вместе: `<имя>.raw.csv` + `<имя>.markup.csv`;
      это единица работы оператора (термин заказчика);
    * «дубль» — файл с тем же `file_name`, что уже есть в хранилище (другая версия
      записи); понятие «субдатасет» в пульте **не используется** — группировка записей
      не выполняется (решение заказчика 15.09.2026; субдатасет остаётся перспективным
      требованием к API, см. `notes.PROSPECTIVE_REQUIREMENTS`).

Правила объединения (все — прозрачные и проверяемые):
    1) файлы классифицируются по имени (`lib.subdatasets.classify`);
    2) внутри базы имени RAW и markup сопоставляются **по времени импорта**
       (`lib.subdatasets.pair_files`, окно Δ ≤ 1 ч → «подтверждено», иначе «не подтверждено»);
    3) ручные решения оператора (`acceptance.overrides`) имеют приоритет над эвристикой
       и помечаются как «вручную»;
    4) RAW без разметки и разметка без RAW не скрываются — отдельные разделы;
    5) файлы без маркеров `.raw`/`.markup` (модели `.h5`, отчёты `.zip`) — «прочие»;
    6) результат выгружается **манифестом** (CSV/JSON), куда попадают **все** файлы
       реестра — так ни один файл не «теряется» (требование прозрачности).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from acceptance.overrides import (
    ACTION_CURRENT,
    ACTION_EXCLUDE,
    ACTION_LINK,
    ACTION_UNLINK,
    Overrides,
    RecordOverride,
)
from acceptance.paths import ARTIFACT_DIR
from acceptance.session import now_iso
from client.schemas import FileMetadataResponse
from lib.subdatasets import (
    PAIR_WINDOW_SECONDS,
    FileKind,
    classify,
    format_duration,
    pair_files,
)

#: Человекочитаемые названия правил объединения.
RULE_NAME_TIME = "имя + время импорта"
RULE_NAME_ONLY = "имя (разметка не найдена)"
RULE_MANUAL = "решение оператора"

#: Уверенность объединения.
CONFIDENCE_CONFIRMED = "подтверждено"
CONFIDENCE_WEAK = "не подтверждено"
CONFIDENCE_MANUAL = "вручную"
CONFIDENCE_NONE = "нет разметки"

#: Статусы разбора разметки (записи/чанки считаются клиентом — серверных агрегатов нет).
STATS_NOT_CALCULATED = "не считалось"
STATS_CALCULATED = "рассчитано (клиентская оценка)"
STATS_BLOCKED = "заблокировано API"

#: Виды строк манифеста (манифест содержит все файлы реестра).
#: RAW без разметки — это тоже запись, поэтому отдельного вида строки у него нет:
#: он выводится как «запись» с пустыми полями markup и попадает в раздел
#: «Несопоставленные» экрана.
ROW_RECORD = "запись"
ROW_UNPAIRED_MARKUP = "разметка без RAW"
ROW_OTHER = "прочий файл"

#: Колонки манифеста объединения (обязательное приложение к отчёту об испытаниях).
MANIFEST_COLUMNS: tuple[str, ...] = (
    "row_kind",
    "record_id",
    "base_name",
    "version",
    "group_size",
    "is_current",
    "duplicate",
    "raw_id",
    "raw_name",
    "raw_size",
    "raw_import_date",
    "raw_s3_path",
    "markup_id",
    "markup_name",
    "markup_size",
    "markup_import_date",
    "markup_s3_path",
    "delta_seconds",
    "rule",
    "confidence",
    "manual",
    "excluded",
    "records",
    "chunks",
    "markup_stats_status",
    "comment",
)


@dataclass(frozen=True)
class MarkupStatsEntry:
    """Результат разбора markup-файла (записи и чанки) для одной записи."""

    records: int | None = None
    chunks: int | None = None
    status: str = STATS_NOT_CALCULATED
    note: str = ""
    at: str = ""

    @property
    def is_calculated(self) -> bool:
        """True, если характеристики рассчитаны."""
        return self.records is not None and self.chunks is not None

    def to_dict(self) -> dict[str, Any]:
        """Сериализует результат (хранится в сессии испытаний)."""
        return {
            "records": self.records,
            "chunks": self.chunks,
            "status": self.status,
            "note": self.note,
            "at": self.at or now_iso(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> MarkupStatsEntry:
        """Восстанавливает результат из словаря сессии."""
        if not data:
            return cls()
        return cls(
            records=data.get("records"),
            chunks=data.get("chunks"),
            status=str(data.get("status", STATS_NOT_CALCULATED)),
            note=str(data.get("note", "")),
            at=str(data.get("at", "")),
        )


def record_id_for(base_name: str, raw_id: Any) -> str:
    """Стабильный идентификатор записи (не меняется при перепривязке разметки)."""
    digest = hashlib.sha1(f"{base_name}|{raw_id}".encode()).hexdigest()
    return digest[:12]


@dataclass(frozen=True)
class Record:
    """Запись: RAW-файл и (при наличии) сопоставленный ему markup."""

    record_id: str
    base_name: str
    raw: FileMetadataResponse
    markup: FileMetadataResponse | None
    delta_seconds: float | None
    rule: str
    confidence: str
    version: int
    group_size: int
    is_current: bool
    candidates: tuple[FileMetadataResponse, ...] = ()
    manual: bool = False
    excluded: bool = False
    comment: str = ""
    stats: MarkupStatsEntry = field(default_factory=MarkupStatsEntry)

    @property
    def duplicate(self) -> bool:
        """True, если у базы имени несколько версий записи (повторные загрузки)."""
        return self.group_size > 1

    @property
    def has_markup(self) -> bool:
        """True, если разметка привязана."""
        return self.markup is not None

    @property
    def size_bytes(self) -> int:
        """Суммарный размер файлов записи."""
        return self.raw.size + (self.markup.size if self.markup is not None else 0)

    @property
    def delta_note(self) -> str:
        """Разница времени импорта RAW → markup (человекочитаемо)."""
        if self.delta_seconds is None:
            return "разметка не найдена"
        sign = "позже" if self.delta_seconds >= 0 else "раньше"
        return f"markup {sign} на {format_duration(self.delta_seconds)}"

    @property
    def confidence_icon(self) -> str:
        """Индикатор уверенности объединения."""
        icons = {
            CONFIDENCE_CONFIRMED: "✅",
            CONFIDENCE_WEAK: "⚠️",
            CONFIDENCE_MANUAL: "🖐️",
            CONFIDENCE_NONE: "❓",
        }
        return icons.get(self.confidence, "❓")

    @property
    def needs_attention(self) -> bool:
        """True, если запись требует внимания оператора (дубль/нет разметки/слабая пара)."""
        return bool(
            self.duplicate
            or not self.has_markup
            or self.confidence != CONFIDENCE_CONFIRMED
            or self.excluded
        )

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        """Идентификаторы markup-кандидатов (все markup-файлы той же базы имени)."""
        return tuple(str(candidate.id) for candidate in self.candidates)

    def with_stats(self, stats: MarkupStatsEntry) -> Record:
        """Копия записи с заполненными характеристиками разметки."""
        return replace(self, stats=stats)


@dataclass(frozen=True)
class RecordsStats:
    """Сводные показатели объединения реестра файлов в записи (KPI экрана)."""

    files: int
    records: int
    confirmed: int
    manual: int
    weak: int
    without_markup: int
    duplicates: int
    duplicate_bases: int
    unpaired_markup: int
    other: int
    excluded: int
    raw_bytes: int
    markup_bytes: int

    @property
    def paired(self) -> int:
        """Число записей с привязанной разметкой."""
        return self.records - self.without_markup

    @property
    def attached_markup(self) -> int:
        """Число markup-файлов, вошедших в записи (по одному на запись с разметкой)."""
        return self.paired

    @property
    def coverage(self) -> float:
        """Доля записей с разметкой, % (0 при пустом реестре)."""
        if not self.records:
            return 0.0
        return round(100.0 * self.paired / self.records, 1)

    @property
    def unpaired(self) -> int:
        """Всего несопоставленных файлов (записи без разметки + markup без записи)."""
        return self.without_markup + self.unpaired_markup

    def to_dict(self) -> dict[str, Any]:
        """Сериализует показатели (в снимок сессии, манифест, отчёт)."""
        return {
            "files": self.files,
            "records": self.records,
            "confirmed": self.confirmed,
            "manual": self.manual,
            "weak": self.weak,
            "without_markup": self.without_markup,
            "attached_markup": self.attached_markup,
            "duplicates": self.duplicates,
            "duplicate_bases": self.duplicate_bases,
            "unpaired_markup": self.unpaired_markup,
            "other": self.other,
            "excluded": self.excluded,
            "coverage_percent": self.coverage,
            "raw_bytes": self.raw_bytes,
            "markup_bytes": self.markup_bytes,
            "total_bytes": self.raw_bytes + self.markup_bytes,
        }


@dataclass(frozen=True)
class RecordsOverview:
    """Результат объединения реестра файлов в записи (вход экрана «Записи»)."""

    records: tuple[Record, ...]
    unpaired_markup: tuple[FileMetadataResponse, ...]
    other_files: tuple[FileMetadataResponse, ...]
    stats: RecordsStats
    window_seconds: float = PAIR_WINDOW_SECONDS

    @property
    def unpaired_raw(self) -> tuple[FileMetadataResponse, ...]:
        """RAW-файлы, для которых разметка не найдена (записи без разметки)."""
        return tuple(record.raw for record in self.records if not record.has_markup)

    @property
    def unpaired_files(self) -> int:
        """Всего несопоставленных файлов (без разметки + markup без RAW)."""
        return len(self.unpaired_raw) + len(self.unpaired_markup)

    # -- выборки -------------------------------------------------------------
    def find(self, record_id: str) -> Record | None:
        """Находит запись по идентификатору."""
        for record in self.records:
            if record.record_id == record_id:
                return record
        return None

    def for_base(self, base_name: str) -> tuple[Record, ...]:
        """Все версии записи с указанной базой имени (дубли)."""
        return tuple(record for record in self.records if record.base_name == base_name)

    @property
    def duplicate_bases(self) -> tuple[str, ...]:
        """Базы имени, у которых несколько версий записи (дубли)."""
        counted: Counter[str] = Counter(record.base_name for record in self.records)
        bases: list[str] = []
        for record in self.records:
            if counted[record.base_name] > 1 and record.base_name not in bases:
                bases.append(record.base_name)
        return tuple(bases)

    @property
    def requires_attention(self) -> tuple[Record, ...]:
        """Записи, требующие внимания оператора (дубли/нет разметки/слабая пара/исключены)."""
        return tuple(record for record in self.records if record.needs_attention)

    def stats_rows(self) -> list[dict[str, Any]]:
        """Показатели объединения в виде строк «метрика — значение» (для отчёта)."""
        stats = self.stats
        return [
            {"metric": "Файлов в реестре", "value": stats.files},
            {"metric": "Записей (RAW + markup)", "value": stats.records},
            {"metric": "Записей с разметкой", "value": stats.paired},
            {"metric": "Покрытие разметкой, %", "value": stats.coverage},
            {"metric": "Объединение подтверждено временем", "value": stats.confirmed},
            {"metric": "Объединение вручную (оператор)", "value": stats.manual},
            {"metric": "Слабое объединение (Δ > окна)", "value": stats.weak},
            {"metric": "Записей без разметки", "value": stats.without_markup},
            {"metric": "Записей-дублей", "value": stats.duplicates},
            {"metric": "Баз имени с дублями", "value": stats.duplicate_bases},
            {"metric": "Записей без разметки (RAW без markup)", "value": stats.without_markup},
            {"metric": "Разметка без RAW", "value": stats.unpaired_markup},
            {"metric": "Прочих файлов (модели, отчёты)", "value": stats.other},
            {"metric": "Исключено оператором", "value": stats.excluded},
            {"metric": "Объём RAW, байт", "value": stats.raw_bytes},
            {"metric": "Объём markup, байт", "value": stats.markup_bytes},
            {"metric": "Окно сопоставления по времени, с", "value": self.window_seconds},
        ]

    # -- манифест ------------------------------------------------------------
    def manifest_rows(self) -> list[dict[str, Any]]:
        """Строки манифеста: записи (RAW + его markup) + разметка без RAW + прочие файлы.

        Гарантия полноты: каждый файл реестра встречается в манифесте ровно один раз —
        как `raw_id` записи, как `markup_id` её разметки, либо отдельной строкой
        (см. `manifest_is_complete`). Ни один файл не «теряется».
        """
        rows: list[dict[str, Any]] = []

        for record in self.records:
            row = _empty_manifest_row()
            row.update(
                {
                    "row_kind": ROW_RECORD,
                    "record_id": record.record_id,
                    "base_name": record.base_name,
                    "version": record.version,
                    "group_size": record.group_size,
                    "is_current": _yes_no(record.is_current),
                    "duplicate": _yes_no(record.duplicate),
                    "rule": record.rule,
                    "confidence": record.confidence,
                    "manual": _yes_no(record.manual),
                    "excluded": _yes_no(record.excluded),
                    "records": record.stats.records if record.stats.records is not None else "",
                    "chunks": record.stats.chunks if record.stats.chunks is not None else "",
                    "markup_stats_status": record.stats.status,
                    "comment": record.comment,
                }
            )
            row.update(_file_fields("raw", record.raw))
            row.update(_file_fields("markup", record.markup))
            if record.delta_seconds is not None:
                row["delta_seconds"] = round(record.delta_seconds, 3)
            rows.append(row)

        for file in self.unpaired_markup:
            row = _empty_manifest_row()
            row.update({"row_kind": ROW_UNPAIRED_MARKUP, "excluded": "нет", "duplicate": "нет"})
            row.update(_file_fields("markup", file))
            rows.append(row)

        for file in self.other_files:
            row = _empty_manifest_row()
            row.update({"row_kind": ROW_OTHER, "excluded": "нет", "duplicate": "нет"})
            row.update(_file_fields("raw", file))
            rows.append(row)

        return rows

    def manifest_file_ids(self) -> list[str]:
        """Идентификаторы файлов, попавших в манифест (в порядке строк).

        Используется для контроля полноты: ни один файл реестра не должен
        пропасть или попасть в манифест дважды.
        """
        ids: list[str] = []
        for row in self.manifest_rows():
            for key in ("raw_id", "markup_id"):
                value = row.get(key)
                if value:
                    ids.append(str(value))
        return ids

    def manifest_is_complete(self) -> bool:
        """True, если каждый файл реестра попал в манифест ровно один раз."""
        ids = self.manifest_file_ids()
        return len(ids) == len(set(ids)) == self.stats.files

    def manifest_csv(self) -> str:
        """Манифест в CSV (для Excel сохранять файл в кодировке `utf-8-sig`)."""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(MANIFEST_COLUMNS))
        writer.writeheader()
        for row in self.manifest_rows():
            writer.writerow(row)
        return buffer.getvalue()

    def manifest_json(self, meta: Mapping[str, Any] | None = None) -> str:
        """Манифест в JSON: шапка (кто/когда/на каком стенде) + строки + показатели."""
        payload = {
            "manifest": "Записи (RAW + markup) — объявление клиента",
            "generated_at": now_iso(),
            "window_seconds": self.window_seconds,
            "meta": dict(meta or {}),
            "stats": self.stats.to_dict(),
            "rows": self.manifest_rows(),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Сборка записей из плоского реестра
# ---------------------------------------------------------------------------
def build_records(
    files: Iterable[FileMetadataResponse],
    *,
    overrides: Overrides | Sequence[RecordOverride] | None = None,
    window_seconds: float = PAIR_WINDOW_SECONDS,
    markup_stats: Mapping[str, MarkupStatsEntry | Mapping[str, Any]] | None = None,
) -> RecordsOverview:
    """Объединяет реестр файлов в записи (RAW + markup), применяя решения оператора.

    Args:
        files: реестр файлов с сервера (`GET /api/data/files`).
        overrides: ручные решения оператора (приоритет над эвристикой по времени).
        window_seconds: окно подтверждения пары по времени импорта.
        markup_stats: рассчитанные характеристики разметки по `id` markup-файла
            (хранятся в сессии испытаний, переживают перерисовку экрана).

    Returns:
        `RecordsOverview`: записи, разметка без RAW, прочие файлы, показатели, манифест.
    """
    file_list = list(files)
    raw_by_base: dict[str, list[FileMetadataResponse]] = defaultdict(list)
    markup_by_base: dict[str, list[FileMetadataResponse]] = defaultdict(list)
    other_files: list[FileMetadataResponse] = []

    for file in file_list:
        kind, base = classify(file.file_name)
        if kind is FileKind.RAW:
            raw_by_base[base].append(file)
        elif kind is FileKind.MARKUP:
            markup_by_base[base].append(file)
        else:
            other_files.append(file)

    resolved_overrides = _as_overrides(overrides)
    stats_by_file = _as_stats(markup_stats)
    # решения применяются в хронологическом порядке: последнее решение каждого вида
    # (привязка/снятие, актуальность, исключение) определяет итог
    manual_by_base: dict[str, list[RecordOverride]] = defaultdict(list)
    for entry in sorted(resolved_overrides.entries, key=lambda item: item.at):
        manual_by_base[entry.base_name].append(entry)

    records: list[Record] = []
    unpaired_markup: list[FileMetadataResponse] = []

    for base in sorted({*raw_by_base, *markup_by_base}, key=str.lower):
        raws = raw_by_base.get(base, [])
        markups = markup_by_base.get(base, [])

        if not raws:
            unpaired_markup.extend(markups)
            continue

        pairs, _unpaired_raw, _leftover = pair_files(raws, markups, window_seconds=window_seconds)
        by_id = {str(markup.id): markup for markup in markups}
        confident_by_raw: dict[str, bool] = {str(pair.raw.id): pair.confident for pair in pairs}

        # эвристика (пары по времени) — база для ручных решений оператора
        effective: dict[str, FileMetadataResponse | None] = {str(raw.id): None for raw in raws}
        for pair in pairs:
            effective[str(pair.raw.id)] = pair.markup

        manual_flags: dict[str, bool] = {}
        excluded_ids: set[str] = set()
        comments: dict[str, str] = {}
        forced_current: str | None = None

        # ручные решения применяются по времени: последнее решение выигрывает,
        # а разметка всегда принадлежит ровно одной записи
        for entry in manual_by_base.get(base, []):
            raw_id = entry.raw_id
            if raw_id not in effective:
                continue
            if entry.comment:
                comments[raw_id] = entry.comment

            if entry.action == ACTION_LINK:
                chosen = by_id.get(str(entry.markup_id or ""))
                if chosen is None:
                    continue
                for other_raw_id, other_markup in effective.items():
                    if other_markup is not None and str(other_markup.id) == str(chosen.id):
                        effective[other_raw_id] = None
                        manual_flags.pop(other_raw_id, None)
                effective[raw_id] = chosen
                manual_flags[raw_id] = True
            elif entry.action == ACTION_UNLINK:
                effective[raw_id] = None
                manual_flags[raw_id] = True
            elif entry.action == ACTION_EXCLUDE:
                excluded_ids.add(raw_id)
            elif entry.action == ACTION_CURRENT:
                forced_current = raw_id

        used_ids = {str(markup.id) for markup in effective.values() if markup is not None}
        unpaired_markup.extend(markup for markup in markups if str(markup.id) not in used_ids)

        ordered = sorted(raws, key=lambda file: file.import_date)
        version_of = {str(file.id): index for index, file in enumerate(ordered, start=1)}
        group_size = len(ordered)
        current_raw_id = forced_current if forced_current in version_of else str(ordered[-1].id)

        for raw in ordered:
            raw_id = str(raw.id)
            markup = effective.get(raw_id)
            is_manual = manual_flags.get(raw_id, False)
            candidates = tuple(
                sorted(
                    (item for item in markups if markup is None or str(item.id) != str(markup.id)),
                    key=lambda item: (
                        abs((item.import_date - raw.import_date).total_seconds()),
                        item.file_name.lower(),
                    ),
                )
            )
            records.append(
                Record(
                    record_id=record_id_for(base, raw.id),
                    base_name=base,
                    raw=raw,
                    markup=markup,
                    delta_seconds=(
                        (markup.import_date - raw.import_date).total_seconds()
                        if markup is not None
                        else None
                    ),
                    rule=_rule(is_manual, markup),
                    confidence=_confidence(is_manual, markup, confident_by_raw.get(raw_id, False)),
                    version=version_of[raw_id],
                    group_size=group_size,
                    is_current=raw_id == current_raw_id,
                    candidates=candidates,
                    manual=is_manual,
                    excluded=raw_id in excluded_ids,
                    comment=comments.get(raw_id, ""),
                    stats=(
                        stats_by_file.get(str(markup.id), MarkupStatsEntry())
                        if markup is not None
                        else MarkupStatsEntry()
                    ),
                )
            )

    records.sort(key=lambda record: (record.base_name.lower(), record.version))
    unpaired_markup.sort(key=lambda file: (file.import_date, file.file_name.lower()))
    other_files.sort(key=lambda file: file.file_name.lower())

    return RecordsOverview(
        records=tuple(records),
        unpaired_markup=tuple(unpaired_markup),
        other_files=tuple(other_files),
        stats=_build_stats(file_list, records, unpaired_markup, other_files),
        window_seconds=window_seconds,
    )


def save_manifest(
    overview: RecordsOverview,
    directory: Path | None = None,
    *,
    stem: str = "records_manifest",
    meta: Mapping[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Сохраняет манифест в `artifacts/` (CSV в `utf-8-sig` для Excel + JSON)."""
    base = Path(directory) if directory is not None else ARTIFACT_DIR
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = base / f"{stem}_{stamp}.csv"
    json_path = base / f"{stem}_{stamp}.json"
    csv_path.write_text(overview.manifest_csv(), encoding="utf-8-sig")
    json_path.write_text(overview.manifest_json(meta), encoding="utf-8")
    return csv_path, json_path


# ---------------------------------------------------------------------------
# Служебные помощники
# ---------------------------------------------------------------------------
def _rule(manual: bool, markup: FileMetadataResponse | None) -> str:
    """Правило объединения для отображения оператору."""
    if manual:
        return RULE_MANUAL
    return RULE_NAME_TIME if markup is not None else RULE_NAME_ONLY


def _confidence(manual: bool, markup: FileMetadataResponse | None, confident: bool) -> str:
    """Уверенность объединения (эвристика подтверждается окном по времени)."""
    if manual:
        return CONFIDENCE_MANUAL
    if markup is None:
        return CONFIDENCE_NONE
    return CONFIDENCE_CONFIRMED if confident else CONFIDENCE_WEAK


def _yes_no(value: bool) -> str:
    """«да»/«нет» для манифеста (файл читается в Excel)."""
    return "да" if value else "нет"


def _empty_manifest_row() -> dict[str, Any]:
    """Пустая строка манифеста (все колонки заданы — CSV не «плывёт»)."""
    return dict.fromkeys(MANIFEST_COLUMNS, "")


def _file_fields(prefix: str, file: FileMetadataResponse | None) -> dict[str, Any]:
    """Поля файла манифеста (`raw_*` или `markup_*`)."""
    if file is None:
        return {}
    return {
        f"{prefix}_id": str(file.id),
        f"{prefix}_name": file.file_name,
        f"{prefix}_size": file.size,
        f"{prefix}_import_date": file.import_date.isoformat(),
        f"{prefix}_s3_path": file.s3_path,
    }


def _as_overrides(source: Overrides | Sequence[RecordOverride] | None) -> Overrides:
    """Приводит ручные решения к `Overrides` (принимает и набор, и список)."""
    if source is None:
        return Overrides()
    if isinstance(source, Overrides):
        return source
    return Overrides(entries=list(source))


def _as_stats(
    source: Mapping[str, MarkupStatsEntry | Mapping[str, Any]] | None,
) -> dict[str, MarkupStatsEntry]:
    """Приводит характеристики разметки к `MarkupStatsEntry` по `id` markup-файла."""
    resolved: dict[str, MarkupStatsEntry] = {}
    for file_id, value in (source or {}).items():
        if isinstance(value, MarkupStatsEntry):
            resolved[str(file_id)] = value
        else:
            resolved[str(file_id)] = MarkupStatsEntry.from_dict(value)
    return resolved


def _build_stats(
    files: Sequence[FileMetadataResponse],
    records: Sequence[Record],
    unpaired_markup: Sequence[FileMetadataResponse],
    other_files: Sequence[FileMetadataResponse],
) -> RecordsStats:
    """Считает показатели; полнота манифеста: files == records + attached + unpaired + other."""
    per_base = Counter(record.base_name for record in records)
    return RecordsStats(
        files=len(files),
        records=len(records),
        confirmed=sum(1 for record in records if record.confidence == CONFIDENCE_CONFIRMED),
        manual=sum(1 for record in records if record.manual),
        weak=sum(1 for record in records if record.confidence == CONFIDENCE_WEAK),
        without_markup=sum(1 for record in records if not record.has_markup),
        duplicates=sum(1 for record in records if record.duplicate),
        duplicate_bases=sum(1 for count in per_base.values() if count > 1),
        unpaired_markup=len(unpaired_markup),
        other=len(other_files),
        excluded=sum(1 for record in records if record.excluded),
        raw_bytes=sum(record.raw.size for record in records),
        markup_bytes=sum(record.markup.size for record in records if record.markup is not None),
    )
