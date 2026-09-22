"""`SCR-404` Отчёт испытаний — Результаты (Ф4).

Макет экрана — `docs/16`, раздел «SCR-404. Отчёт испытаний»; требования ТЗ: `FR-P-45…FR-P-56`,
`DR-P-7`.

Назначение: собрать комплект испытаний — что я передаю заказчику. Экран показывает шапку
сессии (сборка, адрес, период, версия пульта), готовность с перечнем недостающего, состав
разделов, файлы комплекта, контроль `__TEST__`-сущностей и итоговое решение с подписями.

Чего экран **не делает** (anti-goals макета): не опрашивает стенд при формировании — комплект
строится **только** по данным сессии и журнала (`report.build`), поэтому он воспроизводим; и не
подменяет протокол — протокол остаётся приложением отчёта (`SCR-401`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import streamlit as st

from acceptance import report as report_api
from acceptance import session as session_api
from acceptance.paths import REPORT_DIR, ensure_dirs
from acceptance.session import TestSession
from acceptance.ui import state
from acceptance.ui.components import flash, layout
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr404_report"

#: Куда ведут действия экрана (команды запуска среди них нет — `IR-P-4`).
PROTOCOL_SCREEN = "scr401_protocol"
NOTES_SCREEN = "scr403_notes"
OVERVIEW_SCREEN = "scr201_overview"
COMPARE_SCREEN = "scr405_compare"
DATA_SCREEN = "scr204_data"

#: Пометки полноты пункта готовности (`FR-P-48`).
OK = "✅"
MISSING = "⛔"

#: Итоговые решения сессии (`session.CONCLUSIONS`, `FR-P-51`).
CONCLUSIONS = session_api.CONCLUSIONS

#: Ссылки на журнал запуска пульта в шапке отчёта (`FR-P-53`).
LOG_KEYS = ("app_log", "session_log")

#: Сколько строк markdown показывать в предпросмотре отчёта.
PREVIEW_LINES = 60

#: Содержимое экрана: блоки, состояния и переходы макета (`docs/16`).
CONTENT = ScreenContent(
    purpose="Собрать отчёт: реквизиты, готовность, разделы, комплект артефактов и решение.",
    blocks=(
        "Шапка отчёта: реквизиты сессии, сборка стенда, программа и её ревизия",
        "Готовность отчёта: чего не хватает для подписания",
        "Разделы отчёта: программа и объём, результаты, замечания, снимки состояния",
        "Комплект артефактов сессии",
        "Решение по сессии и передача отчёта",
    ),
    states=(
        "Не готов: перечень недостающего (снимок «Окончание», замечания, реквизиты)",
        "Готов: сохранение комплекта в артефакты сессии",
    ),
    transitions=(
        ("SCR-401", "Протокол проверок"),
        ("SCR-403", "Замечания к API"),
        ("SCR-201", "Обзор испытаний"),
    ),
    requirements=(
        "FR-P-45…FR-P-56",
        "DR-P-7",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def session_header(session: TestSession) -> str:
    """Шапка отчёта: сессия, сборка стенда, период, версия пульта и уровень журнала (`FR-P-53`)."""
    return layout.join_parts(
        f"Сессия {session.session_id}",
        session.server_build or "сборка не зафиксирована",
        f"адрес {session.base_url}" if session.base_url else "",
        layout.join_parts(
            layout.short_time(session.started_at),
            layout.short_time(session.ended_at) or "не завершена",
        ),
        f"пульт {session.tool_version.get('tool_version') or '—'}",
        f"журнал {session.logs.get('level')}" if session.logs.get("level") else "",
        f"статус {session.status}",
    )


def log_rows(session: TestSession) -> list[dict[str, Any]]:
    """Файлы журналов сессии: что и где лежит — трассируемость комплекта (`FR-P-53`)."""
    return [
        {"log": key, "path": str(session.logs.get(key) or "")}
        for key in LOG_KEYS
        if session.logs.get(key)
    ]


def readiness_criteria(session: TestSession) -> list[dict[str, Any]]:
    """Пункты полноты отчёта: реквизиты, снимки, проверки, замечания, решение (`FR-P-48`).

    Значения берутся и из `report.readiness` (проверки и замечания), и из сессии (снимки,
    итоговое решение): отчёт формируется ядром, а экран объясняет оператору, чего не хватает.
    """
    ready = report_api.readiness(session)
    stats = ready["checks"]
    notes = ready["notes"]
    criteria = [
        (
            "реквизиты сессии (наименование и объект испытаний)",
            bool(ready["session"]["filled"]),
            "заполняются на `SCR-203`",
        ),
        (
            "снимок стенда «начало»",
            bool(session.snapshots.get(session_api.SNAP_START)),
            "снимается на `SCR-202`",
        ),
        (
            "снимок стенда «окончание»",
            bool(session.snapshots.get(session_api.SNAP_END)),
            "снимается на `SCR-202` перед закрытием сессии",
        ),
        (
            "выполнение проверок",
            not stats["not_run"],
            f"выполнено {stats['done']} из {stats['implemented']}",
        ),
        (
            "разбор отказов и блокировок",
            not stats["failed"] and not stats["blocked"],
            f"отказы {stats['failed']}, блокировано {stats['blocked']}",
        ),
        (
            "замечания к API без пробелов",
            not notes["incomplete"],
            f"без воспроизведения {notes['incomplete']} из {notes['total']}",
        ),
        (
            "итоговое решение сессии",
            bool(str(session.info.conclusion or "").strip()),
            f"решение: {session.info.conclusion or 'не зафиксировано'}",
        ),
    ]
    return [
        {
            "criterion": name,
            "state": OK if passed else MISSING,
            "note": note,
            "passed": bool(passed),
        }
        for name, passed, note in criteria
    ]


def readiness_percent(criteria: Sequence[Mapping[str, Any]]) -> int:
    """Готовность отчёта в процентах: доля выполненных пунктов полноты (`FR-P-48`)."""
    if not criteria:
        return 0
    passed = sum(1 for item in criteria if item["passed"])
    return round(passed * 100 / len(criteria))


def readiness_note(criteria: Sequence[Mapping[str, Any]]) -> str:
    """Подпись готовности: «Готовность отчёта: 71 % (5 из 7)» (`FR-P-48`)."""
    passed = sum(1 for item in criteria if item["passed"])
    return f"Готовность отчёта: {readiness_percent(criteria)} % ({passed} из {len(criteria)})"


def section_rows(annexes: Sequence[str]) -> list[dict[str, Any]]:
    """Состав комплекта: разделы отчёта по номерам и приложения (`FR-P-50`)."""
    rows = [
        {"number": index, "section": title, "kind": "раздел"}
        for index, title in enumerate(report_api.SECTION_TITLES, start=1)
    ]
    rows.extend({"number": "", "section": name, "kind": "приложение"} for name in sorted(annexes))
    return rows


def annex_rows(paths: Mapping[str, Path]) -> list[dict[str, Any]]:
    """Файлы сохранённого комплекта: роль, имя и путь (`FR-P-49`)."""
    return [
        {"role": role, "name": path.name, "path": path.as_posix()}
        for role, path in sorted(paths.items())
    ]


def pending_rows(session: TestSession) -> list[dict[str, Any]]:
    """`__TEST__`-сущности, которые проверки создали и не удалили (`FR-P-46`, `NFR-T4`)."""
    return [
        {
            "id": str(item.get("id") or ""),
            "type": str(item.get("type") or ""),
            "check_id": str(item.get("check_id") or ""),
            "at": layout.short_time(item.get("at")),
        }
        for item in session_api.pending_test_entities(session)
    ]


def solution_note(session: TestSession) -> str:
    """Итоговое решение и подписи одной строкой (`FR-P-51`)."""
    return layout.join_parts(
        f"решение: {session.info.conclusion or 'не зафиксировано'}",
        f"подписи: {session.info.signatures}" if session.info.signatures else "",
        f"оператор: {session.info.operator_fio}" if session.info.operator_fio else "",
    )


# ---------------------------------------------------------------------------
# Экран: шапка, готовность, разделы, комплект и решение
# ---------------------------------------------------------------------------
def render() -> None:
    """Рисует экран отчёта: шапку сессии, готовность, разделы, комплект и решение."""
    session = state.current_session()
    if session is None:
        layout.render_header(KEY, "сессия не выбрана")
        flash.render()
        layout.render_no_session(what="Отчёт собирается только из сессии (`DR-P-7`)")
        return

    criteria = readiness_criteria(session)
    layout.render_header(KEY, session_header(session))
    flash.render()
    st.caption(readiness_note(criteria))

    bundle = report_api.build(session, journal_records=list(state.journal_records()))
    workspace, context = layout.zones()
    with workspace:
        _render_sections(bundle)
        _render_preview(bundle)
        _render_solution(session)
    with context:
        _render_readiness(criteria, session)
        _render_bundle(session, bundle)
        _render_cleanup(session)
        _render_transitions()


def _render_sections(bundle: report_api.ReportBundle) -> None:
    """Состав комплекта: разделы отчёта и приложения (`FR-P-50`)."""
    st.subheader("Разделы комплекта")
    layout.rows_table(
        section_rows(list(bundle.annexes)),
        key=f"{KEY}_sections",
        columns={"number": "№", "section": "Раздел", "kind": "Вид"},
        height=320,
    )


def _render_preview(bundle: report_api.ReportBundle) -> None:
    """Предпросмотр отчёта: начало markdown-комплекта (`FR-P-49`)."""
    st.divider()
    with st.expander("Предпросмотр отчёта (markdown)", expanded=False):
        st.code("\n".join(bundle.markdown.splitlines()[:PREVIEW_LINES]))
        st.caption(
            f"показаны первые {PREVIEW_LINES} строк из "
            f"{len(bundle.markdown.splitlines())}: полный текст — в комплекте"
        )


def _render_readiness(criteria: Sequence[Mapping[str, Any]], session: TestSession) -> None:
    """Готовность отчёта: что выполнено, а что мешает подписанию (`FR-P-48`)."""
    st.subheader(readiness_note(criteria))
    layout.rows_table(
        [
            {"state": item["state"], "criterion": item["criterion"], "note": item["note"]}
            for item in criteria
        ],
        key=f"{KEY}_readiness",
        columns={"state": "", "criterion": "Пункт полноты", "note": "Состояние"},
        height=260,
    )
    for warning in report_api.readiness(session)["warnings"]:
        st.warning(warning)


def _render_bundle(session: TestSession, bundle: report_api.ReportBundle) -> None:
    """Комплект отчёта: `md`, `json`, приложения и сохранение на диск (`FR-P-49`)."""
    st.divider()
    st.subheader("Комплект")
    st.caption(
        layout.join_parts(
            f"версия формата {report_api.REPORT_VERSION}",
            f"сформирован {bundle.generated_at}",
            f"файлов {len(bundle.annexes) + 2}",
            "готов к подписанию" if bundle.is_complete() else "неполон",
        )
    )
    st.download_button(
        "⬇ Комплект (md)",
        data=bundle.markdown,
        file_name=f"{bundle.file_stem}.md",
        mime="text/markdown",
        key=f"{KEY}_md",
    )
    st.download_button(
        "⬇ Комплект (json)",
        data=bundle.json_text(),
        file_name=f"{bundle.file_stem}.json",
        mime="application/json",
        key=f"{KEY}_json",
    )
    st.download_button(
        "⬇ Приложения (csv)",
        data=bundle.annexes.get("checks.csv", ""),
        file_name=f"checks_{session.session_id}.csv",
        mime="text/csv",
        key=f"{KEY}_checks_csv",
    )
    if st.button("💾 Сформировать комплект на диск", key=f"{KEY}_save", width="stretch"):
        _save_bundle(session, bundle)


def _render_solution(session: TestSession) -> None:
    """Итоговое решение и подписи сессии (`FR-P-51`)."""
    st.divider()
    st.subheader("Итоговое решение")
    st.caption(solution_note(session) or "решение не зафиксировано")
    decision = st.selectbox(
        "Решение",
        CONCLUSIONS,
        index=CONCLUSIONS.index(session.info.conclusion)
        if session.info.conclusion in CONCLUSIONS
        else 0,
        key=f"{KEY}_decision",
    )
    reason = st.text_area(
        "Обоснование (обязательно)",
        value=str(session.info.notes or ""),
        key=f"{KEY}_reason",
        height=90,
    )
    operator = st.text_input(
        "Подпись оператора",
        value=str(session.info.operator_fio or ""),
        key=f"{KEY}_operator",
    )
    manager = st.text_input(
        "Подпись руководителя испытаний",
        value=str(session.info.signatures or ""),
        key=f"{KEY}_manager",
    )
    if st.button("Зафиксировать решение", key=f"{KEY}_apply", type="primary", width="stretch"):
        _apply_solution(session, str(decision), str(reason), str(operator), str(manager))


def _render_cleanup(session: TestSession) -> None:
    """Контроль `__TEST__`-сущностей: уборка закрыта или сущности остались (`FR-P-46`)."""
    st.divider()
    st.subheader("Контроль `__TEST__`")
    counts = report_api.readiness(session)["test_entities"]
    st.caption(
        layout.join_parts(
            f"создано сущностей: {counts['total']}",
            f"не удалено: {counts['pending']}",
        )
    )
    rows = pending_rows(session)
    if not rows:
        st.caption("Уборка закрыта: созданные проверками сущности удалены (`NFR-T4`).")
        return
    st.warning("Не удалённые сущности: их надо убрать или оставить доказательством с пометкой.")
    layout.rows_table(
        rows,
        key=f"{KEY}_pending",
        columns={"id": "Сущность", "type": "Тип", "check_id": "Проверка", "at": "Когда"},
        height=180,
    )
    if st.button("→ Данные стенда и сущности (SCR-204)", key=f"{KEY}_goto_data", width="stretch"):
        state.go_to(DATA_SCREEN)


def _render_transitions() -> None:
    """Переходы отчёта: протокол, замечания, обзор и сравнение сессий."""
    st.divider()
    st.subheader("Дальше")
    if st.button("→ Протокол проверок (SCR-401)", key=f"{KEY}_goto_protocol", width="stretch"):
        state.go_to(PROTOCOL_SCREEN)
    if st.button("→ Замечания к API (SCR-403)", key=f"{KEY}_goto_notes", width="stretch"):
        state.go_to(NOTES_SCREEN)
    if st.button("→ Обзор испытаний (SCR-201)", key=f"{KEY}_goto_overview", width="stretch"):
        state.go_to(OVERVIEW_SCREEN)
    if st.button("→ Сравнение сессий (SCR-405)", key=f"{KEY}_goto_compare", width="stretch"):
        state.go_to(COMPARE_SCREEN)


def _save_bundle(session: TestSession, bundle: report_api.ReportBundle) -> None:
    """Сохраняет комплект отчёта в каталог выгрузки и регистрирует файлы сессии (`FR-P-49`)."""
    ensure_dirs()
    written = bundle.save(REPORT_DIR)
    for role, path in written.items():
        session_api.add_artifact(
            session,
            kind=f"report_{role}",
            path=path,
            note=f"комплект отчёта от {bundle.generated_at}",
        )
    state.store_session(session)
    names = ", ".join(sorted(path.name for path in written.values()))
    flash.success(f"Комплект сохранён ({len(written)} файл(ов)): {names}")
    st.rerun()


def _apply_solution(
    session: TestSession,
    decision: str,
    reason: str,
    operator: str,
    manager: str,
) -> None:
    """Фиксирует решение, обоснование и подписи; завершает сессию (`FR-P-51`).

    Без обоснования решение не принимается: в отчёте должно быть видно, почему выбран именно
    такой итог испытаний (`AC-P-21`).
    """
    text = str(reason or "").strip()
    if not text:
        st.error("Решение без обоснования не принимается: обоснование входит в отчёт (`AC-P-21`).")
        return
    session.info.notes = text
    session.info.operator_fio = str(operator or "").strip() or session.info.operator_fio
    session.info.signatures = str(manager or "").strip()
    _close(session, decision)


def _close(session: TestSession, decision: str) -> None:
    """Завершает сессию с итоговым решением (повторная фиксация — с переоткрытием)."""
    if session.status == session_api.STATUS_CLOSED:
        session_api.reopen_session(session)
    session_api.close_session(session, conclusion=decision)
    state.store_session(session)
    flash.success(f"Решение зафиксировано: «{decision}», сессия завершена")
    st.rerun()
