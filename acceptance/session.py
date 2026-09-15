"""Сессия испытаний: модель, снимки стенда и персистентность (FR-T5).

Сессия — единица испытаний: она фиксирует кто/когда/что испытывал, на какой
сборке сервера, какие проверки выполнялись, с каким результатом и что при этом
происходило (журнал). Файл сессии — `acceptance_data/sessions/<session_id>.json`
(каталог в `.gitignore`); структурный журнал — `logs/session_<session_id>.jsonl`.

Данные испытаний разделяются на:
    * автоматические — снимок версии сервера, версия пульта, ОС/Python, снимки
      счётчиков реестров на начало/конец сессии, история событий;
    * заполняемые испытателем (`SessionInfo`) — наименование, программа-методика,
      объект испытаний, комиссия, цель, объём, условия, критерии, подписи.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from acceptance.paths import ROOT, SESSION_DIR, ensure_dirs

SCHEMA_VERSION = 2
TOOL_VERSION = "0.1.0"

#: История версий схемы файла сессии:
#:   v1 — базовые поля (инфо испытателя, снимки стенда, журнал, замечания, проверки);
#:   v2 — добавлены `artifacts` (выгруженные доказательства: манифесты, запрошенные
#:        выгрузки) и `markup_stats` (рассчитанные характеристики разметки по файлам).
#: Файлы v1 читаются без правок: отсутствующие поля заполняются значениями по умолчанию.

STATUS_DRAFT = "черновик"
STATUS_RUNNING = "идёт"
STATUS_CLOSED = "завершена"
SESSION_STATUSES = (STATUS_DRAFT, STATUS_RUNNING, STATUS_CLOSED)

CONCLUSIONS = ("годен", "годен с замечаниями", "не годен", "не определён")

SNAP_START = "start"
SNAP_END = "end"


def now_iso() -> str:
    """Текущее локальное время в ISO-формате (секунды)."""
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class SessionInfo:
    """Поля сессии, заполняемые испытателем (входят в шапку отчёта)."""

    title: str = ""
    program_doc: str = ""
    object_of_test: str = ""
    customer: str = ""
    lab: str = ""
    operator_fio: str = ""
    operator_position: str = ""
    commission: str = ""
    goal: str = ""
    scope: str = ""
    conditions: str = ""
    limitations: str = ""
    criteria: str = ""
    conclusion: str = ""
    notes: str = ""
    signatures: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SessionInfo:
        """Восстанавливает поля из JSON (неизвестные ключи игнорируются)."""
        if not data:
            return cls()
        allowed = cls.__dataclass_fields__
        return cls(**{key: str(value) for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict[str, str]:
        """Сериализует поля сессии."""
        return asdict(self)

    @property
    def is_filled(self) -> bool:
        """True, если заполнено обязательное ядро (наименование и объект испытаний)."""
        return bool(self.title.strip() and self.object_of_test.strip())


@dataclass
class TestSession:
    """Сессия испытаний целиком (сохраняется в `sessions/<id>.json`)."""

    session_id: str
    status: str = STATUS_DRAFT
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    ended_at: str | None = None
    base_url: str = ""
    server_version: dict[str, Any] = field(default_factory=dict)
    tool_version: dict[str, Any] = field(default_factory=dict)
    logs: dict[str, str] = field(default_factory=dict)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    info: SessionInfo = field(default_factory=SessionInfo)
    notes: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    markup_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    # -- свойства ------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        """True, если сессия идёт или черновик (можно продолжать)."""
        return self.status in (STATUS_DRAFT, STATUS_RUNNING)

    @property
    def duration_seconds(self) -> float | None:
        """Длительность сессии в секундах (по началу и концу/текущему времени)."""
        if not self.started_at:
            return None
        try:
            start = datetime.fromisoformat(self.started_at)
            finish = datetime.fromisoformat(self.ended_at) if self.ended_at else datetime.now()
        except ValueError:
            return None
        return max(0.0, (finish - start).total_seconds())

    @property
    def server_build(self) -> str:
        """Краткая идентификация сборки сервера (revision/branch)."""
        revision = str(
            self.server_version.get("revision") or self.server_version.get("commit") or ""
        )[:12]
        branch = str(self.server_version.get("branch") or "")
        if revision and branch:
            return f"{branch}@{revision}"
        return revision or branch or "не определена"

    # -- история событий -----------------------------------------------------
    def add_history(self, event: str, message: str = "", **payload: Any) -> dict[str, Any]:
        """Добавляет событие в историю сессии (входит в отчёт)."""
        record: dict[str, Any] = {
            "at": now_iso(),
            "event": event,
            "message": message or event,
            "payload": payload,
        }
        self.history.append(record)
        return record

    # -- сериализация --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Сериализует сессию в JSON-совместимый словарь."""
        return {
            "session_id": self.session_id,
            "status": self.status,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "base_url": self.base_url,
            "server_version": self.server_version,
            "tool_version": self.tool_version,
            "logs": self.logs,
            "snapshots": self.snapshots,
            "info": self.info.to_dict(),
            "notes": self.notes,
            "checks": self.checks,
            "counters": self.counters,
            "artifacts": self.artifacts,
            "markup_stats": self.markup_stats,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestSession:
        """Восстанавливает сессию из JSON-словаря."""
        return cls(
            session_id=str(data.get("session_id", "")),
            status=str(data.get("status", STATUS_DRAFT)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            created_at=str(data.get("created_at", now_iso())),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            base_url=str(data.get("base_url", "")),
            server_version=dict(data.get("server_version") or {}),
            tool_version=dict(data.get("tool_version") or {}),
            logs={key: str(value) for key, value in (data.get("logs") or {}).items()},
            snapshots={key: dict(value) for key, value in (data.get("snapshots") or {}).items()},
            info=SessionInfo.from_dict(data.get("info")),
            notes=[dict(item) for item in (data.get("notes") or [])],
            checks=[dict(item) for item in (data.get("checks") or [])],
            counters=dict(data.get("counters") or {}),
            artifacts=[dict(item) for item in (data.get("artifacts") or [])],
            markup_stats={
                str(key): dict(value) for key, value in (data.get("markup_stats") or {}).items()
            },
            history=[dict(item) for item in (data.get("history") or [])],
        )


# ---------------------------------------------------------------------------
# Создание и жизненный цикл сессии
# ---------------------------------------------------------------------------
def new_session_id() -> str:
    """Идентификатор сессии: дата-время + короткий случайный суффикс."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:4]}"


def _git_commit() -> str:
    """Текущий commit пульта (для воспроизводимости отчёта)."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def collect_tool_version() -> dict[str, Any]:
    """Версия пульта и среда исполнения (автоматическая часть сессии)."""
    return {
        "tool": "Пульт испытаний сервера обучения",
        "tool_version": TOOL_VERSION,
        "git_commit": _git_commit(),
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
        "root": ROOT.as_posix(),
        "collected_at": now_iso(),
    }


def new_session(
    *,
    base_url: str,
    server_version: dict[str, Any] | None = None,
    tool_version: dict[str, Any] | None = None,
    logs: dict[str, Any] | None = None,
    info: SessionInfo | None = None,
) -> TestSession:
    """Создаёт новый черновик сессии испытаний."""
    session = TestSession(
        session_id=new_session_id(),
        base_url=base_url,
        server_version=dict(server_version or {}),
        tool_version=dict(tool_version or collect_tool_version()),
        logs={key: str(value) for key, value in (logs or {}).items()},
        info=info or SessionInfo(),
    )
    session.add_history(
        "session_created", "Сессия создана", base_url=base_url, server_build=session.server_build
    )
    return session


def start_session(session: TestSession) -> TestSession:
    """Переводит черновик в статус «идёт» и фиксирует время начала."""
    if session.status == STATUS_DRAFT:
        session.status = STATUS_RUNNING
        session.started_at = now_iso()
    session.add_history("session_started", "Сессия начата", server_build=session.server_build)
    return session


def close_session(session: TestSession, *, conclusion: str | None = None) -> TestSession:
    """Завершает сессию: фиксирует время окончания и итоговое решение."""
    session.status = STATUS_CLOSED
    session.ended_at = now_iso()
    if conclusion:
        session.info.conclusion = conclusion
    session.add_history(
        "session_closed",
        "Сессия завершена",
        conclusion=session.info.conclusion,
        duration_seconds=session.duration_seconds,
    )
    return session


def reopen_session(session: TestSession) -> TestSession:
    """Возвращает завершённую сессию в работу (переоткрытие для дозаполнения)."""
    session.status = STATUS_RUNNING
    session.ended_at = None
    session.add_history("session_reopened", "Сессия переоткрыта")
    return session


def set_snapshot(session: TestSession, phase: str, snapshot: dict[str, Any]) -> TestSession:
    """Сохраняет снимок стенда (счётчики реестров) на начало/конец сессии."""
    session.snapshots[phase] = {"at": now_iso(), **snapshot}
    session.add_history("stand_snapshot", f"Снимок стенда ({phase})", phase=phase)
    return session


def add_artifact(
    session: TestSession,
    *,
    kind: str,
    path: Any,
    size_bytes: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Регистрирует выгруженный артефакт сессии (манифест, запрошенная выгрузка).

    Артефакт — доказательство испытаний: он указывается в отчёте, чтобы результат
    можно было проверить. Файлы лежат в `acceptance_data/artifacts/`.
    """
    file_path = Path(str(path))
    record: dict[str, Any] = {
        "at": now_iso(),
        "kind": kind,
        "name": file_path.name,
        "path": file_path.as_posix(),
        "size_bytes": size_bytes if size_bytes is not None else _safe_size(file_path),
        "note": note,
    }
    session.artifacts.append(record)
    session.add_history(
        "artifact_saved", f"Артефакт: {record['name']}", kind=kind, path=record["path"]
    )
    return record


def set_markup_stats(
    session: TestSession,
    file_id: Any,
    *,
    records: int | None = None,
    chunks: int | None = None,
    status: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Сохраняет характеристики разметки (записи/чанки) по `id` markup-файла.

    Значения считает клиент (`lib.markup_stats`), серверных агрегатов в API нет,
    поэтому результат фиксируется в сессии и указывается в отчёте как клиентская оценка.
    """
    payload: dict[str, Any] = {
        "records": records,
        "chunks": chunks,
        "status": status,
        "note": note,
        "at": now_iso(),
    }
    session.markup_stats[str(file_id)] = payload
    session.add_history(
        "markup_stats",
        f"Разбор разметки: записи = {records}, чанки = {chunks}",
        file_id=str(file_id),
        status=status,
    )
    return payload


def _safe_size(path: Path) -> int:
    """Размер файла, если он есть (иначе 0)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def add_note_to_session(session: TestSession, note: Any) -> TestSession:
    """Добавляет замечание к API в сессию (без дублей по заголовку)."""
    payload = note if isinstance(note, dict) else note.to_dict()
    title = str(payload.get("title", ""))
    if title and any(str(item.get("title", "")) == title for item in session.notes):
        return session
    session.notes.append(payload)
    session.add_history(
        "api_note",
        f"Замечание к API: {title}",
        priority=payload.get("priority"),
        module=payload.get("module"),
        source=payload.get("source"),
    )
    return session


# ---------------------------------------------------------------------------
# Персистентность сессии (acceptance_data/sessions/<id>.json)
# ---------------------------------------------------------------------------
def session_path(session_id: str, directory: Path | None = None) -> Path:
    """Путь к файлу сессии."""
    base = Path(directory) if directory is not None else SESSION_DIR
    return base / f"{session_id}.json"


def session_exists(session_id: str, directory: Path | None = None) -> bool:
    """True, если файл сессии существует."""
    return session_path(session_id, directory).exists()


def save_session(session: TestSession, directory: Path | None = None) -> Path:
    """Сохраняет сессию на диск (UTF-8, читаемый JSON)."""
    ensure_dirs()
    path = session_path(session.session_id, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_json(session), encoding="utf-8")
    return path


def load_session(session_id: str, directory: Path | None = None) -> TestSession:
    """Загружает сессию с диска.

    Raises:
        FileNotFoundError: если файл сессии не найден.
    """
    path = session_path(session_id, directory)
    return TestSession.from_dict(json.loads(path.read_text(encoding="utf-8")))


def delete_session(session_id: str, directory: Path | None = None) -> bool:
    """Удаляет файл сессии (True, если файл был)."""
    path = session_path(session_id, directory)
    if not path.exists():
        return False
    path.unlink()
    return True


def session_json(session: TestSession) -> str:
    """JSON-текст сессии (для скачивания/передачи испытателю)."""
    return json.dumps(session.to_dict(), ensure_ascii=False, indent=2)


def load_session_from_text(text: str) -> TestSession:
    """Восстанавливает сессию из JSON-текста (загрузка файла сессии)."""
    return TestSession.from_dict(json.loads(text))


def session_meta(session: TestSession) -> dict[str, Any]:
    """Краткая карточка сессии (для списка сессий)."""
    return {
        "session_id": session.session_id,
        "status": session.status,
        "title": session.info.title or "(без наименования)",
        "object_of_test": session.info.object_of_test,
        "conclusion": session.info.conclusion or "не определён",
        "base_url": session.base_url,
        "server_build": session.server_build,
        "created_at": session.created_at,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "duration_seconds": session.duration_seconds,
        "checks_total": len(session.checks),
        "notes_total": len(session.notes),
        "path": session_path(session.session_id).as_posix(),
    }


def list_sessions(directory: Path | None = None) -> list[dict[str, Any]]:
    """Список сессий (новые — первыми); повреждённые файлы пропускаются."""
    base = Path(directory) if directory is not None else SESSION_DIR
    if not base.exists():
        return []

    metas: list[dict[str, Any]] = []
    for path in base.glob("*.json"):
        try:
            metas.append(session_meta(load_session(path.stem, directory)))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(metas, key=lambda meta: str(meta.get("created_at") or ""), reverse=True)
