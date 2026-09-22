"""Отчёт об испытаниях: каркас комплекта — markdown, JSON и приложения (FR-T8/FR-T9, этап T3).

Отчёт строится **только** по данным сессии и структурного журнала — никакие данные не
запрашиваются у сервера в момент формирования, поэтому результат воспроизводим: любой шаг
проверки repeatable по записи `http_request`/`http_response`.

Состав комплекта (каркас T3):

    * `report.md` — человекочитаемый отчёт: шапка сессии, программа и объём испытаний,
      снимки стенда «начало/окончание» и их сравнение, сводка и детализация проверок,
      наблюдение за задачами (FSM-1), артефакты и `__TEST__`-сущности, замечания к API по
      приоритетам, перспективные требования, итог и подписи;
    * `report.json` — машиночитаемая копия (для обработки и архива);
    * приложения: `checks.csv` (таблица проверок), `notes.csv` (замечания),
      `test_entities.csv` (созданное и удалённое), `journal.jsonl` (выдержка журнала),
      файлы манифеста записей, выгруженные экраном «Записи» (`records_manifest_*`).

Раздел «Программа испытаний (план вызовов)» снят на этапе 2 big bang вместе со старым
планировщиком (`acceptance/plan.py`); его место займёт раздел «Программа и объём» с
приложением `programme.csv` — этап 5 big bang.

Полный комплект (сравнение сессий, финализация с подписями, приложения артефактов) —
этап T9; здесь — каркас, который уже пригоден для приёмки сессии.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acceptance import notes as notes_api
from acceptance.checks import engine as checks_engine
from acceptance.dataset_composition import (
    composition_rows,
    composition_store,
    overlap_report,
)
from acceptance.paths import REPORT_DIR, ensure_dirs
from acceptance.session import TestSession, now_iso, pending_test_entities, test_entities

#: Версия формата отчёта (растёт при изменении состава разделов/приложений).
REPORT_VERSION = 2

#: Колонки приложения «Таблица проверок» (совпадают с выгрузкой экрана чек-листа).
CHECK_COLUMNS: tuple[str, ...] = (
    "check_id",
    "group",
    "class",
    "module",
    "title",
    "requirement",
    "status",
    "verdict",
    "operator_note",
    "journal_range",
    "started_at",
    "ended_at",
    "duration_ms",
)

#: Колонки приложения «Перечень `__TEST__`-сущностей» (NFR-T4, TC-CLEAN-02).
ENTITY_COLUMNS: tuple[str, ...] = ("id", "type", "action", "at", "check_id", "note")

#: Префикс вида артефакта с манифестом записей (`records.save_manifest`).
MANIFEST_KIND_PREFIX = "records_manifest"

#: Разделы markdown-отчёта (нумерация — для отчёта и тестов).
#: Раздел «Программа испытаний (план вызовов)» снят на этапе 2 big bang вместе со старым
#: планировщиком: его место займёт раздел «Программа и объём» (`programme.csv`) на этапе 5.
SECTION_TITLES: tuple[str, ...] = (
    "Сессия испытаний",
    "Программа и объём испытаний",
    "Снимки стенда",
    "Сводка проверок",
    "Детализация проверок",
    "Наблюдение за задачами",
    "Датасеты и состав",
    "Артефакты и тестовые сущности",
    "Замечания к API",
    "Перспективные требования",
    "Итог и подписи",
    "Приложения",
)


@dataclass(frozen=True)
class ReportBundle:
    """Сформированный комплект отчёта: тексты файлов и предупреждения о полноте."""

    session_id: str
    generated_at: str
    markdown: str
    payload: dict[str, Any]
    annexes: dict[str, str]
    warnings: tuple[str, ...] = ()

    @property
    def file_stem(self) -> str:
        """Имя комплекта без расширения (`report_<session>`)."""
        return f"report_{self.session_id}"

    def json_text(self) -> str:
        """Машиночитаемая копия отчёта (JSON)."""
        return json.dumps(self.payload, ensure_ascii=False, indent=2)

    def is_complete(self) -> bool:
        """True, если отчёт готов к подписанию (нет предупреждений о полноте)."""
        return not self.warnings

    def save(self, directory: Path | None = None) -> dict[str, Path]:
        """Сохраняет комплект отчёта на диск.

        Returns:
            Словарь «роль файла → путь»: `report_md`, `report_json` и приложения.
        """
        target = directory or REPORT_DIR
        target.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}

        markdown_path = target / f"{self.file_stem}.md"
        markdown_path.write_text(self.markdown, encoding="utf-8")
        written["report_md"] = markdown_path

        json_path = target / f"{self.file_stem}.json"
        json_path.write_text(self.json_text(), encoding="utf-8")
        written["report_json"] = json_path

        for name, content in self.annexes.items():
            path = target / f"{self.file_stem}_{name}"
            path.write_text(content, encoding="utf-8-sig")
            written[name] = path
        return written


# ---------------------------------------------------------------------------
# Готовность отчёта и данные разделов
# ---------------------------------------------------------------------------
def readiness(session: TestSession) -> dict[str, Any]:
    """Полнота сессии для отчёта: KPI и предупреждения «чего не хватает» (T3/T9)."""
    stats = checks_engine.overall_stats(session)
    summary = notes_api.notes_by_priority(session.notes)
    notes_total = sum(len(items) for items in summary.values())
    tasks = list(session.tasks or [])
    artifacts = list(session.artifacts or [])
    entities = test_entities(session)
    pending = pending_test_entities(session)
    blocked_without_note = [
        row["check_id"]
        for row in checks_engine.checklist_rows(session, statuses=("блокировано API",))
        if not any(
            str(note.get("check_id") or "") == str(row["check_id"]) for note in session.notes
        )
    ]
    warnings: list[str] = []
    if not session.info.is_filled:
        warnings.append(
            "не заполнено ядро сессии (наименование и объект испытаний) — шапка отчёта неполна"
        )
    if not session.snapshots.get("start"):
        warnings.append("нет снимка стенда «начало» — сравнение реестров не построить")
    if not session.snapshots.get("end"):
        warnings.append("нет снимка стенда «окончание» — изменение реестров не подтверждено")
    if stats["not_run"]:
        warnings.append(
            f"не выполнено проверок: {stats['not_run']} из {stats['implemented']} — "
            "итог может быть «не определён»"
        )
    if stats["failed"]:
        warnings.append(f"проверок с отказом: {stats['failed']} — требуется разбор причин")
    if blocked_without_note:
        warnings.append(
            "статус «блокировано API» без замечания: " + ", ".join(blocked_without_note[:5])
        )
    if pending:
        remaining = ", ".join(str(item.get("id")) for item in pending[:5])
        warnings.append(f"не удалены созданные `__TEST__`-сущности: {remaining}")
    if not str(session.info.conclusion or "").strip():
        warnings.append(
            "не зафиксировано итоговое решение сессии (годен / годен с замечаниями / …)"
        )

    return {
        "checks": stats,
        "notes": {
            "total": notes_total,
            "p0": len(summary.get("P0", [])),
            "p1": len(summary.get("P1", [])),
            "p2": len(summary.get("P2", [])),
            "auto": sum(1 for note in session.notes if str(note.get("source")) == "авто"),
        },
        "tasks": {"observed": len(tasks)},
        "artifacts": {"total": len(artifacts), "kinds": _count_keys(artifacts, "kind")},
        "test_entities": {"total": len(entities), "pending": len(pending)},
        "session": {
            "filled": session.info.is_filled,
            "status": session.status,
            "duration_seconds": session.duration_seconds,
            "build": session.server_build,
        },
        "warnings": warnings,
        "ready": not warnings,
    }


def _count_keys(items: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    """Считает значения поля по списку записей (для KPI отчёта)."""
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "—")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _snapshot_comparison(session: TestSession) -> dict[str, Any]:
    """Сравнение снимков стенда «начало»/«окончание»: счётчики, дельта, ошибки."""
    start = session.snapshots.get("start") or {}
    end = session.snapshots.get("end") or {}
    start_counts = dict(start.get("counts") or {})
    end_counts = dict(end.get("counts") or {})
    keys = sorted({*start_counts, *end_counts})
    rows = [
        {
            "metric": key,
            "start": start_counts.get(key),
            "end": end_counts.get(key),
            "delta": (
                (end_counts.get(key) or 0) - (start_counts.get(key) or 0)
                if key in end_counts and key in start_counts
                else None
            ),
        }
        for key in keys
    ]
    return {
        "at_start": start.get("at"),
        "at_end": end.get("at"),
        "build_start": (start.get("version") or {}).get("revision") if start else None,
        "build_end": (end.get("version") or {}).get("revision") if end else None,
        "rows": rows,
        "start_errors": dict(start.get("errors") or {}),
        "end_errors": dict(end.get("errors") or {}),
        "records_start": dict(start.get("records") or {}),
        "records_end": dict(end.get("records") or {}),
    }


def _md_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    """Markdown-таблица из строк и колонок (пустые таблицы заменяются подписью)."""
    if not rows:
        return "_(данных нет)_\n"
    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "|".join("---" for _ in columns) + "|"
    body = ["| " + " | ".join(_cell(row.get(column)) for column in columns) + " |" for row in rows]
    return "\n".join([header, divider, *body]) + "\n"


def _cell(value: Any) -> str:
    """Значение ячейки markdown-таблицы (переводы строк и вертикальные черты экранируются)."""
    if value is None:
        return "—"
    text = str(value).replace("|", "\\|").replace("\n", " ").strip()
    return text or "—"


def _section(number: int, title: str, lines: Sequence[str]) -> str:
    """Раздел отчёта: заголовок второго уровня и содержимое."""
    return f"## {number}. {title}\n\n" + "\n".join(lines).rstrip() + "\n"


def _session_section(session: TestSession, ready: Mapping[str, Any]) -> str:
    """Раздел 1: кто, когда и на какой сборке испытывал (шапка отчёта)."""
    info = session.info
    lines = [
        f"* **Сессия:** `{session.session_id}` · статус: {session.status}",
        f"* **Стенд:** {session.base_url or '—'} · сборка: **{session.server_build}**",
        f"* **Начало:** {session.started_at or '—'} · **окончание:** {session.ended_at or '—'}",
        f"* **Наименование испытаний:** {info.title or '—'}",
        f"* **Программа-методика:** {info.program_doc or '—'}",
        f"* **Объект испытаний:** {info.object_of_test or '—'}",
        f"* **Заказчик:** {info.customer or '—'} · **организация:** {info.lab or '—'}",
        f"* **Оператор:** {info.operator_fio or '—'} {info.operator_position or ''}".rstrip(),
        f"* **Комиссия:** {info.commission or '—'}",
        f"* **Инструмент:** пульт испытаний "
        f"{session.tool_version.get('tool_version', '—')} "
        f"(commit {session.tool_version.get('git_commit', '—')}; "
        f"{session.tool_version.get('python', '—')} · {session.tool_version.get('platform', '—')})",
        f"* **Проверок выполнено:** {ready['checks']['done']} из {ready['checks']['implemented']} · "
        f"замечаний к API: {ready['notes']['total']} (P0 — {ready['notes']['p0']})",
    ]
    return _section(1, SECTION_TITLES[0], lines)


def _program_section(session: TestSession) -> str:
    """Раздел 2: цель, объём, условия, ограничения и критерии испытаний."""
    info = session.info
    lines: list[str] = []
    for caption, value in (
        ("Цель испытаний", info.goal),
        ("Объём испытаний", info.scope),
        ("Условия проведения", info.conditions),
        ("Ограничения", info.limitations),
        ("Критерии оценки", info.criteria),
        ("Дополнительные заметки", info.notes),
    ):
        lines.append(f"**{caption}.** {value.strip() or '—'}")
        lines.append("")
    lines.append(
        "Программа проверок: `docs/02. Чек-лист испытаний.md` — "
        f"{checks_engine.overall_stats(session)['implemented']} проверок каталога пульта "
        f"из {checks_engine.overall_stats(session)['program_total']} (остальные — этапы T6–T10)."
    )
    return _section(2, SECTION_TITLES[1], lines)


def _snapshots_section(session: TestSession, comparison: Mapping[str, Any]) -> str:
    """Раздел 3: снимки стенда «начало»/«окончание» и их сравнение (FR-T1, BR-R6)."""
    rows = [
        {
            "metric": row["metric"],
            "start": row["start"],
            "end": row["end"],
            "delta": row["delta"],
        }
        for row in comparison["rows"]
    ]
    lines = [
        f"* Снимок «начало»: {comparison['at_start'] or '—'} "
        f"(revision {comparison['build_start'] or '—'})",
        f"* Снимок «окончание»: {comparison['at_end'] or '—'} "
        f"(revision {comparison['build_end'] or '—'})",
        "",
        _md_table(rows, ("metric", "start", "end", "delta")),
    ]
    start_errors = comparison["start_errors"] or {}
    end_errors = comparison["end_errors"] or {}
    if start_errors or end_errors:
        lines.append("**Ошибки снимков:**")
        for name, text in {**start_errors, **end_errors}.items():
            lines.append(f"* {name}: {text}")
    records_end = comparison["records_end"] or {}
    if records_end:
        lines.append("")
        lines.append(
            "**Записи (RAW + markup) на окончание сессии:** "
            + ", ".join(f"{key} = {value}" for key, value in records_end.items())
        )
    if not session.snapshots.get("end"):
        lines.append("")
        lines.append("> ⚠️ Снимок «окончание» отсутствует: изменение реестров не подтверждено.")
    return _section(3, SECTION_TITLES[2], lines)


def _summary_section(session: TestSession, groups: Sequence[Mapping[str, Any]]) -> str:
    """Раздел 4: сводка проверок — итоги по статусам и по группам программы."""
    stats = checks_engine.overall_stats(session)
    overview = [
        {"metric": "Проверок в каталоге пульта", "value": stats["implemented"]},
        {"metric": "Проверок в программе испытаний (docs/02)", "value": stats["program_total"]},
        {"metric": "Выполнено", "value": stats["done"]},
        {"metric": "Успех", "value": stats["passed"]},
        {"metric": "Отказ", "value": stats["failed"]},
        {"metric": "Блокировано API", "value": stats["blocked"]},
        {"metric": "Не выполнено", "value": stats["not_run"]},
    ]
    lines = [
        _md_table(overview, ("metric", "value")),
        "**По группам:**",
        "",
        _md_table(
            groups, ("group", "stage", "total", "done", "passed", "failed", "blocked", "not_run")
        ),
    ]
    blocked = checks_engine.checklist_rows(session, statuses=("блокировано API",))
    if blocked:
        lines.append("**Ожидаемо блокированные проверки (дефекты API):**")
        lines.append("")
        lines.append(_md_table(blocked, ("check_id", "title", "verdict", "journal_range")))
    return _section(4, SECTION_TITLES[3], lines)


def _details_section(session: TestSession) -> str:
    """Раздел 5: детализация проверок по группам — вердикты, журнал и доказательства."""
    from acceptance.checks import catalog

    lines: list[str] = []
    for group in catalog.groups(implemented_only=True):
        rows = checks_engine.checklist_rows(session, groups=(group.key,))
        lines.append(f"### {group.key} — {group.module}")
        lines.append("")
        lines.append(
            _md_table(
                rows,
                ("check_id", "check_class", "status", "verdict", "journal_range", "ended_at"),
            )
        )
        interesting = [row for row in rows if row["status"] != "не выполнена"]
        for row in interesting:
            lines.append(
                f"* **{row['check_id']}** — {row['status']}: {row['verdict'] or 'без вердикта'}"
            )
            if row["operator_note"]:
                lines.append(f"  * заключение оператора: {row['operator_note']}")
            if row["blocked_by_api"]:
                lines.append(f"  * ожидаемый дефект: {row['blocked_by_api']}")
            if row["evidence"]:
                lines.append("  * доказательства:")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(row["evidence"], ensure_ascii=False, indent=2, default=str))
                lines.append("```")
        lines.append("")
    return _section(5, SECTION_TITLES[4], lines)


def _tasks_section(session: TestSession) -> str:
    """Раздел 6: наблюдение за задачами — типы, происхождение и история переходов FSM-1."""
    tasks = list(session.tasks or [])
    if not tasks:
        return _section(6, SECTION_TITLES[5], ["Наблюдаемых задач нет."])
    rows = [
        {
            "task_id": task.get("task_id"),
            "type": task.get("type"),
            "name": task.get("name"),
            "origin": task.get("origin"),
            "status": task.get("status"),
            "check_id": task.get("check_id"),
            "first_seen": task.get("first_seen"),
            "last_seen": task.get("last_seen"),
            "polls": (task.get("poll") or {}).get("polls"),
        }
        for task in tasks
    ]
    lines = [_md_table(rows, list(rows[0].keys()))]
    for task in tasks:
        # история FSM-1 хранится в сессии парами `previous` → `status` (см. `session.update_task`)
        chain = [
            f"{item.get('at')}: {item.get('previous') or '—'} → {item.get('status') or '—'}"
            + (f" ({item['note']})" if item.get("note") else "")
            for item in (task.get("history") or [])
        ]
        if chain:
            lines.append(f"**История FSM-1 задачи `{task.get('task_id')}`:**")
            for step in chain:
                lines.append(f"* {step}")
            lines.append("")
    return _section(6, SECTION_TITLES[5], lines)


def _datasets_section(session: TestSession) -> str:
    """Раздел 7: датасеты и учёт состава (`acceptance.dataset_composition`, замечание P1)."""
    compositions = composition_store(session)
    if not compositions:
        return _section(
            7,
            SECTION_TITLES[6],
            [
                "Состав датасетов в этой сессии не фиксировался.",
                "",
                "Учёт состава ведёт `acceptance.dataset_composition`: он включается, когда на "
                "стенде выполнялись проверки `TC-DS-01…07` или наполнение с экрана «Датасеты».",
            ],
        )
    overlap = overlap_report(compositions)
    rows = composition_rows(compositions)
    lines = [
        _md_table(
            rows,
            ("dataset_id", "name", "source", "files", "raw", "markup", "size", "task_id"),
        ),
        "",
        "**Источник состава (P1):** серверного состава и агрегатов в API нет, поэтому состав "
        "ведётся локально: `local` — факт наполнения пультом, `heuristic` — предположение по "
        "реестру файлов (требует подтверждения), `server` — состав от сервера (когда появится "
        "операция состава), `unknown` — данных нет.",
        "",
        "**Косвенные признаки дублей (вывод вероятностный):**",
        f"* записей состава: {overlap['totals']['compositions']}; датасетов: "
        f"{overlap['totals']['datasets']}; файлов в составах: "
        f"{overlap['totals']['files_in_compositions']}",
    ]
    lines.extend(f"* {hypothesis}" for hypothesis in overlap["hypotheses"])
    cross = overlap["cross_dataset"]
    if cross:
        lines.append("")
        lines.append("**Файлы, входящие в несколько датасетов:**")
        lines.append(_md_table(cross[:10], ("file_id", "file_name", "datasets")))
    repeats = overlap["repeat_uploads"]
    if repeats:
        lines.append("")
        lines.append("**Повторные загрузки «имя + размер» с разными `id`:**")
        lines.append(_md_table(repeats[:10], ("file_name", "size", "file_ids", "datasets")))
    return _section(7, SECTION_TITLES[6], lines)


def _artifacts_section(session: TestSession) -> str:
    """Раздел 8: артефакты испытаний и учёт `__TEST__`-сущностей (NFR-T4, FR-T10)."""
    artifacts = list(session.artifacts or [])
    entities = test_entities(session)
    pending = pending_test_entities(session)
    lines = [
        "**Артефакты (доказательства):**",
        "",
        _md_table(
            [
                {
                    "kind": item.get("kind"),
                    "name": item.get("name"),
                    "size_bytes": item.get("size_bytes"),
                    "at": item.get("at"),
                    "note": item.get("note"),
                }
                for item in artifacts
            ],
            ("kind", "name", "size_bytes", "at", "note"),
        ),
        "**`__TEST__`-сущности (создано/удалено):**",
        "",
        _md_table(entities, ENTITY_COLUMNS),
    ]
    if pending:
        lines.append("> ⚠️ Не удалены: " + ", ".join(str(item.get("id")) for item in pending) + ".")
    else:
        lines.append("Самоочистка выполнена: созданных и не удалённых сущностей нет.")
    return _section(8, SECTION_TITLES[7], lines)


def _notes_section(session: TestSession) -> str:
    """Раздел 9: замечания к API по приоритетам P0/P1/P2 (FR-T7)."""
    grouped = notes_api.notes_by_priority(session.notes)
    lines: list[str] = []
    for priority in notes_api.PRIORITIES:
        items = grouped.get(priority, [])
        hint = notes_api.PRIORITY_HINTS.get(priority, "")
        lines.append(f"### {priority} — {hint} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("_замечаний нет_")
            lines.append("")
            continue
        for note in items:
            lines.append(f"* **{note.title}** · модуль: {note.module} · источник: {note.source}")
            if note.status:
                status = note.status + (
                    f" ({note.status_note})" if str(note.status_note or "").strip() else ""
                )
                lines.append(f"  * статус: {status}")
            if note.endpoint:
                lines.append(f"  * эндпоинт: `{note.endpoint}`")
            if note.check_id:
                lines.append(f"  * проверка: `{note.check_id}`")
            if note.fact:
                lines.append(f"  * факт: {note.fact}")
            if note.expected:
                lines.append(f"  * ожидание: {note.expected}")
            if note.reproduction:
                lines.append(f"  * воспроизведение: {note.reproduction}")
            if note.evidence:
                lines.append(f"  * доказательства: {note.evidence}")
        lines.append("")
    return _section(9, SECTION_TITLES[8], lines)


def _prospective_section() -> str:
    """Раздел 10: перспективные требования к API (бэклог, не блокирует приёмку)."""
    requirements = notes_api.prospective_requirements()
    lines = [
        _md_table(
            [
                {
                    "priority": item.get("priority"),
                    "module": item.get("module"),
                    "title": item.get("title"),
                    "detail": item.get("detail"),
                }
                for item in requirements
            ],
            ("priority", "module", "title", "detail"),
        )
    ]
    return _section(10, SECTION_TITLES[9], lines)


def _conclusion_section(session: TestSession, ready: Mapping[str, Any]) -> str:
    """Раздел 11: выводы, итоговое решение сессии и подписи."""
    stats = ready["checks"]
    recommendation = "не определён"
    if stats["not_run"] == 0:
        if stats["failed"]:
            recommendation = "не годен (есть отказы проверок)"
        elif stats["blocked"] or ready["notes"]["total"]:
            recommendation = "годен с замечаниями"
        else:
            recommendation = "годен"
    lines = [
        f"* Выполнено проверок: **{stats['done']} из {stats['implemented']}**, "
        f"успех — {stats['passed']}, отказ — {stats['failed']}, "
        f"блокировано API — {stats['blocked']}, не выполнено — {stats['not_run']}.",
        f"* Замечаний к API: {ready['notes']['total']} "
        f"(P0 — {ready['notes']['p0']}, P1 — {ready['notes']['p1']}, P2 — {ready['notes']['p2']}).",
        f"* Наблюдаемых задач: {ready['tasks']['observed']}; "
        f"артефактов: {ready['artifacts']['total']}; "
        f"тестовых сущностей: {ready['test_entities']['total']} "
        f"(не удалено — {ready['test_entities']['pending']}).",
        f"* Рекомендация пульта по результатам сессии: **{recommendation}**.",
        f"* Итоговое решение сессии (по критериям испытаний): "
        f"**{session.info.conclusion.strip() or 'не зафиксировано'}**.",
        "",
        "**Критерии оценки (из программы испытаний).** " + (session.info.criteria.strip() or "—"),
        "",
        "**Подписи.**",
        "",
        session.info.signatures.strip() or "—",
    ]
    if ready["warnings"]:
        lines.append("")
        lines.append("**Предупреждения о полноте отчёта:**")
        for warning in ready["warnings"]:
            lines.append(f"* {warning}")
    return _section(11, SECTION_TITLES[10], lines)


def _annexes_section(annexes: Mapping[str, str]) -> str:
    """Раздел 13: приложения комплекта отчёта (файлы, которые сохраняются рядом)."""
    rows = [
        {
            "annex": name,
            "lines": content.count("\n") or 1,
            "size_bytes": len(content.encode("utf-8")),
        }
        for name, content in annexes.items()
    ]
    return _section(12, SECTION_TITLES[11], [_md_table(rows, ("annex", "lines", "size_bytes"))])


def _group_summary(session: TestSession) -> list[dict[str, Any]]:
    """Сводка по группам чек-листа: всего, выполнено, успех, отказ, блокировано."""
    from acceptance.checks import catalog  # локальный импорт: каталог ссылается на реестр

    rows: list[dict[str, Any]] = []
    for group in catalog.groups(implemented_only=True):
        ids = [spec.check_id for spec in group.checks]
        stats = checks_engine.group_stats(session, ids)
        rows.append(
            {
                "group": group.key,
                "title": group.title,
                "stage": group.stage,
                "total": stats["total"],
                "done": stats["done"],
                "passed": stats["passed"],
                "failed": stats["failed"],
                "blocked": stats["blocked"],
                "not_run": stats["not_run"],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Приложения комплекта (CSV/JSONL)
# ---------------------------------------------------------------------------
#: Колонки приложения «Замечания к API» (совпадают с выгрузкой экрана замечаний).
NOTE_COLUMNS: tuple[str, ...] = (
    "note_id",
    "priority",
    "status",
    "module",
    "title",
    "endpoint",
    "check_id",
    "source",
    "created_at",
    "fact",
    "expected",
    "reproduction",
    "evidence",
)


def _csv(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> str:
    """CSV-текст приложения (Excel открывает `utf-8-sig` — кодировку ставит `save`)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row.get(column)) for column in columns})
    return buffer.getvalue()


def _csv_value(value: Any) -> Any:
    """Значение ячейки CSV: None превращается в пустую строку (не `None`)."""
    return "" if value is None else value


def checks_csv(session: TestSession) -> str:
    """Приложение «Таблица проверок»: все проверки каталога с вердиктами и журналом."""
    rows = checks_engine.checklist_rows(session)
    return _csv(CHECK_COLUMNS, [{**row, "class": row["check_class"]} for row in rows])


def notes_csv(session: TestSession) -> str:
    """Приложение «Замечания к API»: замечания сессии в порядке приоритетов."""
    return _csv(NOTE_COLUMNS, [note.to_dict() for note in notes_api.sorted_notes(session.notes)])


def entities_csv(session: TestSession) -> str:
    """Приложение «`__TEST__`-сущности»: что создано проверками и что удалено (NFR-T4)."""
    return _csv(ENTITY_COLUMNS, test_entities(session))


def manifest_annexes(session: TestSession) -> tuple[dict[str, str], list[str]]:
    """Приложения «Манифест записей»: выгруженные в артефакты файлы манифеста (FR-T8).

    Манифест записи (RAW + markup) сохраняет экран «Записи»
    (`records.save_manifest` → `artifacts/records_manifest_*.csv|json`), поэтому в
    комплект отчёта он попадает как готовый файл — сервер при этом не опрашивается.

    Returns:
        Пары «имя приложения → текст файла» и предупреждения о нечитаемых файлах.
    """
    annexes: dict[str, str] = {}
    warnings: list[str] = []
    for item in session.artifacts or []:
        kind = str(item.get("kind") or "")
        if not kind.startswith(MANIFEST_KIND_PREFIX):
            continue
        path = Path(str(item.get("path") or ""))
        try:
            annexes[path.name] = path.read_text(encoding="utf-8-sig")
        except OSError:
            warnings.append(f"манифест записей не прочитан: {path.as_posix()}")
    return annexes, warnings


def journal_annex(
    *, journal_records: Sequence[Any] | None = None, journal_text: str | None = None
) -> str:
    """Приложение «Выдержка журнала»: JSONL-записи обмена с испытуемым сервером.

    Источник — структурный журнал сессии (`logs/session_<id>.jsonl`) либо записи журнала
    в памяти (`Journal.records`): оба дают один и тот же формат JSONL.
    """
    if journal_text:
        return journal_text if journal_text.endswith("\n") else journal_text + "\n"
    if not journal_records:
        return ""
    lines: list[str] = []
    for record in journal_records:
        payload = record.as_dict() if hasattr(record, "as_dict") else dict(record)
        lines.append(json.dumps(payload, ensure_ascii=False, default=str))
    return "\n".join(lines) + "\n"


def journal_labels(journal_records: Sequence[Any] | None = None) -> dict[str, int]:
    """Сводка журнала по меткам проверок (сколько запросов у каждой проверки)."""
    counts: dict[str, int] = {}
    for record in journal_records or ():
        label = str(getattr(record, "label", "") or "")
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
    return counts


def build(
    session: TestSession,
    *,
    journal_records: Sequence[Any] | None = None,
    journal_text: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> ReportBundle:
    """Собирает комплект отчёта по данным сессии и журнала (без запросов к серверу).

    Args:
        session: сессия испытаний.
        journal_records: записи журнала обмена (`Journal.records`) — для приложения JSONL.
        journal_text: готовый текст структурного журнала сессии (альтернатива записям).
        meta: дополнительная шапка комплекта (путь журнала, каталог выгрузки, оператор).

    Returns:
        `ReportBundle` с текстами markdown/JSON, приложениями и предупреждениями о полноте.
    """
    generated_at = now_iso()
    ready = readiness(session)
    comparison = _snapshot_comparison(session)
    groups = _group_summary(session)

    annexes: dict[str, str] = {
        "checks.csv": checks_csv(session),
        "notes.csv": notes_csv(session),
        "test_entities.csv": entities_csv(session),
    }
    manifests, manifest_warnings = manifest_annexes(session)
    annexes.update(manifests)
    if manifest_warnings:
        ready["warnings"] = [*ready["warnings"], *manifest_warnings]
        ready["ready"] = False
    journal = journal_annex(journal_records=journal_records, journal_text=journal_text)
    if journal:
        annexes["journal.jsonl"] = journal

    header_lines = [
        "# Протокол испытаний сервера обучения",
        "",
        f"*Отчёт сформирован {generated_at} пультом испытаний "
        f"{session.tool_version.get('tool_version', '—')} "
        f"(commit {session.tool_version.get('git_commit', '—')}).*",
        "",
    ]
    if meta:
        header_lines.extend(f"* {key}: {value}" for key, value in meta.items())
        header_lines.append("")
    if ready["warnings"]:
        header_lines.append("> ⚠️ **Отчёт неполон:**")
        header_lines.extend(f"> * {warning}" for warning in ready["warnings"])
        header_lines.append("")

    sections = [
        _session_section(session, ready),
        _program_section(session),
        _snapshots_section(session, comparison),
        _summary_section(session, groups),
        _details_section(session),
        _tasks_section(session),
        _datasets_section(session),
        _artifacts_section(session),
        _notes_section(session),
        _prospective_section(),
        _conclusion_section(session, ready),
        _annexes_section(annexes),
    ]
    markdown = "\n".join([*header_lines, *sections]).rstrip() + "\n"

    payload: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generated_at": generated_at,
        "meta": dict(meta or {}),
        "session": {
            "session_id": session.session_id,
            "status": session.status,
            "base_url": session.base_url,
            "schema_version": session.schema_version,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "duration_seconds": session.duration_seconds,
            "server_version": session.server_version,
            "server_build": session.server_build,
            "tool_version": session.tool_version,
            "info": session.info.to_dict(),
            "logs": session.logs,
        },
        "readiness": ready,
        "checks": checks_engine.checklist_rows(session),
        "groups": groups,
        "snapshots": comparison,
        "tasks": list(session.tasks or []),
        "datasets": {
            "compositions": [item.to_dict() for item in composition_store(session)],
            "overlap": overlap_report(composition_store(session)),
        },
        "console_calls": list(session.console_calls or []),
        "notes": [note.to_dict() for note in notes_api.sorted_notes(session.notes)],
        "prospective_requirements": notes_api.prospective_requirements(),
        "artifacts": list(session.artifacts or []),
        "test_entities": test_entities(session),
        "markup_stats": dict(session.markup_stats),
        "history": list(session.history or []),
        "journal_labels": journal_labels(journal_records),
        "annexes": sorted(annexes),
        "warnings": ready["warnings"],
    }
    return ReportBundle(
        session_id=session.session_id,
        generated_at=generated_at,
        markdown=markdown,
        payload=payload,
        annexes=annexes,
        warnings=tuple(ready["warnings"]),
    )


def report_markdown(session: TestSession, **kwargs: Any) -> str:
    """Markdown-отчёт одной функцией (для тестов и выгрузок)."""
    return build(session, **kwargs).markdown


def report_json(session: TestSession, **kwargs: Any) -> str:
    """JSON-отчёт одной функцией (машиночитаемая копия комплекта)."""
    return build(session, **kwargs).json_text()


def save_report(
    session: TestSession, *, meta: Mapping[str, Any] | None = None, **kwargs: Any
) -> dict[str, Path]:
    """Формирует отчёт и сохраняет комплект в `acceptance_data/reports`.

    Returns:
        Словарь «роль файла → путь» (`report_md`, `report_json` и приложения).
    """
    ensure_dirs()
    return build(session, meta=meta, **kwargs).save(REPORT_DIR)
