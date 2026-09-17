"""Снимок серверных статусов задач и архив экрана «Задачи» (FR-T11, этап T4).

В `GET /api/tasks/` статуса задачи нет: в спецификации у `CeleryTask` только `type`,
`name`, `description`, `id`, `created_at`. Состояние («исполняется задача сейчас или
нет») приходит лишь в карточке `GET /api/tasks/{id}` (`runtimes[-1].status`), поэтому
список со статусами — это N запросов на N задач (замечание P1). Снимок решает две
задачи:

    * хранит **последний известный серверный статус** каждой задачи (со временем
      получения и ошибкой, если карточка не пришла) — таблица показывает статус без
      новых запросов, а оператор видит, когда он получен;
    * ведёт **архив**: задача с терминальным статусом, завершённая ≥ суток назад,
      автоматически уходит в архив (правило `ARCHIVE_AFTER`); завершённые можно
      перенести в архив сразу кнопкой. Если задача вернулась в работу, метка архива
      снимается — в основном списке снова видно то, что исполняется сейчас.

Файл `acceptance_data/task_snapshot.json` общий для пульта (как
`records_overrides.json`), поэтому архив и последние статусы переживают перезапуск.
В файл сессии эти данные не пишутся: протокол ведётся журналом обмена и чек-листом.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from acceptance.paths import DATA_DIR, ensure_dirs
from acceptance.session import now_iso
from client.schemas import TaskStatus
from lib.task_status import is_terminal, presentation_for

SNAPSHOT_FILENAME = "task_snapshot.json"
SCHEMA_VERSION = 1

#: Возраст завершения, после которого задача уходит в архив автоматически.
ARCHIVE_AFTER = timedelta(days=1)

#: Терминальные статусы FSM-1 (задача больше не изменится): завершена, ошибка,
#: прервана и «не найдена» (карточка подтвердила, что задачи нет).
TERMINAL_STATUSES: tuple[str, ...] = tuple(
    str(status) for status in TaskStatus if is_terminal(status)
)

ARCHIVED_BY_AUTO = "auto"
ARCHIVED_BY_OPERATOR = "operator"
ARCHIVED_LABELS: dict[str, str] = {
    ARCHIVED_BY_AUTO: "автоматически (завершена более суток назад)",
    ARCHIVED_BY_OPERATOR: "оператором",
}


def _parse_time(value: Any) -> datetime | None:
    """Разбирает отметку времени из API/файла (`datetime`), None — если не разобрать."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class TaskSnapshotEntry:
    """Последнее известное состояние одной задачи (снимок статуса и архив)."""

    task_id: str
    type: str = ""
    name: str = ""
    status: str = ""
    start_time: str = ""
    end_time: str = ""
    fetched_at: str = ""
    error: str = ""
    served: bool = True
    archived_at: str = ""
    archived_by: str = ""

    @property
    def is_terminal(self) -> bool:
        """True, если последний известный статус терминальный."""
        return self.status in TERMINAL_STATUSES

    @property
    def archived(self) -> bool:
        """True, если задача в архиве."""
        return bool(self.archived_at)

    @property
    def status_label(self) -> str:
        """Подпись последнего известного статуса."""
        return presentation_for(self.status).label

    @property
    def status_glyph(self) -> str:
        """Компактная пиктограмма последнего известного статуса."""
        return presentation_for(self.status).glyph

    @property
    def archived_label(self) -> str:
        """Как задача попала в архив (автоматически или оператором)."""
        return ARCHIVED_LABELS.get(self.archived_by, "")

    def finished_at(self) -> datetime | None:
        """Время завершения задачи (None, если оно ещё не известно)."""
        return _parse_time(self.end_time)

    def to_dict(self) -> dict[str, Any]:
        """Сериализует запись снимка."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskSnapshotEntry:
        """Восстанавливает запись снимка (устойчиво к неполным данным файла)."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in dict(data).items() if key in allowed}
        payload["task_id"] = str(payload.get("task_id") or "")
        payload["served"] = bool(payload.get("served", True))
        return cls(**payload)


@dataclass
class TaskSnapshot:
    """Снимок статусов задач и архив экрана «Задачи»."""

    entries: dict[str, TaskSnapshotEntry] = field(default_factory=dict)
    updated_at: str = ""

    # -- наблюдение за задачами ----------------------------------------------
    def entry(self, task_id: str) -> TaskSnapshotEntry:
        """Запись задачи (создаётся при первом обращении)."""
        key = str(task_id).strip()
        if key not in self.entries:
            self.entries[key] = TaskSnapshotEntry(task_id=key)
        return self.entries[key]

    def observe(
        self,
        task_id: str,
        *,
        status: str = "",
        type: str = "",
        name: str = "",
        start_time: str = "",
        end_time: str = "",
        error: str = "",
        served: bool = True,
    ) -> TaskSnapshotEntry:
        """Записывает последнее известное состояние задачи (из карточки или списка).

        Пустые значения не затирают уже известные: если карточка не получена,
        сохраняются прежние статус и времена, а в записи остаётся текст ошибки.
        """
        record = self.entry(task_id)
        record.status = str(status) or record.status
        record.type = str(type) or record.type
        record.name = str(name) or record.name
        record.start_time = str(start_time) or record.start_time
        record.end_time = str(end_time) or record.end_time
        record.error = str(error)
        record.served = bool(served)
        record.fetched_at = now_iso()
        self.updated_at = record.fetched_at
        return record

    def mark_present(self, tasks: Mapping[str, tuple[str, str]]) -> None:
        """Отмечает, какие задачи сейчас есть в выдаче сервера (`served`).

        Args:
            tasks: отображение `task_id → (тип, название)` из текущего списка.
        """
        for key, record in self.entries.items():
            record.served = key in tasks
        for task_id, (task_type, name) in tasks.items():
            record = self.entry(task_id)
            record.type = str(task_type) or record.type
            record.name = str(name) or record.name
            record.served = True

    # -- архив ---------------------------------------------------------------
    def archive(self, task_id: str, *, by: str = ARCHIVED_BY_OPERATOR) -> bool:
        """Переносит задачу в архив (True, если состояние изменилось)."""
        record = self.entry(task_id)
        if record.archived:
            return False
        record.archived_at = now_iso()
        record.archived_by = str(by)
        self.updated_at = record.archived_at
        return True

    def unarchive(self, task_id: str) -> bool:
        """Возвращает задачу из архива (True, если состояние изменилось)."""
        record = self.entries.get(str(task_id).strip())
        if record is None or not record.archived:
            return False
        record.archived_at = ""
        record.archived_by = ""
        self.updated_at = now_iso()
        return True

    def archive_candidates(self) -> list[TaskSnapshotEntry]:
        """Задачи, которые можно перенести в архив: терминальные и не в архиве."""
        return [
            record for record in self.entries.values() if record.is_terminal and not record.archived
        ]

    def archive_completed(self, *, by: str = ARCHIVED_BY_OPERATOR) -> list[str]:
        """Переносит в архив все завершённые задачи (кнопка оператора).

        Returns:
            Идентификаторы перенесённых задач.
        """
        return [
            record.task_id
            for record in self.archive_candidates()
            if self.archive(record.task_id, by=by)
        ]

    def refresh_auto(self, *, now: datetime | None = None) -> tuple[list[str], list[str]]:
        """Применяет авто-правило архива.

        Завершённые (терминальные) задачи старше `ARCHIVE_AFTER` уходят в архив, а
        задачи, вернувшиеся в работу, из архива возвращаются — иначе оператор видел бы
        в архиве то, что исполняется сейчас.

        Returns:
            Кортеж (перенесённые в архив, возвращённые из архива).
        """
        moment = now or datetime.now()
        archived: list[str] = []
        unarchived: list[str] = []
        for record in self.entries.values():
            if record.status and not record.is_terminal:
                if self.unarchive(record.task_id):
                    unarchived.append(record.task_id)
                continue
            finished = record.finished_at()
            if record.is_terminal and not record.archived and finished is not None:
                if moment - finished >= ARCHIVE_AFTER and self.archive(
                    record.task_id, by=ARCHIVED_BY_AUTO
                ):
                    archived.append(record.task_id)
        return archived, unarchived

    def is_aged(self, record: TaskSnapshotEntry, *, now: datetime | None = None) -> bool:
        """True, если задача «завершена ≥ суток назад» (вид архива по умолчанию)."""
        moment = now or datetime.now()
        finished = record.finished_at()
        if finished is None:
            return record.archived_by == ARCHIVED_BY_OPERATOR
        return moment - finished >= ARCHIVE_AFTER

    def archived_entries(
        self, *, include_fresh: bool = False, now: datetime | None = None
    ) -> list[TaskSnapshotEntry]:
        """Записи архива, отсортированные по завершению (сначала свежие).

        Args:
            include_fresh: показывать и «только что перенесённые» (моложе суток).
            now: момент расчёта (в тестах подменяется).
        """
        moment = now or datetime.now()
        selected = [
            record
            for record in self.entries.values()
            if record.archived and (include_fresh or self.is_aged(record, now=moment))
        ]
        return sorted(
            selected,
            key=lambda record: (record.end_time, record.archived_at),
            reverse=True,
        )

    def kpi(self, *, now: datetime | None = None) -> dict[str, int]:
        """Показатели списка: активные, завершённые за сутки, архив, «только что».

        Ключи: `known` (статус известен), `unknown`, `active`, `finished_today`
        (завершены за последние сутки), `archived`, `archived_fresh`.
        """
        moment = now or datetime.now()
        known = [record for record in self.entries.values() if record.status]
        archived = [record for record in self.entries.values() if record.archived]
        fresh = [
            record
            for record in archived
            if (archived_at := _parse_time(record.archived_at)) is not None
            and (moment - archived_at) < ARCHIVE_AFTER
        ]
        finished_today = [
            record
            for record in known
            if record.is_terminal
            and (finished := record.finished_at()) is not None
            and (moment - finished) < ARCHIVE_AFTER
        ]
        return {
            "known": len(known),
            "unknown": len(self.entries) - len(known),
            "active": sum(1 for record in known if not record.is_terminal),
            "finished_today": len(finished_today),
            "archived": len(archived),
            "archived_fresh": len(fresh),
        }

    # -- сериализация --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Представление снимка для файла и отчёта."""
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self.updated_at,
            "entries": [record.to_dict() for record in self.entries.values()],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskSnapshot:
        """Восстанавливает снимок из файла (устойчиво к неполным данным)."""
        snapshot = cls(updated_at=str(data.get("updated_at") or ""))
        raw = data.get("entries")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, Mapping):
                    record = TaskSnapshotEntry.from_dict(item)
                    if record.task_id:
                        snapshot.entries[record.task_id] = record
        elif isinstance(raw, Mapping):  # совместимость с «словарём по id»
            for key, item in raw.items():
                if isinstance(item, Mapping):
                    record = TaskSnapshotEntry.from_dict({"task_id": key, **dict(item)})
                    snapshot.entries[record.task_id or str(key)] = record
        return snapshot


def snapshot_path(directory: Path | None = None) -> Path:
    """Путь к файлу снимка (`acceptance_data/task_snapshot.json`)."""
    base = Path(directory) if directory is not None else DATA_DIR
    return base / SNAPSHOT_FILENAME


def load_snapshot(directory: Path | None = None) -> TaskSnapshot:
    """Читает снимок; при отсутствии или повреждении файла возвращает пустой."""
    path = snapshot_path(directory)
    if not path.exists():
        return TaskSnapshot()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TaskSnapshot()
    if not isinstance(payload, dict):
        return TaskSnapshot()
    return TaskSnapshot.from_dict(payload)


def save_snapshot(snapshot: TaskSnapshot, directory: Path | None = None) -> Path:
    """Сохраняет снимок на диск (UTF-8, читаемый JSON)."""
    ensure_dirs()
    path = snapshot_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def archive_summary(ids: Iterable[str], snapshot: TaskSnapshot) -> str:
    """Подпись переноса в архив: сколько всего и сколько завершилось «только что»."""
    moved = [str(task_id) for task_id in ids]
    if not moved:
        return "переносить нечего: завершённых задач вне архива нет"
    moment = datetime.now()
    fresh = sum(
        1
        for task_id in moved
        if (record := snapshot.entries.get(task_id)) is not None
        and (finished := record.finished_at()) is not None
        and (moment - finished) < ARCHIVE_AFTER
    )
    return f"перенесено в архив: {len(moved)} (из них завершились за последние сутки: {fresh})"
