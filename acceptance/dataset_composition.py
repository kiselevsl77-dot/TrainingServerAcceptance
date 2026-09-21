"""Учёт состава датасетов: локальный факт, серверный состав, эвристика (этап T5).

Контракт 17.09.2026 **не отдаёт состав датасета**: `GET /api/datasets/{id}` возвращает
только метаданные (`id`, `creation_date`, `name`, `description`, `type`), а
`POST /api/datasets/fill/{id}` отвечает 202 без `task_id`. Поэтому пульт ведёт учёт
состава у себя, а источник данных всегда помечается:

    * `server` — состав от сервера (эталон). Появляется, когда замечание P1 устранят:
      провайдер сам находит операцию состава в реестре (`acceptance.endpoints`), поэтому
      ни экран, ни сценарии переписывать не нужно;
    * `local` — достоверный факт пульта: состав известен точно, потому что `fill`
      отправлял сам пульт (перечень `raw_file_ids`/`markup_file_ids` записывается
      **до** вызова API, поэтому факт верен даже при отказе задачи);
    * `heuristic` — предположение по реестру файлов (имя, `s3_path`, дата импорта) для
      датасетов, наполненных вне пульта; всегда помечается как гипотеза;
    * `unknown` — данных нет: «состав недоступен (P1)».

Серверный состав **имеет приоритет**, локальный факт остаётся для сверки: расхождение
означает, что сервер сообщает не тот состав, который принял (`compare`).

Дополнительно модуль считает косвенные признаки дублей (`overlap_report`): пересечения
файлов между датасетами и повторные загрузки (`file_name` + `size` при разных `id`) —
в контракте нет `checksum`/`uploaded_by`, поэтому выводы вероятностные.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from acceptance import endpoints
from acceptance.logging_setup import log_event
from acceptance.paths import DATA_DIR
from acceptance.session import TestSession, now_iso
from client.errors import ApiError, ClientError
from client.schemas import FileMetadataResponse
from lib.subdatasets import FileKind, classify

__all__ = [
    "COMPOSITION_MODES",
    "MODE_AUTO",
    "MODE_HEURISTIC",
    "MODE_LOCAL",
    "MODE_SERVER",
    "ROLE_LABELS",
    "ROLE_MARKUP",
    "ROLE_RAW",
    "SOURCE_HEURISTIC",
    "SOURCE_LABELS",
    "SOURCE_LOCAL",
    "SOURCE_SERVER",
    "SOURCE_UNKNOWN",
    "STORE_NAME",
    "STORE_VERSION",
    "CompositionFile",
    "DatasetComposition",
    "compare",
    "composition_from_server",
    "composition_of",
    "composition_rows",
    "composition_store",
    "heuristic_composition",
    "load_store",
    "overlap_report",
    "read_composition",
    "record_composition",
    "record_fill",
    "role_for",
    "save_store",
    "server_available",
    "server_composition",
    "server_summary_key",
    "store_path",
    "stored_composition",
    "unknown_composition",
]

#: Режимы чтения состава (`read_composition`).
MODE_AUTO = "auto"
MODE_SERVER = "server"
MODE_LOCAL = "local"
MODE_HEURISTIC = "heuristic"
COMPOSITION_MODES: tuple[str, ...] = (MODE_AUTO, MODE_SERVER, MODE_LOCAL, MODE_HEURISTIC)

#: Происхождение состава: откуда взяты данные.
SOURCE_SERVER = "server"
SOURCE_LOCAL = "local"
SOURCE_HEURISTIC = "heuristic"
SOURCE_UNKNOWN = "unknown"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_SERVER: "сервер (эталон)",
    SOURCE_LOCAL: "локальный учёт пульта (факт fill)",
    SOURCE_HEURISTIC: "предположение по реестру файлов (гипотеза)",
    SOURCE_UNKNOWN: "состав недоступен (P1)",
}

ROLE_RAW = "raw"
ROLE_MARKUP = "markup"
ROLE_LABELS: dict[str, str] = {ROLE_RAW: "RAW", ROLE_MARKUP: "markup"}

#: Операции реестра, появление которых означает, что серверный состав доступен.
SERVER_SUMMARY_KEYS: tuple[str, ...] = (
    "get /api/datasets/{dataset_id}/summary",
    "get /api/datasets/{dataset_id}/composition",
)

#: Ожидаемые имена полей серверного состава (первое найденное выигрывает).
SERVER_RAW_KEYS: tuple[str, ...] = ("raw_file_ids", "raw_file_id", "raw_files", "raw")
SERVER_MARKUP_KEYS: tuple[str, ...] = ("markup_file_ids", "markup_files", "markup")


def _as_id(value: Any) -> str:
    """Идентификатор файла/датасета строкой (сервер отдаёт UUID объектом)."""
    return str(value or "").strip()


@dataclass(frozen=True)
class CompositionFile:
    """Файл в составе датасета: идентификатор, роль и справочные поля реестра."""

    file_id: str
    role: str = ROLE_RAW
    file_name: str = ""
    size: int = 0
    file_type: str = ""
    import_date: str = ""
    s3_path: str = ""

    @property
    def role_label(self) -> str:
        """Роль файла в записи (`RAW`/`markup`)."""
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def size_label(self) -> str:
        """Размер файла в мегабайтах (для таблиц интерфейса и отчёта)."""
        return f"{self.size / 1024 / 1024:.2f} МБ"

    def to_dict(self) -> dict[str, Any]:
        """Сериализует файл состава в JSON-совместимый словарь."""
        return {
            "file_id": self.file_id,
            "role": self.role,
            "file_name": self.file_name,
            "size": int(self.size),
            "file_type": self.file_type,
            "import_date": self.import_date,
            "s3_path": self.s3_path,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompositionFile:
        """Восстанавливает файл состава из словаря."""
        return cls(
            file_id=_as_id(data.get("file_id")),
            role=str(data.get("role") or ROLE_RAW),
            file_name=str(data.get("file_name") or ""),
            size=int(data.get("size") or 0),
            file_type=str(data.get("file_type") or ""),
            import_date=str(data.get("import_date") or ""),
            s3_path=str(data.get("s3_path") or ""),
        )

    @classmethod
    def from_metadata(cls, file: FileMetadataResponse, *, role: str = ROLE_RAW) -> CompositionFile:
        """Снимок файла реестра (`GET /api/data/files`) в составе датасета."""
        return cls(
            file_id=str(file.id),
            role=role,
            file_name=str(file.file_name),
            size=int(file.size),
            file_type=str(file.file_type),
            import_date=file.import_date.isoformat() if file.import_date else "",
            s3_path=str(file.s3_path),
        )


@dataclass(frozen=True)
class DatasetComposition:
    """Состав датасета по одному источнику (сервер, факт пульта, гипотеза)."""

    dataset_id: str
    source: str = SOURCE_UNKNOWN
    name: str = ""
    dataset_type: str = ""
    captured_at: str = field(default_factory=now_iso)
    files: tuple[CompositionFile, ...] = ()
    task_id: str = ""
    task_status: str = ""
    session_id: str = ""
    operator: str = ""
    note: str = ""
    confidence: str = ""
    checks: tuple[str, ...] = ()
    #: Серверные агрегаты состава (`records`, `chunks`, …) — когда API их отдаёт.
    counts: dict[str, Any] = field(default_factory=dict)

    # -- свойства ------------------------------------------------------------
    @property
    def source_label(self) -> str:
        """Человекочитаемое происхождение состава."""
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def is_trusted(self) -> bool:
        """True, если состав достоверен (серверный или факт пульта)."""
        return self.source in (SOURCE_SERVER, SOURCE_LOCAL)

    @property
    def is_hypothesis(self) -> bool:
        """True, если состав — предположение и требует подтверждения."""
        return self.source == SOURCE_HEURISTIC

    @property
    def raw_files(self) -> tuple[CompositionFile, ...]:
        """Файлы RAW в составе."""
        return tuple(item for item in self.files if item.role == ROLE_RAW)

    @property
    def markup_files(self) -> tuple[CompositionFile, ...]:
        """Файлы разметки в составе."""
        return tuple(item for item in self.files if item.role == ROLE_MARKUP)

    @property
    def raw_ids(self) -> tuple[str, ...]:
        """Идентификаторы RAW-файлов состава."""
        return tuple(item.file_id for item in self.raw_files)

    @property
    def markup_ids(self) -> tuple[str, ...]:
        """Идентификаторы markup-файлов состава."""
        return tuple(item.file_id for item in self.markup_files)

    @property
    def files_count(self) -> int:
        """Число файлов в составе."""
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        """Суммарный размер файлов состава."""
        return sum(item.size for item in self.files)

    @property
    def total_label(self) -> str:
        """Суммарный размер состава в мегабайтах."""
        return f"{self.total_bytes / 1024 / 1024:.2f} МБ"

    def with_files(self, files: Sequence[CompositionFile]) -> DatasetComposition:
        """Копия состава с другим перечнем файлов (уточнение гипотезы)."""
        return replace(self, files=tuple(files))

    def with_counts(self, counts: Mapping[str, Any]) -> DatasetComposition:
        """Копия состава с серверными агрегатами (`records`, `chunks`, …)."""
        return replace(self, counts={str(key): value for key, value in counts.items()})

    @property
    def counts_label(self) -> str:
        """Агрегаты состава строкой (пусто, если сервер их не сообщает)."""
        return ", ".join(f"{key}={value}" for key, value in self.counts.items())

    def to_dict(self) -> dict[str, Any]:
        """Сериализует состав в JSON-совместимый словарь (файл сессии, sidecar)."""
        return {
            "dataset_id": self.dataset_id,
            "source": self.source,
            "name": self.name,
            "dataset_type": self.dataset_type,
            "captured_at": self.captured_at,
            "files": [item.to_dict() for item in self.files],
            "task_id": self.task_id,
            "task_status": self.task_status,
            "session_id": self.session_id,
            "operator": self.operator,
            "note": self.note,
            "confidence": self.confidence,
            "checks": list(self.checks),
            "counts": dict(self.counts),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DatasetComposition:
        """Восстанавливает состав из словаря (терпимо к пропущенным полям)."""
        raw_files = data.get("files")
        files = tuple(
            CompositionFile.from_dict(item)
            for item in (raw_files if isinstance(raw_files, list) else [])
            if isinstance(item, Mapping)
        )
        raw_checks = data.get("checks")
        return cls(
            dataset_id=_as_id(data.get("dataset_id") or data.get("id")),
            source=str(data.get("source") or SOURCE_UNKNOWN),
            name=str(data.get("name") or ""),
            dataset_type=str(data.get("dataset_type") or ""),
            captured_at=str(data.get("captured_at") or now_iso()),
            files=files,
            task_id=str(data.get("task_id") or ""),
            task_status=str(data.get("task_status") or ""),
            session_id=str(data.get("session_id") or ""),
            operator=str(data.get("operator") or ""),
            note=str(data.get("note") or ""),
            confidence=str(data.get("confidence") or ""),
            checks=tuple(
                str(item) for item in (raw_checks if isinstance(raw_checks, list) else [])
            ),
            counts=dict(data.get("counts") or {})
            if isinstance(data.get("counts"), Mapping)
            else {},
        )


# ---------------------------------------------------------------------------
# Локальное хранилище состава (sidecar рядом с сессиями испытаний)
# ---------------------------------------------------------------------------
#: Имя sidecar-файла локального учёта состава в `acceptance_data/`.
STORE_NAME = "dataset_composition.json"

#: Версия формата sidecar-файла (появляется в отчёте для воспроизводимости).
STORE_VERSION = 1


def store_path(directory: Path | str | None = None) -> Path:
    """Путь к sidecar-файлу локального учёта состава.

    Состав датасетов — знание, которое переживает одну сессию испытаний (датасеты
    живут дольше сессии), поэтому он хранится и в сессии (для отчёта), и отдельным
    файлом в `acceptance_data/`.
    """
    base = Path(directory) if directory is not None else DATA_DIR
    return base / STORE_NAME


def save_store(records: Sequence[DatasetComposition], path: Path | str | None = None) -> Path:
    """Сохраняет локальный учёт состава в sidecar-файл.

    Returns:
        Путь к записанному файлу (для регистрации артефактом сессии).
    """
    file_path = Path(path) if path is not None else store_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": STORE_VERSION,
        "saved_at": now_iso(),
        "datasets": [item.to_dict() for item in records],
    }
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return file_path


def load_store(path: Path | str | None = None) -> list[DatasetComposition]:
    """Читает локальный учёт состава; отсутствие или порча файла — пустой список."""
    file_path = Path(path) if path is not None else store_path()
    if not file_path.exists():
        return []
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = payload.get("datasets") if isinstance(payload, Mapping) else payload
    if not isinstance(raw, list):
        return []
    return [
        DatasetComposition.from_dict(item)
        for item in raw
        if isinstance(item, Mapping) and str(item.get("dataset_id") or "")
    ]


# ---------------------------------------------------------------------------
# Состав в сессии испытаний (схема v5: `session.dataset_composition`)
# ---------------------------------------------------------------------------
def composition_store(session: TestSession) -> list[DatasetComposition]:
    """Записи состава из сессии (по одной на датасет и источник данных)."""
    return [
        DatasetComposition.from_dict(item)
        for item in session.dataset_composition
        if isinstance(item, Mapping) and str(item.get("dataset_id") or "")
    ]


def composition_of(
    session: TestSession, dataset_id: Any, *, source: str = ""
) -> DatasetComposition | None:
    """Запись состава датасета из сессии (опционально — конкретного источника)."""
    key = _as_id(dataset_id)
    for item in reversed(composition_store(session)):
        if item.dataset_id != key:
            continue
        if source and item.source != source:
            continue
        return item
    return None


def record_composition(
    session: TestSession,
    composition: DatasetComposition,
    *,
    store_directory: Path | str | None = None,
) -> dict[str, Any]:
    """Записывает состав в сессию и в sidecar (заменяя запись того же источника).

    Returns:
        Словарь записи — для интерфейса и истории сессии.
    """
    record = composition.to_dict()
    stored = [
        item
        for item in session.dataset_composition
        if not (
            str(item.get("dataset_id")) == composition.dataset_id
            and str(item.get("source")) == composition.source
        )
    ]
    stored.append(record)
    session.dataset_composition = stored
    save_store(composition_store(session), store_path(store_directory))
    session.add_history(
        "dataset_composition",
        f"Состав датасета {composition.name or composition.dataset_id}: "
        f"{composition.files_count} файлов ({composition.source_label})",
        dataset_id=composition.dataset_id,
        source=composition.source,
        files=composition.files_count,
    )
    log_event(
        "dataset_composition",
        f"Состав датасета зафиксирован: {composition.dataset_id}",
        module="datasets",
        check_id=(composition.checks[0] if composition.checks else None),
        payload={
            "dataset_id": composition.dataset_id,
            "source": composition.source,
            "files": composition.files_count,
            "raw": len(composition.raw_ids),
            "markup": len(composition.markup_ids),
            "task_id": composition.task_id or None,
        },
    )
    return record


def record_fill(
    session: TestSession,
    *,
    dataset_id: Any,
    raw_file_ids: Sequence[Any],
    markup_file_ids: Sequence[Any] = (),
    name: str = "",
    dataset_type: str = "",
    task_id: str = "",
    task_status: str = "",
    check_id: str = "",
    note: str = "",
    files: Sequence[Any] | Mapping[str, Any] | None = None,
    operator: str = "",
    store_directory: Path | str | None = None,
) -> DatasetComposition:
    """Фиксирует факт наполнения датасета пультом (достоверный состав).

    Состав записывается **до** вызова `POST /api/datasets/fill/{id}`: сервер не
    возвращает ни состав, ни `task_id`, поэтому факт «что именно отправлено» —
    единственный достоверный источник, и он не должен теряться при отказе задачи.
    """
    index = _file_index(files)
    items = [
        *_composition_files(raw_file_ids, ROLE_RAW, index),
        *_composition_files(markup_file_ids, ROLE_MARKUP, index),
    ]
    composition = DatasetComposition(
        dataset_id=_as_id(dataset_id),
        source=SOURCE_LOCAL,
        name=name,
        dataset_type=dataset_type,
        files=tuple(items),
        task_id=task_id,
        task_status=task_status,
        session_id=session.session_id,
        operator=operator or session.info.operator_fio,
        note=note or "состав отправлен пультом в POST /api/datasets/fill/{dataset_id}",
        confidence="факт",
        checks=(check_id,) if check_id else (),
    )
    record_composition(session, composition, store_directory=store_directory)
    return composition


def _file_index(
    files: Sequence[Any] | Mapping[str, Any] | None,
) -> dict[str, FileMetadataResponse]:
    """Индекс файлов реестра по `id` — для обогащения состава именами и размерами."""
    if files is None:
        return {}
    items = files.values() if isinstance(files, Mapping) else files
    return {str(item.id): item for item in items if isinstance(item, FileMetadataResponse)}


def _composition_files(
    file_ids: Sequence[Any], role: str, index: Mapping[str, FileMetadataResponse]
) -> list[CompositionFile]:
    """Файлы состава по перечню идентификаторов (с обогащением из реестра)."""
    result: list[CompositionFile] = []
    for value in file_ids:
        file_id = _as_id(value)
        if not file_id:
            continue
        known = index.get(file_id)
        if known is not None:
            result.append(CompositionFile.from_metadata(known, role=role))
        else:
            result.append(CompositionFile(file_id=file_id, role=role))
    return result


def role_for(file: FileMetadataResponse) -> str:
    """Роль файла в составе: RAW или markup (по имени, затем по типу)."""
    kind, _base = classify(str(file.file_name))
    if kind is FileKind.MARKUP:
        return ROLE_MARKUP
    if kind is FileKind.RAW:
        return ROLE_RAW
    if "markup" in str(file.file_name).casefold():
        return ROLE_MARKUP
    return ROLE_RAW


# ---------------------------------------------------------------------------
# Провайдеры состава: сервер → локальный факт → гипотеза
# ---------------------------------------------------------------------------
def server_summary_key() -> str:
    """Ключ операции состава в реестре (`""` — операции нет, замечание P1 открыто)."""
    for key in SERVER_SUMMARY_KEYS:
        if endpoints.find(key) is not None:
            return key
    return ""


def server_available() -> bool:
    """True, если в реестре операций есть операция состава датасета (P1 устранено)."""
    return bool(server_summary_key())


def server_composition(
    apis: Any,
    dataset_id: Any,
    *,
    name: str = "",
    dataset_type: str = "",
    files: Sequence[Any] | Mapping[str, Any] | None = None,
) -> DatasetComposition | None:
    """Серверный состав датасета, если клиент API его отдаёт.

    Провайдер включается сам, когда в реестре операций появляется операция состава:
    клиентский метод `DatasetsApi.get_dataset_summary` вызывается по наличию, поэтому
    ни экран, ни сценарии проверок менять не нужно (задел на устранение замечания P1).
    """
    if apis is None:
        return None
    method = getattr(getattr(apis, "datasets", None), "get_dataset_summary", None)
    if not callable(method):
        return None
    payload = method(dataset_id)
    return composition_from_server(
        dataset_id, payload, name=name, dataset_type=dataset_type, files=files
    )


def composition_from_server(
    dataset_id: Any,
    payload: Any,
    *,
    name: str = "",
    dataset_type: str = "",
    files: Sequence[Any] | Mapping[str, Any] | None = None,
) -> DatasetComposition | None:
    """Разбирает серверный состав датасета (None — состав в ответе не найден).

    Ожидаемые формы ответа (перечислены в перспективных требованиях,
    `notes.PROSPECTIVE_REQUIREMENTS`): `{raw_file_ids, markup_file_ids, records, chunks}`
    либо `{files: [{id, role|file_type, …}], …}`.
    """
    if not isinstance(payload, Mapping):
        return None
    body: Mapping[str, Any] = payload
    nested = payload.get("dataset")
    if isinstance(nested, Mapping):
        body = nested
    index = _file_index(files)
    items: list[CompositionFile] = []
    for keys, role in ((SERVER_RAW_KEYS, ROLE_RAW), (SERVER_MARKUP_KEYS, ROLE_MARKUP)):
        for key in keys:
            value = body.get(key)
            if isinstance(value, list):
                items.extend(_composition_files(value, role, index))
                break
    listed = body.get("files") or body.get("composition")
    if isinstance(listed, list):
        for entry in listed:
            if isinstance(entry, Mapping):
                file_id = _as_id(entry.get("file_id") or entry.get("id"))
                if not file_id:
                    continue
                known = index.get(file_id)
                role = str(entry.get("role") or "")
                if known is not None:
                    resolved = role_for(known)
                    items.append(
                        CompositionFile.from_metadata(
                            known, role=role if role in (ROLE_RAW, ROLE_MARKUP) else resolved
                        )
                    )
                else:
                    items.append(
                        CompositionFile(
                            file_id=file_id,
                            role=role if role in (ROLE_RAW, ROLE_MARKUP) else ROLE_RAW,
                            file_name=str(entry.get("file_name") or ""),
                            size=int(entry.get("size") or 0),
                        )
                    )
            else:
                items.extend(_composition_files([entry], ROLE_RAW, index))
    if not items:
        return None
    counts = {
        key: body.get(key)
        for key in ("records", "chunks", "raw_count", "markup_count", "files_count")
        if body.get(key) is not None
    }
    return DatasetComposition(
        dataset_id=_as_id(dataset_id),
        source=SOURCE_SERVER,
        name=name,
        dataset_type=dataset_type,
        files=tuple(items),
        note="состав получен от сервера (операция есть в реестре)",
        confidence="эталон",
        checks=(),
    ).with_counts(counts)


def _tokens(text: str) -> list[str]:
    """Слова-токены имени датасета (для сопоставления с именами файлов)."""
    cleaned = "".join(char if char.isalnum() else " " for char in str(text).casefold())
    return [token for token in cleaned.split() if token]


def _haystack(file: FileMetadataResponse) -> str:
    """Текст для сопоставления файла с именем датасета (имя + путь в хранилище)."""
    return f"{file.file_name} {file.s3_path}".casefold()


def heuristic_composition(
    dataset_id: Any,
    *,
    name: str = "",
    dataset_type: str = "",
    files: Sequence[Any] | Mapping[str, Any] | None = None,
    min_token_len: int = 3,
) -> DatasetComposition:
    """Предполагаемый состав датасета по реестру файлов (гипотеза).

    Датасеты, наполненные вне пульта, сервер не описывает (P1), поэтому состав
    восстанавливается косвенно: файлы сопоставляются с именем датасета по токенам
    (`skfu-train` → `skfu` + `train`), при неудаче — по первому токену (организация).
    Результат **всегда** помечается как гипотеза: он не доказывает состав, а даёт
    оператору материал для подтверждения или опровержения.
    """
    index = _file_index(files)
    tokens = [token for token in _tokens(name) if len(token) >= min_token_len]
    matched: list[FileMetadataResponse] = []
    confidence = ""
    if tokens:
        matched = [
            item for item in index.values() if all(token in _haystack(item) for token in tokens)
        ]
        if matched:
            confidence = f"гипотеза (совпадение по «{'+'.join(tokens)}»)"
        else:
            first = tokens[0]
            matched = [item for item in index.values() if first in _haystack(item)]
            confidence = f"гипотеза (совпадение только по «{first}»)"
    items = [
        CompositionFile.from_metadata(item, role=role_for(item))
        for item in sorted(matched, key=lambda file: str(file.file_name).casefold())
    ]
    if not items:
        confidence = "гипотеза не построена: совпадений по имени датасета нет"
    return DatasetComposition(
        dataset_id=_as_id(dataset_id),
        source=SOURCE_HEURISTIC,
        name=name,
        dataset_type=dataset_type,
        files=tuple(items),
        note=(
            "серверный состав датасета в контракте отсутствует (P1): состав предполагается "
            "по реестру файлов и требует подтверждения заказчика"
        ),
        confidence=confidence,
    )


def unknown_composition(
    dataset_id: Any, *, name: str = "", dataset_type: str = "", note: str = ""
) -> DatasetComposition:
    """Состав недоступен: ни сервер, ни пульт не знают содержимое датасета."""
    return DatasetComposition(
        dataset_id=_as_id(dataset_id),
        source=SOURCE_UNKNOWN,
        name=name,
        dataset_type=dataset_type,
        note=note or "состав недоступен: серверного состава нет (P1), локальной записи тоже",
    )


def stored_composition(
    dataset_id: Any, *, source: str = SOURCE_LOCAL, path: Path | str | None = None
) -> DatasetComposition | None:
    """Запись состава из sidecar-файла — знание, переживающее сессию испытаний.

    Датасет наполняется один раз, а подтверждать его состав приходится в разных
    сессиях, поэтому локальный учёт читается и из файла (`store_path`), а не только
    из текущей сессии.
    """
    key = _as_id(dataset_id)
    for item in reversed(load_store(path)):
        if item.dataset_id != key:
            continue
        if source and item.source != source:
            continue
        return item
    return None


def read_composition(
    session: TestSession,
    apis: Any,
    dataset_id: Any,
    *,
    mode: str = MODE_AUTO,
    name: str = "",
    dataset_type: str = "",
    files: Sequence[Any] | Mapping[str, Any] | None = None,
    allow_heuristic: bool = True,
    use_store: bool = True,
) -> DatasetComposition:
    """Состав датасета с ярлыком происхождения: сервер → факт пульта → гипотеза.

    Args:
        session: сессия испытаний (источник локального учёта состава).
        apis: агрегат сервисов API (серверный состав; None — сервер недоступен).
        dataset_id: датасет.
        mode: `auto` (сервер → локальный факт → гипотеза), `server`, `local`,
            `heuristic` — принудительный источник данных.
        name: имя датасета (для гипотезы и подписи).
        dataset_type: тип датасета (`direct_fill`).
        files: реестр файлов (`GET /api/data/files`) — источник имён и размеров.
        allow_heuristic: разрешать ли гипотезу, когда сервера и факта нет.
        use_store: читать локальный учёт ещё и из sidecar-файла (`store_path`),
            если в текущей сессии записи нет: состав переживает сессии испытаний.
    """
    key = _as_id(dataset_id)
    if mode not in COMPOSITION_MODES:
        mode = MODE_AUTO
    if not key:
        return unknown_composition(
            key, name=name, dataset_type=dataset_type, note="датасет не выбран"
        )

    if mode in (MODE_AUTO, MODE_SERVER):
        try:
            server = server_composition(
                apis, key, name=name, dataset_type=dataset_type, files=files
            )
        except (ApiError, ClientError) as exc:
            server = None
            server_error = str(exc)
        else:
            server_error = ""
        if server is not None:
            return server
        if mode == MODE_SERVER:
            return unknown_composition(
                key,
                name=name,
                dataset_type=dataset_type,
                note=(
                    "серверный состав запрошен принудительно, но API его не отдал: "
                    f"{server_error or 'операции состава нет в реестре (P1)'}"
                ),
            )

    local = composition_of(session, key, source=SOURCE_LOCAL)
    if local is None and use_store:
        local = stored_composition(key)
    if local is not None and mode in (MODE_AUTO, MODE_LOCAL):
        return local
    if mode == MODE_LOCAL:
        return unknown_composition(
            key,
            name=name,
            dataset_type=dataset_type,
            note="локальной записи о наполнении нет: датасет наполнен вне пульта",
        )

    if allow_heuristic and mode in (MODE_AUTO, MODE_HEURISTIC):
        guess = heuristic_composition(key, name=name, dataset_type=dataset_type, files=files)
        if guess.files_count:
            return guess
    return unknown_composition(key, name=name, dataset_type=dataset_type)


def compare(server: DatasetComposition, local: DatasetComposition) -> dict[str, Any]:
    """Сверяет серверный состав с локальным фактом наполнения (расхождение — дефект).

    Локальный факт — то, что пульт отправил в `fill`; серверный состав — то, что
    сервер сообщает как содержимое датасета. Разные наборы означают, что API отдаёт
    не тот состав, который принял (или наполнение прошло не полностью).
    """

    def block(left: tuple[str, ...], right: tuple[str, ...]) -> dict[str, Any]:
        left_set, right_set = set(left), set(right)
        return {
            "server": len(left_set),
            "local": len(right_set),
            "only_server": sorted(left_set - right_set),
            "only_local": sorted(right_set - left_set),
            "match": left_set == right_set,
        }

    raw = block(server.raw_ids, local.raw_ids)
    markup = block(server.markup_ids, local.markup_ids)
    matched = bool(raw["match"] and markup["match"])
    return {
        "dataset_id": server.dataset_id or local.dataset_id,
        "server_source": server.source,
        "local_source": local.source,
        "server_captured_at": server.captured_at,
        "local_captured_at": local.captured_at,
        "raw": raw,
        "markup": markup,
        "match": matched,
        "verdict": (
            "состав сервера совпал с составом, отправленным пультом"
            if matched
            else "состав сервера не совпал с отправленным пультом — дефект API"
        ),
    }


def overlap_report(
    compositions: Sequence[DatasetComposition],
    files: Sequence[Any] | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Косвенные признаки дублей: пересечения датасетов и повторные загрузки.

    В контракте 17.09.2026 у `FileMetadataResponse` нет `checksum`/`uploaded_by`,
    поэтому «дубли, загруженные разными организациями», доказывать нечем: отчёт даёт
    косвенные признаки — один файл в нескольких датасетах и одинаковые
    `file_name` + `size` при разных `id` — и явно называет вывод вероятностным.
    """
    index = _file_index(files)
    membership: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for item in compositions:
        marker = item.name or item.dataset_id
        for entry in item.files:
            if not entry.file_id:
                continue
            membership.setdefault(entry.file_id, set()).add(marker)
            names.setdefault(entry.file_id, entry.file_name)

    cross = [
        {"file_id": file_id, "file_name": names.get(file_id, ""), "datasets": sorted(values)}
        for file_id, values in sorted(membership.items())
        if len(values) > 1
    ]

    by_name: dict[tuple[str, int], list[str]] = {}
    for file in index.values():
        by_name.setdefault((str(file.file_name).casefold(), int(file.size)), []).append(
            str(file.id)
        )
    repeats: list[dict[str, Any]] = []
    for (file_name, size), file_ids in by_name.items():
        if len(file_ids) < 2:
            continue
        repeats.append(
            {
                "file_name": file_name,
                "size": size,
                "file_ids": sorted(file_ids),
                "datasets": sorted(
                    {marker for file_id in file_ids for marker in membership.get(file_id, ())}
                ),
            }
        )
    repeats.sort(key=lambda item: item["size"], reverse=True)

    by_source: dict[str, int] = {}
    for item in compositions:
        by_source[item.source] = by_source.get(item.source, 0) + 1

    hypotheses: list[str] = []
    if cross:
        hypotheses.append(
            f"файлов в нескольких датасетах: {len(cross)} — состав датасетов пересекается "
            "(датасеты формировались из одного хранилища)"
        )
    if repeats:
        hypotheses.append(
            f"повторов «имя + размер» с разными `id`: {len(repeats)} — признак повторной "
            "загрузки одним или разными специалистами"
        )
    if not hypotheses:
        hypotheses.append("признаков пересечений и повторных загрузок не найдено")
    hypotheses.append(
        "в контракте нет `checksum`/`uploaded_by`: вывод о дублях вероятностный, "
        "требуется перспективное требование к API"
    )
    return {
        "totals": {
            "compositions": len(compositions),
            "datasets": len({item.dataset_id for item in compositions if item.dataset_id}),
            "files_in_compositions": len(membership),
            "cross_dataset": len(cross),
            "repeat_uploads": len(repeats),
            "registry_files": len(index),
        },
        "by_source": by_source,
        "cross_dataset": cross[:50],
        "repeat_uploads": repeats[:50],
        "hypotheses": hypotheses,
    }


def composition_rows(records: Sequence[DatasetComposition]) -> list[dict[str, Any]]:
    """Строки состава для таблиц интерфейса и отчёта испытаний."""
    return [
        {
            "dataset_id": item.dataset_id,
            "name": item.name or "—",
            "source": item.source_label,
            "files": item.files_count,
            "raw": len(item.raw_ids),
            "markup": len(item.markup_ids),
            "size": item.total_label,
            "counts": item.counts_label or "—",
            "task_id": item.task_id or "—",
            "captured_at": item.captured_at,
            "confidence": item.confidence or "—",
            "note": item.note,
        }
        for item in records
    ]
