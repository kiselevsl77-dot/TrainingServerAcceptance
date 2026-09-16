"""Работа с реестром замечаний к API в интерфейсе пульта (FR-T7).

Хелпер общий для экранов «Записи (RAW + markup)», «Консоль запросов» и
«Замечания к API»: создание замечания (вручную и из шаблона известного дефекта),
дедупликация по заголовку, фильтры, сводка, таблицы и выгрузка для отчёта
(JSON/CSV/Markdown). Раньше эта логика была зашита в экран «Записи» — вынесена,
чтобы шаблон, тексты сообщений и правила дедупликации были **одни на весь пульт**.

Замечания хранятся в сессии (`TestSession.notes`), поэтому попадают в отчёт
отдельным разделом с группировкой по приоритету P0/P1/P2; «Перспективные
требования» ведутся отдельно и в отчёт идут как бэклог, а не как замечание приёмки.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from acceptance.logging_setup import log_event
from acceptance.notes import (
    PRIORITIES,
    PRIORITY_HINTS,
    SOURCE_OPERATOR,
    ApiNote,
    known_defect_notes,
    new_note,
    prospective_requirements,
    sorted_notes,
)
from acceptance.paths import ARTIFACT_DIR
from acceptance.session import TestSession, add_note_to_session, now_iso
from acceptance.ui import state
from acceptance.ui.common.flash import set_flash

#: Колонки выгрузки замечаний (порядок для CSV/таблиц).
NOTE_COLUMNS = (
    "note_id",
    "priority",
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

#: Заголовок раздела замечаний в отчёте.
REPORT_SECTION_TITLE = "Замечания к API"

NOT_FOUND_TEMPLATE = "Шаблон замечания не найден"


def note_templates() -> list[ApiNote]:
    """Готовые шаблоны известных дефектов API (11 замечаний)."""
    return known_defect_notes()


def template_titles() -> list[str]:
    """Заголовки шаблонов для выбора в интерфейсе."""
    return [note.title for note in note_templates()]


def find_template(title_part: str) -> ApiNote | None:
    """Шаблон известного дефекта по части заголовка."""
    lowered = title_part.strip().lower()
    return next(
        (note for note in note_templates() if lowered in note.title.lower()),
        None,
    )


def note_from_template(
    title_part: str,
    *,
    check_id: str = "",
    evidence: str = "",
    priority: str | None = None,
    module: str | None = None,
) -> ApiNote | None:
    """Готовое замечание по шаблону с привязкой к проверке и доказательствами."""
    template = find_template(title_part)
    if template is None:
        return None
    if check_id:
        template.check_id = check_id
    if evidence:
        template.evidence = evidence
    if priority:
        template.priority = (
            priority.upper() if priority.upper() in PRIORITIES else template.priority
        )
    if module:
        template.module = module
    return template


def manual_note(
    title: str,
    *,
    module: str = "Прочее",
    endpoint: str = "",
    priority: str = "P1",
    fact: str = "",
    expected: str = "",
    reproduction: str = "",
    check_id: str | None = None,
    evidence: str = "",
) -> ApiNote:
    """Замечание, созданное оператором вручную."""
    return new_note(
        title,
        module=module,
        endpoint=endpoint,
        priority=priority,
        fact=fact,
        expected=expected,
        reproduction=reproduction,
        check_id=check_id or None,
        evidence=evidence,
        source=SOURCE_OPERATOR,
    )


# ---------------------------------------------------------------------------
# Сохранение замечаний в сессии
# ---------------------------------------------------------------------------
def create_note(
    note: ApiNote,
    *,
    session: TestSession | None,
    module: str = "notes",
    silent: bool = False,
    rerun: bool = True,
) -> bool:
    """Добавляет замечание в сессию (дедупликация по заголовку) и пишет журнал.

    Returns:
        True, если замечание добавлено; False, если сессия не выбрана или такое
        замечание в ней уже есть (оператор получает соответствующее сообщение).
    """
    log_event(
        "api_note_created",
        f"Замечание к API: {note.title}",
        module=module,
        check_id=note.check_id,
        payload=note.to_dict(),
    )

    added = False
    if session is None:
        if not silent:
            set_flash(
                "warning",
                f"Замечание «{note.title}» создано, но сессия не выбрана — в отчёт не попадёт.",
            )
    else:
        before = len(session.notes)
        add_note_to_session(session, note)
        added = len(session.notes) > before
        if added:
            state.store_session(session)
        if not silent:
            if added:
                set_flash("success", f"Замечание добавлено в сессию: {note.title}")
            else:
                set_flash("info", f"Такое замечание уже есть в сессии: {note.title}")

    if rerun:
        st.rerun()
    return added


def create_note_from_template(
    title_part: str,
    *,
    session: TestSession | None,
    check_id: str = "",
    evidence: str = "",
    module: str = "notes",
    silent: bool = False,
    rerun: bool = True,
) -> bool:
    """Создаёт замечание из шаблона известного дефекта (кнопки на экранах).

    Тексты сообщений совпадают с прежним поведением экрана «Записи», поэтому
    оператор видит одинаковые подсказки на всех экранах пульта.
    """
    template = note_from_template(title_part, check_id=check_id, evidence=evidence)
    if template is None:
        set_flash("error", f"{NOT_FOUND_TEMPLATE}: {title_part}")
        if rerun:
            st.rerun()
        return False
    return create_note(template, session=session, module=module, silent=silent, rerun=rerun)


def remove_note(session: TestSession, note_id: str) -> bool:
    """Удаляет замечание из сессии (ошибочно созданное) и фиксирует это в истории."""
    remaining = [item for item in session.notes if str(item.get("note_id")) != str(note_id)]
    if len(remaining) == len(session.notes):
        return False

    removed = next(item for item in session.notes if str(item.get("note_id")) == str(note_id))
    session.notes = remaining
    session.add_history(
        "api_note_removed",
        f"Замечание к API удалено: {removed.get('title', '')}",
        note_id=str(note_id),
        priority=removed.get("priority"),
    )
    log_event(
        "api_note_removed",
        f"Замечание к API удалено: {removed.get('title', '')}",
        level="WARNING",
        module="notes",
        payload={"note_id": str(note_id), "title": removed.get("title", "")},
    )
    state.store_session(session)
    return True


def update_note(session: TestSession, note: ApiNote) -> bool:
    """Заменяет замечание в сессии по `note_id` (правка формулировки оператором)."""
    index = next(
        (
            position
            for position, item in enumerate(session.notes)
            if str(item.get("note_id")) == str(note.note_id)
        ),
        None,
    )
    if index is None:
        return False

    session.notes[index] = note.to_dict()
    session.add_history(
        "api_note_updated",
        f"Замечание к API уточнено: {note.title}",
        note_id=note.note_id,
        priority=note.priority,
    )
    log_event(
        "api_note_updated",
        f"Замечание к API уточнено: {note.title}",
        module="notes",
        check_id=note.check_id,
        payload=note.to_dict(),
    )
    state.store_session(session)
    return True


# ---------------------------------------------------------------------------
# Чтение, фильтры, сводка, выгрузка (входят в отчёт испытаний)
# ---------------------------------------------------------------------------
def notes_of(session: TestSession | None) -> list[dict[str, Any]]:
    """Замечания сессии в порядке отчёта (приоритет, затем дата создания)."""
    if session is None:
        return []
    items: list[dict[str, Any] | ApiNote] = list(session.notes)
    return [note.to_dict() for note in sorted_notes(items)]


def filter_notes(
    notes: list[dict[str, Any]],
    *,
    priority: str | None = None,
    module: str | None = None,
    source: str | None = None,
    linked: str | None = None,
    search: str = "",
) -> list[dict[str, Any]]:
    """Фильтрует замечания по приоритету, модулю, источнику, привязке и подстроке."""
    needle = search.strip().lower()
    result: list[dict[str, Any]] = []
    for note in notes:
        if priority and note.get("priority") != priority:
            continue
        if module and note.get("module") != module:
            continue
        if source and note.get("source") != source:
            continue
        if linked == "с привязкой к проверке" and not note.get("check_id"):
            continue
        if linked == "без привязки" and note.get("check_id"):
            continue
        if needle:
            haystack = " ".join(
                str(note.get(key, ""))
                for key in (
                    "title",
                    "fact",
                    "expected",
                    "reproduction",
                    "endpoint",
                    "check_id",
                    "evidence",
                )
            ).lower()
            if needle not in haystack:
                continue
        result.append(note)
    return result


def notes_rows(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Строки таблицы замечаний для интерфейса."""
    return [
        {
            "№": index,
            "Приоритет": note.get("priority", ""),
            "Модуль": note.get("module", ""),
            "Замечание": note.get("title", ""),
            "Эндпоинт": note.get("endpoint", ""),
            "Проверка": note.get("check_id") or "",
            "Источник": note.get("source", ""),
            "Создано": note.get("created_at", ""),
            "id": note.get("note_id", ""),
        }
        for index, note in enumerate(notes, start=1)
    ]


def notes_summary(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """Сводка замечаний: всего, по приоритетам, модулям, источникам, привязкам."""
    by_priority = dict.fromkeys(PRIORITIES, 0)
    by_module: dict[str, int] = {}
    by_source: dict[str, int] = {}
    linked = 0
    for note in notes:
        priority = str(note.get("priority", "P1")).upper()
        by_priority[priority] = by_priority.get(priority, 0) + 1
        module = str(note.get("module") or "Прочее")
        by_module[module] = by_module.get(module, 0) + 1
        source = str(note.get("source") or SOURCE_OPERATOR)
        by_source[source] = by_source.get(source, 0) + 1
        if note.get("check_id"):
            linked += 1
    return {
        "total": len(notes),
        "by_priority": by_priority,
        "by_module": by_module,
        "by_source": by_source,
        "linked_to_checks": linked,
        "without_check": len(notes) - linked,
    }


def prospective_rows() -> list[dict[str, str]]:
    """Перспективные требования к API (раздел бэклога в отчёте)."""
    return prospective_requirements()


def priority_hint(priority: str) -> str:
    """Пояснение приоритета для интерфейса и отчёта."""
    return PRIORITY_HINTS.get(priority.upper(), "")


def notes_to_json(notes: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> str:
    """Машинная выгрузка реестра замечаний (приложение к отчёту)."""
    payload = {
        "meta": dict(meta or {}),
        "summary": notes_summary(notes),
        "notes": notes,
        "prospective_requirements": prospective_rows(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def notes_to_csv(notes: list[dict[str, Any]]) -> str:
    """Выгрузка реестра замечаний для Excel (UTF-8 BOM добавляет вызывающий)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(list(NOTE_COLUMNS))
    for note in notes:
        writer.writerow([_cell(note.get(column)) for column in NOTE_COLUMNS])
    return buffer.getvalue()


def notes_to_markdown(
    notes: list[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
    include_details: bool = True,
) -> str:
    """Раздел отчёта «Замечания к API» (группировка P0/P1/P2, факт/ожидание)."""
    summary = notes_summary(notes)
    lines: list[str] = [f"## {REPORT_SECTION_TITLE}", ""]
    if meta:
        lines.append(_meta_line(meta))
    lines.append(
        "Всего замечаний: {total} — P0: {p0}, P1: {p1}, P2: {p2}; привязано к проверкам: "
        "{linked} из {total}.".format(
            total=summary["total"],
            p0=summary["by_priority"].get("P0", 0),
            p1=summary["by_priority"].get("P1", 0),
            p2=summary["by_priority"].get("P2", 0),
            linked=summary["linked_to_checks"],
        )
    )
    lines.append("")

    if not notes:
        lines.append("Замечаний к API нет.")
        lines.append("")
    for priority in PRIORITIES:
        group = [note for note in notes if str(note.get("priority", "")).upper() == priority]
        if not group:
            continue
        lines.append(f"### {priority} — {priority_hint(priority)}")
        lines.append("")
        lines.append("| № | Замечание | Модуль | Эндпоинт | Проверка | Источник |")
        lines.append("|---|---|---|---|---|---|")
        for index, note in enumerate(group, start=1):
            lines.append(
                "| {n} | {title} | {module} | {endpoint} | {check} | {source} |".format(
                    n=index,
                    title=_cell(note.get("title")),
                    module=_cell(note.get("module")),
                    endpoint=_cell(note.get("endpoint")),
                    check=_cell(note.get("check_id")),
                    source=_cell(note.get("source")),
                )
            )
        lines.append("")

        if not include_details:
            continue
        for index, note in enumerate(group, start=1):
            lines.append(f"**{priority}-{index}. {_cell(note.get('title'))}**")
            lines.append("")
            for label, key in (
                ("Модуль", "module"),
                ("Эндпоинт", "endpoint"),
                ("Факт", "fact"),
                ("Ожидание", "expected"),
                ("Воспроизведение", "reproduction"),
                ("Доказательства", "evidence"),
                ("Создано", "created_at"),
            ):
                value = _cell(note.get(key))
                if value:
                    lines.append(f"* **{label}:** {value}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_notes_export(
    notes: list[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
    stamp: str | None = None,
) -> dict[str, Path]:
    """Сохраняет реестр замечаний в `artifacts/` (md, csv, json) для отчёта."""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    resolved_stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    paths = {
        "markdown": ARTIFACT_DIR / f"api_notes_{resolved_stamp}.md",
        "csv": ARTIFACT_DIR / f"api_notes_{resolved_stamp}.csv",
        "json": ARTIFACT_DIR / f"api_notes_{resolved_stamp}.json",
    }
    paths["markdown"].write_text(notes_to_markdown(notes, meta=meta), encoding="utf-8")
    paths["csv"].write_text(notes_to_csv(notes), encoding="utf-8-sig")
    paths["json"].write_text(notes_to_json(notes, meta), encoding="utf-8")
    return paths


def _meta_line(meta: dict[str, Any]) -> str:
    """Строка шапки выгрузки (сессия, сборка, оператор)."""
    parts = [
        f"сессия: {meta.get('session_id') or 'не выбрана'}",
        f"сборка сервера: {meta.get('server_build') or 'не определена'}",
        f"адрес: {meta.get('base_url') or ''}",
        f"оператор: {meta.get('operator') or 'не указан'}",
        f"выгружено: {meta.get('exported_at') or now_iso()}",
    ]
    return " · ".join(part for part in parts if part)


def _cell(value: Any) -> str:
    """Значение поля для CSV/Markdown: одна строка без переводов строк и `|`."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text.replace("|", "\\|")
