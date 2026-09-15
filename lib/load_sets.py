"""Наборы нагрузок для субдатасетов и моделей (FR-3, замечание №2).

В API сервера обучения понятия «набор нагрузок» нет: реестр (`GET /api/loads/list`)
плоский и содержит все нагрузки сразу. Замечание №2 требует, чтобы для разных
датасетов/моделей предлагались **разные наборы** нагрузок (иначе список нечитаем)
и чтобы при несоответствии набора выводилось сообщение оператору.

Набор строится гибридно (решение по этапу 2):

    1. **Точно — из markup-файла** (`LoadSetSource.MARKUP`): уникальные `LoadID`
       вместе с фазами (`Ph_n`) и категориями (`Category`). Требует скачивания
       markup-файла, что невозможно для имён с не-ASCII символами (дефект сервера,
       замечание этапа 1) — тогда применяется следующий вариант.
    2. **Резервно — по имени субдатасета** (`LoadSetSource.NAME`): `load_id` реестра
       сопоставляется с базой имени файлов (`Antminer_S19` → «Antminer S19»)
       с нормализацией регистра, `_` и пробелов; набор помечается как
       **предварительный** и требует проверки оператором.
    3. **По модели** (`LoadSetSource.MODEL`): привязки `signals`
       (`signal_num` → `device_id[]`) на живом сервисе не заполнены, поэтому
       используется `signal_aliases` (индекс класса → подпись, например
       «Майнер»/«Не майнер»); такие подписи обычно не являются нагрузками —
       именно этот случай даёт сообщение о несоответствии.
    4. **Вручную** (`LoadSetSource.MANUAL`): набор, который оператор выбрал сам,
       чтобы проверить соответствие.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from client.schemas import LoadItem, ModelMetadata
from lib.markup_stats import DELIMITER, MarkupStats, parse_markup_stats

LOAD_ID_COLUMN = "loadid"
PHASE_COLUMN = "ph_n"
CATEGORY_COLUMN = "category"

#: Ключи, в которых у модели может лежать привязка к нагрузкам (`signals`).
DEVICE_ID_KEYS = ("device_ids", "device_id", "load_ids", "load_id", "loads")

_NORMALIZE_RE = re.compile(r"[\s_]+")
_MESSAGE_LIMIT = 10


class LoadSetSource(StrEnum):
    """Источник набора нагрузок."""

    MARKUP = "markup"
    NAME = "name"
    MODEL = "model"
    MANUAL = "manual"


SOURCE_LABELS: dict[LoadSetSource, str] = {
    LoadSetSource.MARKUP: "разметка (markup-файл)",
    LoadSetSource.NAME: "подбор по имени субдатасета (предварительно)",
    LoadSetSource.MODEL: "классы модели",
    LoadSetSource.MANUAL: "выбрано оператором",
}

#: Нагрузка, которой на сервере обозначено «всё остальное» (category = system).
SYSTEM_LOAD_ID = "__OTHER__"


@dataclass(frozen=True)
class LoadEntry:
    """Нагрузка в наборе: идентификатор, фазы и категории из разметки."""

    load_id: str
    phases: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarkupLoads:
    """Нагрузки, извлечённые из markup-файла."""

    load_ids: tuple[str, ...]
    phases: dict[str, tuple[str, ...]]
    categories: dict[str, tuple[str, ...]]
    stats: MarkupStats

    def entries(self) -> tuple[LoadEntry, ...]:
        """Нагрузки набора с фазами и категориями."""
        return tuple(
            LoadEntry(
                load_id=load_id,
                phases=self.phases.get(load_id, ()),
                categories=self.categories.get(load_id, ()),
            )
            for load_id in self.load_ids
        )


def parse_markup_loads(content: bytes, *, delimiter: str = DELIMITER) -> MarkupLoads:
    """Извлекает нагрузки, фазы и категории из содержимого markup-файла.

    Ожидаемый формат (проверено на живом сервисе):
    `markupId;chunkID;LoadID;Ph_n;Category`.
    """
    stats = parse_markup_stats(content, delimiter=delimiter)
    lines = [
        line for line in content.decode("utf-8", errors="replace").splitlines() if line.strip()
    ]
    if not lines:
        return MarkupLoads(load_ids=(), phases={}, categories={}, stats=stats)

    header = [column.strip().lower() for column in lines[0].split(delimiter)]
    load_index = _column_index(header, LOAD_ID_COLUMN)
    phase_index = _column_index(header, PHASE_COLUMN)
    category_index = _column_index(header, CATEGORY_COLUMN)
    if load_index is None:
        return MarkupLoads(load_ids=(), phases={}, categories={}, stats=stats)

    order: list[str] = []
    seen: set[str] = set()
    phases: dict[str, set[str]] = defaultdict(set)
    categories: dict[str, set[str]] = defaultdict(set)

    for line in lines[1:]:
        cells = line.split(delimiter)
        if load_index >= len(cells):
            continue

        load_id = cells[load_index].strip()
        if not load_id:
            continue

        if load_id not in seen:
            seen.add(load_id)
            order.append(load_id)

        _add_cell(phases[load_id], cells, phase_index)
        _add_cell(categories[load_id], cells, category_index)

    return MarkupLoads(
        load_ids=tuple(order),
        phases={key: tuple(sorted(value)) for key, value in phases.items()},
        categories={key: tuple(sorted(value)) for key, value in categories.items()},
        stats=stats,
    )


def _column_index(header: Sequence[str], column: str) -> int | None:
    """Индекс колонки в заголовке (без учёта регистра)."""
    return next((index for index, name in enumerate(header) if name == column), None)


def _add_cell(target: set[str], cells: Sequence[str], index: int | None) -> None:
    """Добавляет значение колонки, если она есть и не пуста."""
    if index is None or index >= len(cells):
        return

    value = cells[index].strip()
    if value:
        target.add(value)


def normalize_name(value: str) -> str:
    """Нормализует имя для сопоставления: регистр, `_` и кратные пробелы."""
    return _NORMALIZE_RE.sub(" ", value).strip().lower()


@dataclass(frozen=True)
class LoadSet:
    """Набор нагрузок для субдатасета, модели или проверки оператором."""

    label: str
    source: LoadSetSource
    entries: tuple[LoadEntry, ...]
    confident: bool
    notes: tuple[str, ...] = ()

    @property
    def load_ids(self) -> tuple[str, ...]:
        """Идентификаторы нагрузок набора."""
        return tuple(entry.load_id for entry in self.entries)

    @property
    def is_empty(self) -> bool:
        """True, если в наборе нет нагрузок."""
        return not self.entries

    @property
    def source_label(self) -> str:
        """Человекочитаемое описание источника набора."""
        return SOURCE_LABELS[self.source]


def load_set_from_markup(label: str, markup: MarkupLoads) -> LoadSet:
    """Точный набор нагрузок, извлечённый из markup-файла."""
    return LoadSet(
        label=label,
        source=LoadSetSource.MARKUP,
        entries=markup.entries(),
        confident=True,
    )


def load_set_by_name(
    label: str,
    subdataset_name: str,
    registry: Sequence[LoadItem],
    *,
    note: str = "",
) -> LoadSet:
    """Резервный набор: сопоставление `load_id` реестра с именем субдатасета.

    Сначала ищется точное совпадение нормализованных имён (`Antminer_S19` →
    «Antminer S19»), затем — совпадение по словам. Набор всегда помечается
    как предварительный (`confident = False`).
    """
    normalized = normalize_name(subdataset_name)
    exact = [item for item in registry if normalize_name(item.load_id) == normalized]
    matches = exact or [
        item for item in registry if _tokens_subset(normalized, normalize_name(item.load_id))
    ]
    entries = tuple(
        LoadEntry(load_id=item.load_id, categories=(item.category,)) for item in matches
    )

    notes: list[str] = []
    if note:
        notes.append(note)
    notes.append(
        f"Набор получен резервным подбором по имени («{subdataset_name}»): "
        "требуется проверка оператором."
    )
    if matches and not exact:
        notes.append("Точного совпадения по имени нет — использовано совпадение по словам.")
    if not matches:
        notes.append("Совпадений по имени в реестре не найдено — назначьте набор вручную.")

    return LoadSet(
        label=label,
        source=LoadSetSource.NAME,
        entries=entries,
        confident=False,
        notes=tuple(notes),
    )


def load_set_from_model(model: ModelMetadata) -> LoadSet:
    """Набор нагрузок модели: привязки `signals`, иначе подписи классов."""
    device_ids = model_device_ids(model)
    if device_ids:
        return LoadSet(
            label=model.name,
            source=LoadSetSource.MODEL,
            entries=tuple(LoadEntry(load_id=item) for item in device_ids),
            confident=True,
            notes=("Набор получен из привязки сигналов модели (`signals`).",),
        )

    aliases = tuple(dict.fromkeys((model.signal_aliases or {}).values()))
    notes = [
        "Поле `signals` (привязка `signal_num` → `device_id[]`) у модели не заполнено — "
        "набор построен по подписям классов (`signal_aliases`) и требует проверки "
        "(замечание к API)."
    ]
    if not aliases:
        notes.append("У модели не заданы ни привязки нагрузок, ни подписи классов.")

    return LoadSet(
        label=model.name,
        source=LoadSetSource.MODEL,
        entries=tuple(LoadEntry(load_id=label) for label in aliases),
        confident=False,
        notes=tuple(notes),
    )


def load_set_manual(
    label: str,
    load_ids: Sequence[str],
    registry: Sequence[LoadItem],
) -> LoadSet:
    """Набор, выбранный оператором, — для проверки соответствия реестру."""
    by_id = {item.load_id: item for item in registry}
    entries = tuple(
        LoadEntry(
            load_id=load_id,
            categories=(by_id[load_id].category,) if load_id in by_id else (),
        )
        for load_id in load_ids
    )
    return LoadSet(
        label=label,
        source=LoadSetSource.MANUAL,
        entries=entries,
        confident=False,
        notes=("Набор задан оператором для проверки соответствия реестру.",),
    )


def model_device_ids(model: ModelMetadata) -> tuple[str, ...]:
    """Извлекает привязки `device_id` из `signals` модели (если они заполнены)."""
    ids: list[str] = []
    for signal in model.signals or []:
        if not isinstance(signal, dict):
            continue
        for key in DEVICE_ID_KEYS:
            value = signal.get(key)
            if isinstance(value, str) and value:
                ids.append(value)
            elif isinstance(value, list):
                ids.extend(str(item) for item in value if str(item).strip())

    return _unique(ids)


@dataclass(frozen=True)
class LoadSetCheck:
    """Результат сравнения набора нагрузок с реестром."""

    matched: tuple[str, ...]
    missing_in_registry: tuple[str, ...]
    registry_only: tuple[str, ...]
    confident: bool

    @property
    def has_mismatch(self) -> bool:
        """True, если набор не соответствует реестру и об этом нужно сообщить."""
        return bool(self.missing_in_registry) or (self.confident and bool(self.registry_only))


def compare_with_registry(
    load_set: LoadSet,
    registry: Sequence[LoadItem],
) -> LoadSetCheck:
    """Сравнивает набор нагрузок с реестром (замечание №2)."""
    registry_ids = [item.load_id for item in registry]
    registry_set = set(registry_ids)
    set_ids = list(load_set.load_ids)
    set_set = set(set_ids)

    return LoadSetCheck(
        matched=tuple(load_id for load_id in set_ids if load_id in registry_set),
        missing_in_registry=tuple(load_id for load_id in set_ids if load_id not in registry_set),
        registry_only=tuple(load_id for load_id in registry_ids if load_id not in set_set),
        confident=load_set.confident,
    )


def mismatch_messages(check: LoadSetCheck, load_set: LoadSet) -> tuple[str, ...]:
    """Формулирует сообщения оператору о наборе нагрузок (замечание №2)."""
    messages: list[str] = []

    if not load_set.confident:
        messages.append(
            f"Набор «{load_set.label}» — предварительный ({load_set.source_label}); "
            "требуется проверка оператором."
        )

    if check.missing_in_registry:
        messages.append(
            "Нагрузки набора не найдены в реестре: "
            f"{_limited(check.missing_in_registry)}. Проверьте разметку и реестр нагрузок "
            "(замечание к API: реестр наполняется данными разметки)."
        )

    if check.confident and check.registry_only:
        messages.append(
            f"В реестре есть нагрузки, не входящие в набор: {len(check.registry_only)} — "
            "показан только выбранный набор, чтобы не перегружать список (замечание №2)."
        )

    if not messages:
        messages.append("Набор нагрузок соответствует реестру.")

    return tuple(messages)


@dataclass(frozen=True)
class LoadCategory:
    """Категория нагрузки: количество и варианты написания."""

    name: str
    count: int
    case_variants: tuple[str, ...] = ()


def categorize_loads(loads: Sequence[LoadItem]) -> tuple[LoadCategory, ...]:
    """Группирует нагрузки по категориям (счётчик + регистровые варианты)."""
    counts: dict[str, int] = defaultdict(int)
    for item in loads:
        counts[item.category] += 1

    variants: dict[str, list[str]] = defaultdict(list)
    for name in counts:
        variants[name.lower()].append(name)

    return tuple(
        LoadCategory(
            name=name,
            count=count,
            case_variants=tuple(sorted(other for other in variants[name.lower()] if other != name)),
        )
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    )


def category_warnings(categories: Sequence[LoadCategory]) -> tuple[str, ...]:
    """Предупреждения о категориях, различающихся только регистром."""
    warnings: list[str] = []
    reported: set[str] = set()

    for category in categories:
        key = category.name.lower()
        if not category.case_variants or key in reported:
            continue

        reported.add(key)
        spellings = ", ".join(f"«{name}»" for name in (category.name, *category.case_variants))
        warnings.append(
            f"Категория встречается в разных написаниях: {spellings}. Серверный фильтр "
            "`category` регистрозависим — выбирайте значения из реестра (замечание к данным)."
        )

    return tuple(warnings)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    """Уникальные значения с сохранением порядка."""
    return tuple(dict.fromkeys(values))


def _tokens_subset(needle: str, haystack: str) -> bool:
    """True, если все слова `needle` входят в `haystack` (для подбора по имени)."""
    tokens = needle.split()
    return bool(tokens) and all(token in haystack.split() for token in tokens)


def _limited(values: Sequence[str], limit: int = _MESSAGE_LIMIT) -> str:
    """Список значений, ограниченный по длине сообщения."""
    head = ", ".join(values[:limit])
    return head if len(values) <= limit else f"{head} … (всего {len(values)})"
