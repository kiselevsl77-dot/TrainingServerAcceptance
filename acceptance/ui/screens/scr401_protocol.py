"""`SCR-401` Протокол проверок — Результаты (Ф2).

Макет экрана — `docs/16`, раздел «SCR-401. Протокол проверок»; требования ТЗ: `FR-P-30`,
`FR-P-32`, `FR-P-34…FR-P-36`, `FR-P-64`, `DR-P-5`, `IR-P-4`.

Экран сводит результаты испытаний в протокол: таблица со статусом, вердиктом и диапазоном
журнала, KPI выборки и готовность, ручные отметки оператора, снятие пункта с причиной и
выгрузки (CSV и артефакт сессии).

Чего экран **не делает** (anti-goals макета): не запускает проверки — команда запуска одна,
на «Прогоне» (`IR-P-4`), и не правит состав программы (`IR-P-18`): сокращение программы —
решение руководителя на `SCR-102`. Статус берётся из `results.py` (`DR-P-5`) — второй копии
результата на экране нет. Проверки, у которых результат есть, а в программе их нет,
показываются строкой «вне программы» (`FR-P-13`, `FR-P-69`) — из протокола они не исчезают.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from acceptance import results as results_api
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks.registry import CheckStatus
from acceptance.paths import ARTIFACT_DIR, ensure_dirs
from acceptance.programme import Programme
from acceptance.queue import Queue
from acceptance.session import TestSession, add_artifact
from acceptance.ui import state
from acceptance.ui.components import flash, layout, status
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr401_protocol"

#: Экраны, куда ведут действия протокола (`IR-P-4`: запуска здесь нет, только переходы).
CHECK_SCREEN = "scr302_check"
RUN_SCREEN = "scr301_run"
PROGRAMME_SCREEN = "scr102_programme"
JOURNAL_SCREEN = "scr402_journal"
REPORT_SCREEN = "scr404_report"
COMPARE_SCREEN = "scr405_compare"

#: Пометка для результатов, которых нет в программе сессии (`FR-P-13`, `FR-P-69`).
OUTSIDE_PROGRAMME = "вне программы"

#: Пустая ячейка таблицы протокола.
DASH = "—"

#: Значение фильтров «без ограничения».
ANY = "все"

#: Место проверки в программе: колонка и фильтр (`FR-P-35` — снятые видны раздельно).
IN_PROGRAMME = "в программе"
SUSPENDED = "снято с причиной"
PROGRAMME_CHOICES: tuple[str, ...] = (ANY, IN_PROGRAMME, OUTSIDE_PROGRAMME, SUSPENDED)

#: Статусы, которые ставит человек: у них обязательно заключение (`FR-P-32`, `AC-P-15`).
CONCLUDED_STATUSES: tuple[str, ...] = (
    str(CheckStatus.MANUAL_OK),
    str(CheckStatus.BLOCKED),
    str(CheckStatus.SKIPPED),
    str(CheckStatus.INTERRUPTED),
)

#: Пометка строки, у которой заключения оператора нет (состояние макета «нужно заключение»).
CONCLUSION_MARK = "нужно заключение"

#: Колонки выгрузки протокола (`FR-P-34`: выборка целиком).
CSV_COLUMNS: tuple[str, ...] = (
    "check_id",
    "group",
    "check_class",
    "title",
    "requirement",
    "status",
    "verdict",
    "operator_note",
    "origin",
    "journal_range",
    "at",
    "author",
    "revision",
    "programme",
)

#: Содержимое экрана: блоки, состояния и переходы макета (`docs/16`).
CONTENT = ScreenContent(
    purpose="Свести результаты: таблица проверок с вердиктами, KPI, отметки и выгрузки.",
    blocks=(
        "Фильтры и поиск: группа, статус, класс, «только невыполненные»",
        "Таблица: статус, вердикт, диапазон журнала, набор-источник",
        "KPI выборки и готовность протокола",
        "Ручные отметки: выполнена вручную, пропущена, блокировано, прервана с причиной",
        "Снятие с причиной и признак «вне программы»",
        "Выгрузки: CSV и сохранение выборки в артефакты",
    ),
    states=(
        "Пусто: «Проверки ещё не выполнялись: начните прогон на SCR-301»",
        "Незаполненные заключения: строка помечена «нужно заключение»",
    ),
    transitions=(
        ("SCR-302", "Карточка проверки"),
        ("SCR-404", "Отчёт испытаний"),
        ("SCR-405", "Сравнение сессий"),
    ),
    requirements=(
        "FR-P-30",
        "FR-P-32",
        "FR-P-34…FR-P-36",
        "FR-P-64",
        "DR-P-5",
        "IR-P-4",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def journal_range(row: Mapping[str, Any]) -> str:
    """Диапазон номеров журнала обмена строки результата: `#12–#13` (пусто — обменов нет)."""
    first = row.get("journal_from")
    last = row.get("journal_to")
    if first is None and last is None:
        return ""
    if first is None:
        return f"#{last}"
    if last is None:
        return f"#{first}"
    if first == last:
        return f"#{first}"
    return f"#{first}–#{last}"


def programme_label(programme: Programme, check_id: str) -> str:
    """Место проверки в программе: наборы-источники или «вне программы» (`FR-P-13`)."""
    item = programme.find(check_id)
    if item is None:
        return OUTSIDE_PROGRAMME
    return item.source_note or IN_PROGRAMME


def programme_group(programme: Programme, check_id: str, *, suspended: bool = False) -> str:
    """Признак места проверки для фильтра: в программе, вне программы или снята (`FR-P-35`)."""
    if suspended:
        return SUSPENDED
    return IN_PROGRAMME if programme.find(check_id) is not None else OUTSIDE_PROGRAMME


def needs_conclusion(result: Mapping[str, Any]) -> bool:
    """True, если статус поставлен человеком, а заключения при нём нет (`FR-P-32`)."""
    if not result.get("has_result"):
        return False
    if str(result.get("status") or "") not in CONCLUDED_STATUSES:
        return False
    return not str(result.get("operator_note") or "").strip()


def protocol_rows(session: TestSession, programme: Programme) -> list[dict[str, Any]]:
    """Строки протокола: состав программы плюс результаты вне программы (`FR-P-13`).

    Результат вне программы не теряется: он попадает в протокол строкой с местом
    «вне программы», чтобы обоснование сокращения оставалось проверяемым (`FR-P-69`).
    """
    check_ids = list(programme.check_ids)
    for record in results_api.records_of(session):
        key = str(record.get("check_id") or "").strip().upper()
        if key and key not in check_ids and programme.find(key) is None:
            check_ids.append(key)

    rows: list[dict[str, Any]] = []
    for check_id in check_ids:
        result = results_api.row(session, check_id)
        spec = catalog.find(check_id)
        item = programme.find(check_id)
        suspended = bool(result["suspended"])
        rows.append(
            {
                "check_id": check_id,
                "group": catalog.group_of(check_id) or DASH,
                "check_class": (
                    spec.class_label
                    if spec is not None
                    else (item.check_class if item is not None else "")
                ),
                "title": (
                    spec.title if spec is not None else (item.title if item is not None else "")
                ),
                "requirement": (
                    spec.requirement
                    if spec is not None
                    else (item.requirement if item is not None else "")
                ),
                "status": f"{result['icon']} {result['status']}",
                "status_raw": str(result["status"]),
                "has_result": bool(result["has_result"]),
                "verdict": str(result["verdict"] or result["operator_note"] or DASH),
                "operator_note": str(result["operator_note"]),
                "origin": status.origin_label(str(result["origin"]), suspended=suspended),
                "journal_range": journal_range(result),
                "at": layout.short_time(result["at"]),
                "author": str(result["author"]),
                "revision": "" if result["revision"] is None else str(result["revision"]),
                "programme": programme_label(programme, check_id),
                "programme_group": programme_group(programme, check_id, suspended=suspended),
                "mark": CONCLUSION_MARK if needs_conclusion(result) else "",
                "suspended": suspended,
                "reason": str(result["reason"]),
                "repeats": int(result["repeats"]),
            }
        )
    return rows


def filter_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    group: str = "",
    check_class: str = "",
    status_value: str = "",
    programme_value: str = "",
    search: str = "",
    only_pending: bool = False,
) -> list[dict[str, Any]]:
    """Выборка протокола по фильтрам макета (`FR-P-30`).

    Пустое значение фильтра (или `ANY`) — без ограничения. Поиск идёт по идентификатору,
    названию и требованию проверки: так пункт находят и по номеру, и по названию.
    """
    needle = str(search or "").strip().lower()
    selected: list[dict[str, Any]] = []
    for row in rows:
        if group and group != ANY and str(row["group"]) != group:
            continue
        if check_class and check_class != ANY and str(row["check_class"]) != check_class:
            continue
        if status_value and status_value != ANY and str(row["status_raw"]) != status_value:
            continue
        if programme_value and programme_value != ANY:
            if str(row["programme_group"]) != programme_value:
                continue
        if only_pending and row["has_result"]:
            continue
        if needle:
            haystack = f"{row['check_id']} {row['title']} {row['requirement']}".lower()
            if needle not in haystack:
                continue
        selected.append(dict(row))
    return selected


def group_options(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Варианты фильтра по группе каталога: «все» и группы выборки по алфавиту."""
    return (ANY, *sorted({str(row["group"]) for row in rows}))


def class_options(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Варианты фильтра по классу проверки: «все» и классы выборки по алфавиту."""
    return (ANY, *sorted({str(row["check_class"]) for row in rows if row["check_class"]}))


def status_filter_options(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Варианты фильтра по статусу: «все» и статусы выборки, «плохие» — первыми."""
    present = {str(row["status_raw"]) for row in rows}
    return (ANY, *[item for item in status.status_labels() if item in present])


def kpi_items(rows: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, Any], ...]:
    """KPI выборки: объём, выполнение, исходы и незаполненные заключения."""
    return (
        ("В выборке", len(rows)),
        ("Выполнено", sum(1 for row in rows if row["has_result"])),
        ("Успех", _count(rows, str(CheckStatus.PASSED))),
        ("Отказ", _count(rows, str(CheckStatus.FAILED))),
        ("Блокировано", _count(rows, str(CheckStatus.BLOCKED))),
        ("Снято", sum(1 for row in rows if row["suspended"])),
        ("Не выполнено", _count(rows, results_api.STATUS_NOT_RUN)),
        ("Нужно заключение", sum(1 for row in rows if row["mark"])),
    )


def readiness_note(rows: Sequence[Mapping[str, Any]]) -> str:
    """Готовность протокола: сколько пунктов выборки выполнено (`FR-P-64`)."""
    done = sum(1 for row in rows if row["has_result"])
    return f"Готовность протокола: {layout.percent(done, len(rows))}"


def conclusion_note(rows: Sequence[Mapping[str, Any]]) -> str:
    """Что ждёт заключения оператора (`FR-P-32`); пусто — у всех строк заключение есть."""
    waiting = [row for row in rows if row["mark"]]
    if not waiting:
        return ""
    listed = ", ".join(str(row["check_id"]) for row in waiting[:5])
    return (
        f"{CONCLUSION_MARK}: "
        f"{layout.plural(len(waiting), ('пункт', 'пункта', 'пунктов'))} — {listed}"
        + ("…" if len(waiting) > 5 else "")
    )


def protocol_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """CSV выборки протокола: колонки `CSV_COLUMNS`, порядок выборки сохранён."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    return buffer.getvalue()


def export_name(session: TestSession) -> str:
    """Имя файла выгрузки протокола (`FR-P-34`): `protocol_<сессия>.csv`."""
    return f"protocol_{session.session_id}.csv"


def save_selection(session: TestSession, rows: Sequence[Mapping[str, Any]]) -> Path:
    """Сохраняет выборку протокола в `artifacts/` и регистрирует её в сессии (`FR-P-64`)."""
    ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = ARTIFACT_DIR / f"protocol_{session.session_id}_{stamp}.csv"
    path.write_text(protocol_csv(rows), encoding="utf-8-sig")
    add_artifact(
        session,
        kind="protocol_csv",
        path=path,
        note=f"протокол проверок: {len(rows)} строк выборки",
    )
    return path


def mark_payload(check_id: str, status_value: str, note: str) -> dict[str, Any]:
    """Payload ручной отметки: статус и заключение оператора (`FR-P-32`)."""
    return {
        "check_id": str(check_id).strip().upper(),
        "status": str(status_value),
        "verdict": str(note),
        "operator_note": str(note),
    }


def _count(rows: Sequence[Mapping[str, Any]], status_value: str) -> int:
    """Сколько строк выборки имеют указанный статус."""
    return sum(1 for row in rows if str(row["status_raw"]) == status_value)


# ---------------------------------------------------------------------------
# Экран: фильтры, таблица, выгрузки, отметки и панель KPI
# ---------------------------------------------------------------------------
def render() -> None:
    """Рисует протокол: выборку, выгрузки, отметки оператора, снятие и KPI."""
    session = state.current_session()
    if session is None:
        layout.render_header(KEY, "сессия не выбрана")
        flash.render()
        layout.render_no_session(what="Протокол собирается только из результатов сессии (`DR-P-5`)")
        return

    programme = state.current_programme()
    queue = state.current_queue()
    rows = protocol_rows(session, programme)
    subtitle = layout.join_parts(
        (
            f"программа: ревизия {programme.revision} ({programme.status_label})"
            if programme.size
            else "программа не собрана"
        ),
        f"пунктов в протоколе: {len(rows)}",
    )
    layout.render_header(KEY, subtitle)
    flash.render()

    if not rows:
        st.warning(
            "Проверки ещё не выполнялись: начните прогон на SCR-301 — программа сессии пока пуста."
        )
        if st.button("→ К прогону (SCR-301)", key=f"{KEY}_goto_run", type="primary"):
            state.go_to(RUN_SCREEN)
        return

    workspace, context = layout.zones()
    with workspace:
        filtered = _render_filters(rows)
        _render_table(filtered)
        _render_exports(session, filtered)
        _render_marks(session, filtered)
        _render_suspension(session, queue, rows)
    with context:
        _render_context(programme, rows, filtered)


def _render_filters(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Фильтры выборки протокола: группа, класс, статус, место и поиск (`FR-P-30`)."""
    st.subheader(f"Протокол ({len(rows)})")
    left, right = st.columns(2)
    group = left.selectbox("Группа", group_options(rows), key=f"{KEY}_group")
    check_class = right.selectbox("Класс", class_options(rows), key=f"{KEY}_class")
    status_value = left.selectbox("Статус", status_filter_options(rows), key=f"{KEY}_status")
    programme_value = right.selectbox("Место", PROGRAMME_CHOICES, key=f"{KEY}_programme")
    search = left.text_input("Поиск (идентификатор, название, требование)", key=f"{KEY}_search")
    only_pending = right.checkbox("Только невыполненные", key=f"{KEY}_pending")
    if right.button("↻ Обновить", key=f"{KEY}_refresh", width="stretch"):
        st.rerun()
    return filter_rows(
        rows,
        group=str(group),
        check_class=str(check_class),
        status_value=str(status_value),
        programme_value=str(programme_value),
        search=str(search),
        only_pending=bool(only_pending),
    )


def _render_table(rows: Sequence[Mapping[str, Any]]) -> None:
    """Таблица выборки: статус, вердикт, журнал, место в программе и причина снятия."""
    if not rows:
        st.info("В выборке нет пунктов: ослабьте фильтры или очистите поиск.")
        return
    layout.rows_table(
        rows,
        key=f"{KEY}_table",
        columns={
            "check_id": "Проверка",
            "group": "Группа",
            "check_class": "Класс",
            "status": "Статус",
            "verdict": "Вердикт",
            "origin": "Источник",
            "journal_range": "Журнал",
            "at": "Когда",
            "revision": "Ревизия",
            "programme": "Программа",
            "reason": "Причина снятия",
            "mark": "",
        },
        height=380,
    )


def _render_exports(session: TestSession, rows: Sequence[Mapping[str, Any]]) -> None:
    """Выгрузки протокола: CSV выборки и артефакт сессии (`FR-P-34`, `FR-P-64`)."""
    st.divider()
    left, right = st.columns(2)
    left.download_button(
        "⬇ CSV выборки",
        data=protocol_csv(rows),
        file_name=export_name(session),
        mime="text/csv",
        key=f"{KEY}_csv",
    )
    if right.button("💾 В артефакты сессии", key=f"{KEY}_artifact", width="stretch"):
        path = save_selection(session, rows)
        state.store_session(session)
        flash.success(f"Выборка протокола сохранена в артефакты сессии: {path.name}")
        st.rerun()
    st.caption(
        "Команд запуска в протоколе нет: единственная команда — «▶ Следующая» на `SCR-301` (`IR-P-4`)."
    )


def _render_marks(session: TestSession, rows: Sequence[Mapping[str, Any]]) -> None:
    """Ручная отметка оператора: заключение обязательно (`FR-P-32`, `AC-P-15`)."""
    st.divider()
    with st.expander("🖐 Ручная отметка (`FR-P-32`)", expanded=False):
        if not rows:
            st.caption("Отмечать нечего: выборка пуста.")
            return
        check_id = st.selectbox(
            "Пункт",
            [str(row["check_id"]) for row in rows],
            key=f"{KEY}_mark_pick",
        )
        options = [str(item) for item in checks_engine.status_options()]
        mark = st.selectbox("Отметка", options, key=f"{KEY}_mark_status")
        note = st.text_area("Заключение (обязательно)", key=f"{KEY}_mark_note", height=80)
        if st.button("Записать отметку", key=f"{KEY}_mark_apply", width="stretch"):
            _apply_mark(session, str(check_id), str(mark), str(note))
        _render_history(session, str(check_id))


def _apply_mark(session: TestSession, check_id: str, mark: str, note: str) -> None:
    """Пишет отметку через `results.py`: без заключения отметка не принимается (`AC-P-15`)."""
    if not str(note or "").strip():
        st.error(
            "Отметка без заключения не принимается: объясните, почему проверка выполнена "
            "не машиной (`AC-P-15`)."
        )
        return
    results_api.record(
        session,
        mark_payload(check_id, mark, note),
        origin=results_api.ORIGIN_MANUAL,
        author=session.info.operator_fio,
        revision=state.current_queue().revision,
    )
    state.store_session(session)
    flash.success(f"{check_id}: отметка «{mark}» записана в протокол")
    st.rerun()


def _render_history(session: TestSession, check_id: str) -> None:
    """История повторов проверки: прежние результаты сохранены (`FR-P-36`)."""
    log = results_api.history(session, check_id)
    st.caption(
        f"Повторов: {results_api.repeats(session, check_id)} — предыдущие результаты "
        f"сохраняются в истории (`FR-P-36`)."
    )
    if not log:
        return
    layout.rows_table(
        [
            {
                "at": layout.short_time(item.get("at") or item.get("ended_at")),
                "status": status.result_text(item),
                "verdict": str(item.get("verdict") or item.get("operator_note") or ""),
                "origin": status.origin_label(str(item.get("origin") or "")),
            }
            for item in log
        ],
        key=f"{KEY}_history_{check_id}",
        columns={"at": "Когда", "status": "Статус", "verdict": "Вердикт", "origin": "Источник"},
        height=180,
    )


def _render_suspension(
    session: TestSession, queue: Queue, rows: Sequence[Mapping[str, Any]]
) -> None:
    """Снятие пункта с причиной и возврат (`FR-P-15`, `FR-P-35`).

    Снятие — результат прогона, а не правка состава программы: пункт остаётся в очереди со
    состоянием «снята», причина уходит в протокол, а состав правится только на `SCR-102`.
    """
    st.divider()
    with st.expander("⏭ Снять пункт с причиной (`FR-P-15`)", expanded=False):
        if queue.is_empty:
            st.caption(
                "Очередь прогона не собрана: снимать нечего — очередь собирается на `SCR-102`."
            )
        else:
            chosen = st.selectbox("Пункт очереди", queue.check_ids, key=f"{KEY}_off_pick")
            reason = st.text_input("Причина снятия (обязательно)", key=f"{KEY}_off_reason")
            if st.button("Снять с причиной", key=f"{KEY}_off_apply", width="stretch"):
                _take_off(session, queue, str(chosen), str(reason))
            suspended = [str(row["check_id"]) for row in rows if row["suspended"]]
            if suspended:
                back = st.selectbox("Вернуть в очередь", suspended, key=f"{KEY}_back_pick")
                if st.button("↩ Вернуть в очередь", key=f"{KEY}_back_apply", width="stretch"):
                    _return_to_queue(session, queue, str(back))
        outside = [
            str(row["check_id"]) for row in rows if row["programme_group"] == OUTSIDE_PROGRAMME
        ]
        if outside:
            st.caption(
                "Вне программы: "
                + ", ".join(outside)
                + " — результаты остались в протоколе, обоснование сокращения — на `SCR-102` "
                "(`FR-P-69`)."
            )


def _take_off(session: TestSession, queue: Queue, check_id: str, reason: str) -> None:
    """Снимает пункт с причиной: в очереди — `queue.take_off`, иначе — `results.suspend`."""
    if not str(reason or "").strip():
        st.error("Снятие без причины не принимается: причина попадает в протокол (`FR-P-15`).")
        return
    author = session.info.operator_fio
    if queue.find(check_id) is None:
        try:
            results_api.suspend(session, check_id, reason, author=author)
        except ValueError as exc:
            st.error(str(exc))
            return
        state.store_session(session)
    else:
        queue.take_off(check_id, reason, session=session, author=author)
        state.store_queue(
            queue,
            event="queue_take_off",
            message=f"Пункт {check_id} снят с очереди: {reason}",
        )
    flash.success(f"{check_id}: снятие с причиной записано в протокол")
    st.rerun()


def _return_to_queue(session: TestSession, queue: Queue, check_id: str) -> None:
    """Возвращает снятый пункт: результат снятия убирается, событие остаётся в истории."""
    author = session.info.operator_fio
    if queue.find(check_id) is None:
        results_api.resume(session, check_id, author=author)
        state.store_session(session)
    else:
        queue.return_to_queue(check_id, session=session, author=author)
        state.store_queue(
            queue, event="queue_returned", message=f"Пункт {check_id} возвращён в очередь"
        )
    flash.success(f"{check_id}: снятие убрано, пункт снова в очереди")
    st.rerun()


def _render_context(
    programme: Programme,
    rows: Sequence[Mapping[str, Any]],
    filtered: Sequence[Mapping[str, Any]],
) -> None:
    """Панель контекста: KPI выборки, готовность, правила протокола и переходы."""
    st.subheader("KPI выборки")
    layout.kpi(kpi_items(filtered), columns=2)
    st.caption(readiness_note(filtered))
    waiting = conclusion_note(filtered)
    if waiting:
        st.warning(waiting)
    st.caption(
        layout.join_parts(
            f"в протоколе: {len(rows)}",
            f"в выборке: {len(filtered)}",
            f"повторов: {sum(int(row['repeats']) for row in rows)}",
        )
    )

    st.divider()
    st.caption("Правила протокола")
    st.markdown(
        "- Ручная отметка без заключения не принимается (`FR-P-32`).\n"
        "- Снятые с причиной и «вне программы» видны раздельно (`FR-P-35`).\n"
        "- Повтор проверки сохраняет предыдущий результат как историю (`FR-P-36`)."
    )
    ids = [str(row["check_id"]) for row in filtered]
    if ids:
        chosen = st.selectbox("Открыть карточку проверки", ids, key=f"{KEY}_open_pick")
        if st.button("→ Карточка проверки (SCR-302)", key=f"{KEY}_open", width="stretch"):
            st.session_state[state.KEY_CHECK] = str(chosen)
            state.go_to(CHECK_SCREEN)
    st.divider()
    st.caption(
        f"Программа: ревизия {programme.revision} ({programme.status_label}), "
        f"пунктов {programme.size}"
    )
    if st.button(
        "→ Программа: состав и ревизии (SCR-102)", key=f"{KEY}_to_programme", width="stretch"
    ):
        state.go_to(PROGRAMME_SCREEN)
    if st.button("→ Журнал обмена (SCR-402)", key=f"{KEY}_journal", width="stretch"):
        state.go_to(JOURNAL_SCREEN)
    if st.button("→ Отчёт испытаний (SCR-404)", key=f"{KEY}_report", width="stretch"):
        state.go_to(REPORT_SCREEN)
    if st.button("→ Сравнение сессий (SCR-405)", key=f"{KEY}_compare", width="stretch"):
        state.go_to(COMPARE_SCREEN)
