"""Ручные решения оператора по объединению записей (FR-T2).

В API записи (пары RAW+markup) нет: объединение выполняет пульт, сопоставляя файлы
по базовому имени и времени импорта. При дублях (одинаковые имена, различаются
только `id`/`size`/`import_date`/`s3_path`) эвристика может ошибаться, поэтому
оператор задаёт решение вручную:

    link    — привязать выбранный markup к RAW;
    unlink  — снять привязку разметки;
    exclude — исключить запись из испытаний;
    current — отметить запись актуальной версией (при дублях).

Решения хранятся в `acceptance_data/records_overrides.json` — файл общий для пульта
(стенд один), поэтому решения переиспользуются между сессиями испытаний. Каждое
решение несёт аудит: кто (`operator`), когда (`at`), в какой сессии (`session_id`)
и комментарий; действует правило «последнее решение выигрывает».
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from acceptance.paths import DATA_DIR, ensure_dirs
from acceptance.session import now_iso

OVERRIDES_FILENAME = "records_overrides.json"
SCHEMA_VERSION = 1

ACTION_LINK = "link"
ACTION_UNLINK = "unlink"
ACTION_EXCLUDE = "exclude"
ACTION_CURRENT = "current"
ACTIONS = (ACTION_LINK, ACTION_UNLINK, ACTION_EXCLUDE, ACTION_CURRENT)

ACTION_LABELS: dict[str, str] = {
    ACTION_LINK: "привязка markup к RAW",
    ACTION_UNLINK: "снятие привязки разметки",
    ACTION_EXCLUDE: "исключение записи из испытаний",
    ACTION_CURRENT: "отметка актуальной версии",
}


@dataclass
class RecordOverride:
    """Одно ручное решение оператора по записи."""

    base_name: str
    raw_id: str
    action: str
    markup_id: str | None = None
    comment: str = ""
    operator: str = ""
    session_id: str = ""
    at: str = field(default_factory=now_iso)

    @property
    def action_label(self) -> str:
        """Человекочитаемое название действия."""
        return ACTION_LABELS.get(self.action, self.action)

    def to_dict(self) -> dict[str, Any]:
        """Сериализует решение."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordOverride:
        """Восстанавливает решение из JSON (терпимо к неизвестным ключам)."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        payload.setdefault("base_name", "")
        payload.setdefault("raw_id", "")
        payload.setdefault("action", ACTION_LINK)
        payload.setdefault("at", now_iso())
        return cls(**payload)


@dataclass
class Overrides:
    """Набор ручных решений (файл `records_overrides.json`)."""

    entries: list[RecordOverride] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    # -- чтение --------------------------------------------------------------
    def for_base(self, base_name: str) -> list[RecordOverride]:
        """Решения по базе имени (в порядке применения — по времени)."""
        return sorted(
            (entry for entry in self.entries if entry.base_name == base_name),
            key=lambda entry: entry.at,
        )

    @property
    def last_by_raw(self) -> dict[str, RecordOverride]:
        """Последнее решение по каждому RAW-файлу (действует именно оно)."""
        resolved: dict[str, RecordOverride] = {}
        for entry in sorted(self.entries, key=lambda item: item.at):
            if entry.raw_id:
                resolved[entry.raw_id] = entry
        return resolved

    def history(self) -> list[dict[str, Any]]:
        """История решений (для отчёта и интерфейса)."""
        return [entry.to_dict() for entry in sorted(self.entries, key=lambda item: item.at)]

    # -- запись --------------------------------------------------------------
    def add(
        self,
        *,
        action: str,
        base_name: str,
        raw_id: str,
        markup_id: str | None = None,
        comment: str = "",
        operator: str = "",
        session_id: str = "",
    ) -> RecordOverride:
        """Добавляет решение оператора (действует как последнее для этого RAW)."""
        if action not in ACTIONS:
            raise ValueError(f"Неизвестное действие: {action}")

        entry = RecordOverride(
            base_name=base_name,
            raw_id=str(raw_id),
            action=action,
            markup_id=str(markup_id) if markup_id else None,
            comment=comment.strip(),
            operator=operator.strip(),
            session_id=session_id.strip(),
        )
        self.entries.append(entry)
        return entry

    def remove_for_raw(self, raw_id: str) -> int:
        """Убирает все решения по RAW-файлу (возврат к эвристике)."""
        before = len(self.entries)
        self.entries = [entry for entry in self.entries if entry.raw_id != str(raw_id)]
        return before - len(self.entries)

    # -- сериализация --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Сериализует набор решений."""
        return {
            "schema_version": self.schema_version,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Overrides:
        """Восстанавливает набор решений из JSON."""
        entries = [RecordOverride.from_dict(item) for item in (data.get("entries") or [])]
        return cls(entries=entries, schema_version=int(data.get("schema_version", SCHEMA_VERSION)))


def overrides_path(directory: Path | None = None) -> Path:
    """Путь к файлу ручных решений."""
    base = Path(directory) if directory is not None else DATA_DIR
    return base / OVERRIDES_FILENAME


def load_overrides(directory: Path | None = None) -> Overrides:
    """Читает решения; при отсутствии или повреждении файла возвращает пустой набор."""
    path = overrides_path(directory)
    if not path.exists():
        return Overrides()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Overrides()

    if not isinstance(payload, dict):
        return Overrides()
    return Overrides.from_dict(payload)


def save_overrides(overrides: Overrides, directory: Path | None = None) -> Path:
    """Сохраняет решения на диск (UTF-8, читаемый JSON)."""
    ensure_dirs()
    path = overrides_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(overrides.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
