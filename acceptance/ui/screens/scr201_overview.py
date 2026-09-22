"""`SCR-201` Обзор испытаний — Подготовка (Ф0–Ф4).

Макет экрана — `docs/16`, раздел «SCR-201. Обзор испытаний»; требования ТЗ: `FR-P-11`,
`FR-P-12`, `FR-P-48`, `IR-P-8`.

Назначение: одна панель, отвечающая на вопрос «где я и что требует внимания» — фаза процесса,
программа и покрытие разделов, прогресс очереди, готовность отчёта, состояние стенда и сводка
обменов, а также панель «Внимание» с открытыми замечаниями.

Чего экран **не делает** (anti-goals макета): не запускает проверки (`IR-P-4`) и не меняет
данные — только показывает положение в процессе и ведёт к нужному экрану. Все значения берутся
из ядра: программа (`programme.py`), очередь и результаты (`queue.py`, `results.py`), готовность
отчёта (`report.readiness`), снимки и реквизиты сессии, журнал обмена.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import streamlit as st

from acceptance import notes as notes_api
from acceptance import report as report_api
from acceptance import session as session_api
from acceptance.session import TestSession
from acceptance.ui import state
from acceptance.ui.components import flash, journal, layout
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr201_overview"

#: Куда ведут действия обзора (команд запуска среди них нет — `IR-P-4`).
SETS_SCREEN = "scr101_sets"
PROGRAMME_SCREEN = "scr102_programme"
STAND_SCREEN = "scr202_stand"
SESSION_SCREEN = "scr203_session"
RUN_SCREEN = "scr301_run"
PROTOCOL_SCREEN = "scr401_protocol"
JOURNAL_SCREEN = "scr402_journal"
NOTES_SCREEN = "scr403_notes"
REPORT_SCREEN = "scr404_report"

#: Фазы бизнес-процесса испытаний (`docs/16` §0): где мы находимся.
PHASES: tuple[tuple[str, str], ...] = (
    ("Ф0", "планирование: наборы и программа"),
    ("Ф1", "подготовка: стенд, сессия, данные"),
    ("Ф2", "прогон и оценка: очередь, протокол"),
    ("Ф3", "разбор: замечания и регресс"),
    ("Ф4", "финализация: отчёт и решение"),
)

#: Пометки состояния фазы.
PHASE_DONE = "✅"
PHASE_CURRENT = "●"
PHASE_TODO = "○"
DONE = "✅"
MISSING = "⛔"

#: Содержимое экрана: блоки, состояния и переходы макета (`docs/16`).
CONTENT = ScreenContent(
    purpose="Одна панель: где я и что требует внимания (фаза, очередь, замечания, отчёт).",
    blocks=(
        "Полоса фаз Ф0–Ф4 и положение в бизнес-процессе",
        "Программа и покрытие разделов, сокращения с обоснованием",
        "Прогресс очереди: выполнено из всего, успех/отказ/блокировано/пропущено",
        "Готовность отчёта: чего не хватает для подписания",
        "Состояние стенда и сборки, сводка обменов за сессию",
        "Панель «Внимание»: открытые замечания P0/P1 и пробелы",
    ),
    states=(
        "Пусто: «Сессия не выбрана: создайте сессию, чтобы пульт вёл протокол»",
        "Загрузка: скелетон блоков, счётчики «…»",
        "Ошибка: «Не удалось прочитать очередь: <причина>. Повторить»",
        "Частичные данные: блок помечен «нет данных от стенда» с номером обмена",
    ),
    transitions=(
        ("SCR-102", "Программа сессии"),
        ("SCR-301", "Прогон"),
        ("SCR-401", "Протокол проверок"),
        ("SCR-404", "Отчёт испытаний"),
        ("SCR-202", "Стенд"),
        ("SCR-403", "Замечания к API"),
    ),
    requirements=(
        "FR-P-11",
        "FR-P-12",
        "FR-P-48",
        "IR-P-8",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def phase_index(session: TestSession | None) -> int:
    """Индекс текущей фазы процесса: Ф0 — планирование, Ф4 — финализация (`FR-P-11`).

    Фаза выводится из состояния сессии, а не хранится отдельно: пульт показывает, что уже
    сделано (сессия, программа, прогон, замечания, решение), а не то, что оператор о себе думает.
    """
    if session is None:
        return 0
    if str(session.info.conclusion or "").strip():
        return 4
    if session.notes:
        return 3
    if session.checks:
        return 2
    if session.started_at or session.snapshots:
        return 1
    return 0


def phase_rows(current: int) -> list[dict[str, Any]]:
    """Полоса фаз: пройденные, текущая и предстоящие (`FR-P-11`)."""
    rows: list[dict[str, Any]] = []
    for index, (code, title) in enumerate(PHASES):
        if index < current:
            mark = PHASE_DONE
        elif index == current:
            mark = PHASE_CURRENT
        else:
            mark = PHASE_TODO
        rows.append({"phase": code, "mark": mark, "what": title})
    return rows


def phase_note(current: int) -> str:
    """Подпись фазы: «Фаза Ф2 · прогон и оценка: очередь, протокол»."""
    code, title = PHASES[min(max(current, 0), len(PHASES) - 1)]
    return f"Фаза {code} · {title}"


def progress_rows(queue_summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Прогресс очереди: сколько пройдено и с каким исходом (`FR-P-12`)."""
    total = int(queue_summary.get("total") or 0)
    done = int(queue_summary.get("done") or 0) + int(queue_summary.get("off") or 0)
    return [
        {
            "what": "выполнено",
            "value": layout.percent(done, total),
            "detail": layout.join_parts(
                f"успех {queue_summary.get('passed', 0)}",
                f"отказ {queue_summary.get('failed', 0)}",
                f"блокировано {queue_summary.get('blocked', 0)}",
                f"снято {queue_summary.get('off', 0)}",
                f"осталось {queue_summary.get('pending', 0)}",
            ),
        }
    ]


def attention_rows(session: TestSession) -> list[dict[str, Any]]:
    """Панель «Внимание»: открытые P0/P1, неполные замечания, снимки, сущности (`FR-P-48`)."""
    ready = report_api.readiness(session)
    notes = notes_api.sorted_notes(session.notes)
    rows: list[dict[str, Any]] = []
    for priority in ("P0", "P1"):
        opened = [
            note
            for note in notes
            if note.priority == priority and note.status != notes_api.NOTE_CLOSED
        ]
        rows.append(
            {
                "what": f"Открытые {priority}",
                "value": len(opened),
                "detail": ", ".join(f"{note.note_id} · {note.title[:40]}" for note in opened[:3]),
                "ok": not opened,
            }
        )
    rows.append(
        {
            "what": "Замечания без воспроизведения",
            "value": ready["notes"]["incomplete"],
            "detail": "нельзя передать разработчику (`FR-P-48`)",
            "ok": ready["notes"]["incomplete"] == 0,
        }
    )
    rows.append(
        {
            "what": "Снимок «Окончание»",
            "value": "есть" if session.snapshots.get(session_api.SNAP_END) else "нет",
            "detail": "снимается на `SCR-202` перед закрытием сессии",
            "ok": bool(session.snapshots.get(session_api.SNAP_END)),
        }
    )
    rows.append(
        {
            "what": "`__TEST__`-сущности не удалены",
            "value": ready["test_entities"]["pending"],
            "detail": "уборка закрывается до подписания (`FR-P-46`)",
            "ok": ready["test_entities"]["pending"] == 0,
        }
    )
    return rows


def attention_note(session: TestSession) -> str:
    """Сводка панели «Внимание»: что требует решения (`FR-P-48`)."""
    waiting = [row for row in attention_rows(session) if not row["ok"]]
    if not waiting:
        return "Внимание: не требуется — все пункты закрыты"
    return "Внимание: " + "; ".join(f"{row['what']} — {row['value']}" for row in waiting)


def coverage_note(programme: Any) -> str:
    """Покрытие разделов: сколько разделов закрыто и что сокращено (`IR-P-19`)."""
    coverage = list(programme.coverage()) if getattr(programme, "size", 0) else []
    if not coverage:
        return "Программа не собрана: наборы и программа — на `SCR-102`"
    covered = [
        row for row in coverage if int(row.get("actual") or 0) >= int(row.get("target") or 0)
    ]
    short = [str(row.get("section")) for row in coverage if row not in covered]
    return layout.join_parts(
        f"Покрытие разделов: {len(covered)} из {len(coverage)}",
        ("сокращено: " + ", ".join(short) + " — обоснование в отчёте") if short else "",
    )


def stand_note(session: TestSession, records: Sequence[Any]) -> str:
    """Состояние стенда и сводка обменов: адрес, сборка и объём работы за сессию (`FR-P-12`)."""
    summary = journal.summary_items(records)
    return layout.join_parts(
        f"стенд: {session.base_url or 'адрес не задан'}",
        session.server_build,
        f"обменов за сессию: {summary['total']}",
        f"ошибок {summary['errors']}" if summary["errors"] else "",
    )


def report_note(session: TestSession) -> str:
    """Готовность отчёта и чего не хватает для подписания (`FR-P-48`)."""
    ready = report_api.readiness(session)
    if ready["ready"]:
        return "Готовность отчёта: полная — предупреждений нет"
    return "Готовность отчёта: не хватает — " + "; ".join(ready["warnings"][:2])


def session_note(session: TestSession | None) -> str:
    """Шапка обзора: сессия, статус и реквизиты (`IR-P-8`)."""
    if session is None:
        return "сессия не выбрана"
    return layout.join_parts(
        f"Сессия {session.session_id}",
        f"«{session.info.title}»" if session.info.title else "",
        session.server_build,
        f"статус {session.status}",
        "реквизиты заполнены" if session.info.is_filled else "реквизиты не заполнены",
    )


def status_mark(ok: bool) -> str:
    """Пометка состояния пункта внимания (`IR-P-8`)."""
    return DONE if ok else MISSING


def status_label(ok: bool) -> str:
    """Подпись состояния пункта внимания: «выполнено» / «требует внимания»."""
    return "выполнено" if ok else "требует внимания"


def attention_table(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Строки панели «Внимание» для таблицы экрана."""
    return [
        {
            "mark": status_mark(bool(row["ok"])),
            "what": str(row["what"]),
            "state": status_label(bool(row["ok"])),
            "value": str(row["value"]),
            "detail": str(row["detail"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Экран: фазы, программа, очередь, отчёт, стенд и «Внимание»
# ---------------------------------------------------------------------------
def render() -> None:
    """Рисует обзор: положение в процессе, прогресс, готовность и панель «Внимание»."""
    session = state.current_session()
    current = phase_index(session)
    layout.render_header(KEY, layout.join_parts(phase_note(current), session_note(session)))
    flash.render()

    if session is None:
        st.info(
            "Сессия не выбрана: создайте сессию, чтобы пульт начал вести протокол испытаний (`IR-P-8`)."
        )
        if st.button("→ Сессия испытаний (SCR-203)", key=f"{KEY}_goto_session", type="primary"):
            state.go_to(SESSION_SCREEN)
        return

    programme = state.current_programme()
    queue = state.current_queue()
    records = state.journal_records()
    workspace, context = layout.zones()
    with workspace:
        _render_phases(current)
        _render_programme(programme)
        _render_progress(queue, session)
    with context:
        _render_attention(session)
        _render_stand(session, records)
        _render_transitions()


def _render_phases(current: int) -> None:
    """Полоса фаз процесса: где мы и что уже сделано (`FR-P-11`)."""
    st.subheader(phase_note(current))
    layout.rows_table(
        phase_rows(current),
        key=f"{KEY}_phases",
        columns={"mark": "", "phase": "Фаза", "what": "Что в ней"},
        height=220,
    )


def _render_programme(programme: Any) -> None:
    """Программа сессии: ревизия, объём и покрытие разделов (`IR-P-19`)."""
    st.divider()
    st.subheader("Программа и объём")
    if not getattr(programme, "size", 0):
        st.caption(coverage_note(programme))
        return
    st.caption(
        layout.join_parts(
            f"ревизия {programme.revision}",
            programme.status_label,
            layout.plural(programme.size, ("пункт", "пункта", "пунктов")),
            f"обязательных {len(programme.mandatory_ids)}",
        )
    )
    st.caption(coverage_note(programme))


def _render_progress(queue: Any, session: TestSession) -> None:
    """Прогресс очереди прогона: сколько пройдено и с каким исходом (`FR-P-12`)."""
    st.divider()
    st.subheader("Прогресс очереди")
    summary = queue.summary(state.results_state(queue.check_ids))
    if not summary["total"]:
        st.caption("Очередь не собрана: прогон начинается с программы на `SCR-102`.")
        return
    layout.rows_table(
        progress_rows(summary),
        key=f"{KEY}_progress",
        columns={"what": "Показатель", "value": "Значение", "detail": "Исходы"},
        height=120,
    )
    st.caption(report_note(session))


def _render_attention(session: TestSession) -> None:
    """Панель «Внимание»: открытые P0/P1, неполные замечания, снимки и уборка (`FR-P-48`)."""
    st.subheader("Внимание")
    rows = attention_rows(session)
    if any(not row["ok"] for row in rows):
        st.warning(attention_note(session))
    else:
        st.caption(attention_note(session))
    layout.rows_table(
        attention_table(rows),
        key=f"{KEY}_attention",
        columns={
            "mark": "",
            "what": "Пункт",
            "state": "Состояние",
            "value": "Значение",
            "detail": "Пояснение",
        },
        height=280,
    )


def _render_stand(session: TestSession, records: Sequence[Any]) -> None:
    """Состояние стенда и сводка обменов за сессию (`FR-P-12`)."""
    st.divider()
    st.subheader("Стенд и обмен")
    st.caption(stand_note(session, records))
    if not records:
        st.caption("Обменов ещё не было: пульт обращается к стенду по вашим действиям.")


def _render_transitions() -> None:
    """Переходы обзора: к программе, прогону, протоколу, отчёту и разбору."""
    st.divider()
    st.subheader("Перейти")
    if st.button("→ Программа сессии (SCR-102)", key=f"{KEY}_goto_programme", width="stretch"):
        state.go_to(PROGRAMME_SCREEN)
    if st.button("→ Наборы проверок (SCR-101)", key=f"{KEY}_goto_sets", width="stretch"):
        state.go_to(SETS_SCREEN)
    if st.button("→ К прогону (SCR-301)", key=f"{KEY}_goto_run", type="primary", width="stretch"):
        state.go_to(RUN_SCREEN)
    if st.button("→ Протокол проверок (SCR-401)", key=f"{KEY}_goto_protocol", width="stretch"):
        state.go_to(PROTOCOL_SCREEN)
    if st.button("→ Замечания к API (SCR-403)", key=f"{KEY}_goto_notes", width="stretch"):
        state.go_to(NOTES_SCREEN)
    if st.button("→ Отчёт испытаний (SCR-404)", key=f"{KEY}_goto_report", width="stretch"):
        state.go_to(REPORT_SCREEN)
    if st.button("→ Журнал обмена (SCR-402)", key=f"{KEY}_goto_journal", width="stretch"):
        state.go_to(JOURNAL_SCREEN)
    if st.button("→ Стенд и снимки (SCR-202)", key=f"{KEY}_goto_stand", width="stretch"):
        state.go_to(STAND_SCREEN)
