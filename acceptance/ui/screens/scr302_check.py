"""`SCR-302` Карточка проверки — Испытания (Ф1–Ф2).

Макет экрана — `docs/16`, раздел «SCR-302. Карточка проверки»; требования ТЗ: `FR-P-14`,
`FR-P-20`, `FR-P-33`, `IR-P-5`.

Экран раскрывает **один** пункт очереди целиком: требование и ожидание, шаги сценария, что
именно уйдёт на стенд, след обменов и доказательства — на одной странице, без переходов.

Чего экран **не делает** (`IR-P-4`): не вводит собственной команды запуска — запуск один, на
`SCR-301` («▶ Следующая»). Здесь оператор может только **отметить** проверку, которая не может
быть выполнена машиной (`FR-P-32`), и посмотреть её след.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import streamlit as st

from acceptance import results as results_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme
from acceptance.queue import Queue
from acceptance.session import TestSession
from acceptance.test_payloads import payload_previews, preview_lines
from acceptance.ui import state
from acceptance.ui.components import flash, journal, layout, status
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr302_check"

#: Экран прогона (переход «к очереди»).
RUN_SCREEN = "scr301_run"

#: Статусы ручной отметки: что оператор ставит вместо машинного результата (`FR-P-32`).
MARK_OPTIONS: tuple[str, ...] = (
    str(CheckStatus.MANUAL_OK),
    str(CheckStatus.SKIPPED),
    str(CheckStatus.INTERRUPTED),
)

#: Пометка состояния «блокировано API» — такая проверка не выполняется машиной.
BLOCKED = str(CheckStatus.BLOCKED)

#: Содержимое скелета: что известно об экране из макета до реализации.
CONTENT = ScreenContent(
    purpose="Разобрать одну проверку: шаги, payload, след обменов, доказательства, отметка.",
    blocks=(
        "Шаги проверки и ожидания сценария",
        "Payload шага: что именно уходит на стенд (`FR-P-20`)",
        "След обменов: диапазон журнала, раскрытый лентой внутри карточки",
        "Доказательства и артефакты проверки",
        "Отметка оператора и повтор проверки с сохранением истории (`FR-P-36`)",
    ),
    states=(
        "Нет результата: проверка ещё не выполнялась",
        "Проверка выполнена: вердикт, статус, диапазон журнала",
        "Повтор: история повторов и предыдущий результат",
    ),
    transitions=(
        ("SCR-301", "Прогон"),
        ("SCR-401", "Протокол проверок"),
        ("SCR-402", "Журнал обмена"),
    ),
    requirements=(
        "FR-P-14",
        "FR-P-20",
        "FR-P-33",
        "IR-P-5",
    ),
)


def render() -> None:
    """Рисует карточку проверки: заголовок, шаги, payload, след, доказательства, отметка."""
    session = state.current_session()
    if session is None:
        layout.render_header(KEY, "сессия не выбрана")
        flash.render()
        layout.render_no_session(what="Карточка проверки читает результат из сессии (`DR-P-5`)")
        return

    programme = state.current_programme()
    queue = state.current_queue()
    results = state.results_state(queue.check_ids)
    check_id = selected_check(session, queue, str(st.session_state.get(state.KEY_CHECK) or ""))
    spec = catalog.find(check_id) if check_id else None

    layout.render_header(KEY, check_id or "пункт не выбран")
    flash.render()
    if not check_id or spec is None:
        st.info(
            "Пункт не выбран: откройте карточку из очереди на экране «Прогон» (`SCR-301`) "
            "или соберите очередь программы на `SCR-102`."
        )
        if st.button("→ К прогону (SCR-301)", key=f"{KEY}_goto_run", type="primary"):
            state.go_to(RUN_SCREEN)
        return

    row = results.get(check_id) or results_api.row(session, check_id)
    workspace, context = layout.zones()
    with workspace:
        _render_header_block(spec, programme, queue, row)
        _render_steps(spec)
        _render_trace(session, check_id, row)
    with context:
        _render_payload(session, check_id, row)
        _render_evidence(row)
        _render_mark(session, check_id, spec)
        _render_neighbours(queue, check_id)


def selected_check(session: TestSession, queue: Queue, chosen: str = "") -> str:
    """Проверка для показа: выбранная оператором, иначе последняя прогнанная, иначе следующая.

    Выбор приходит из состояния сессии пульта (`state.KEY_CHECK`): так переход «открыть
    карточку проверки» с `SCR-301` открывает именно тот пункт, который выбрал оператор.
    Если выбора нет, карточка показывает то, что только что прогнали (чтобы прочитать
    результат и доказательства), а до первого прогона — то, что запустится следующим.
    """
    key = str(chosen or "").strip().upper()
    if key and queue.find(key) is not None:
        return key
    last = queue.last()
    if last is not None and last.is_done:
        return str(last.check_id)
    following = queue.next_item()
    if following is not None:
        return str(following.check_id)
    return str(session.checks[0]["check_id"]) if session.checks else ""


def neighbour_ids(check_ids: Sequence[str], current: str) -> tuple[str, str]:
    """Соседние пункты очереди: (предыдущий, следующий) — пустая строка на краю."""
    ids = [str(item) for item in check_ids]
    key = str(current).strip().upper()
    if key not in ids:
        return "", ""
    position = ids.index(key)
    previous = ids[position - 1] if position > 0 else ""
    following = ids[position + 1] if position + 1 < len(ids) else ""
    return previous, following


def header_lines(
    spec: Any,
    programme: Programme,
    queue: Queue,
    row: Mapping[str, Any],
) -> list[str]:
    """Строки заголовка карточки: класс, модуль, требования, ожидание и место в программе."""
    item = programme.find(spec.check_id)
    position = queue.index_of(spec.check_id)
    confirmation = (
        "подтверждение обязательно (карточка запуска)"
        if item is not None and status.is_confirmable(item.check_class)
        else "без карточки запуска"
    )
    return [
        layout.join_parts(
            f"Класс: {spec.class_label}",
            f"Модуль: {spec.module}",
            f"Требования: {spec.requirement}",
            confirmation,
        ),
        f"Ожидание: {spec.expected or 'в описании проверки не задано'}",
        layout.join_parts(
            f"В программе: позиция {position + 1} из {queue.size}"
            if position >= 0
            else "нет в очереди",
            f"ревизия {queue.revision}",
            f"результат: {status.result_text(dict(row))}"
            if row.get("has_result")
            else "результат: не выполнена",
        ),
    ]


def evidence_rows(row: Mapping[str, Any]) -> list[dict[str, str]]:
    """Доказательства результата строками: поле → значение (сложные — JSON-строкой)."""
    evidence = dict(row.get("evidence") or {})
    rows: list[dict[str, str]] = []
    for key, value in evidence.items():
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = "" if value is None else str(value)
        rows.append({"field": str(key), "value": text})
    return rows


def mark_payload(check_id: str, mark: str, note: str) -> dict[str, Any]:
    """Запись ручной отметки: статус, вердикт и заключение оператора (`FR-P-32`)."""
    text = str(note or "").strip()
    return {
        "check_id": str(check_id).strip().upper(),
        "status": str(mark),
        "verdict": text or str(mark),
        "operator_note": text,
    }


# ---------------------------------------------------------------------------
# Блоки карточки
# ---------------------------------------------------------------------------
def _render_header_block(
    spec: Any,
    programme: Programme,
    queue: Queue,
    row: Mapping[str, Any],
) -> None:
    """Заголовок: что проверяем, на каком основании и где пункт стоит в программе."""
    st.subheader(f"{spec.check_id}. {spec.title}")
    for line in header_lines(spec, programme, queue, row):
        st.markdown(line)
    if str(row.get("status")) == BLOCKED:
        st.error(
            "Проверка блокирована API: "
            + str(row.get("verdict") or "причина в замечании к API (`SCR-403`)")
        )


def _render_steps(spec: Any) -> None:
    """Шаги сценария проверки и её вызовы (`DR-P-2`)."""
    st.markdown("**Шаги проверки**")
    if spec.steps:
        for index, step in enumerate(spec.steps, start=1):
            st.markdown(f"{index}. {step}")
    else:
        st.caption("Сценарий шагов не описан: проверка выполняется вручную.")
    if spec.endpoints:
        st.caption("Вызовы: " + ", ".join(f"`{item}`" for item in spec.endpoints))
    if spec.probe_paths:
        st.caption(
            "Негативные пробы (выполняются сценарием, не шагами режима «по вызовам»): "
            + ", ".join(f"`{path}`" for path in spec.probe_paths)
        )
    if spec.automation:
        st.caption(f"Сценарий: `{spec.automation}`")


def _render_trace(session: TestSession, check_id: str, row: Mapping[str, Any]) -> None:
    """След прогона: диапазон журнала, история повторов и лента обменов этой проверки."""
    st.markdown("**След прогона**")
    st.caption(
        layout.join_parts(
            f"журнал: {row.get('journal_from') or '—'}–{row.get('journal_to') or '—'}",
            f"длительность: {round(float(row.get('duration_ms') or 0))} мс"
            if row.get("duration_ms")
            else "",
            f"повторов: {row.get('repeats') or 0}",
            f"получено: {layout.short_time(row.get('at'))}" if row.get("at") else "",
            f"источник: {status.origin_label(str(row.get('origin') or ''), suspended=bool(row.get('suspended')))}",
        )
    )
    if not row.get("has_result"):
        st.info("Проверка ещё не запускалась: запуск — командой «▶ Следующая» на `SCR-301`.")

    history = results_api.history(session, check_id)
    if history:
        with st.expander(f"История повторов ({len(history)})", expanded=False):
            layout.rows_table(
                [
                    {
                        "at": layout.short_time(item.get("at")),
                        "status": status.result_text(item),
                        "author": str(item.get("author") or ""),
                    }
                    for item in history
                ],
                key=f"{KEY}_history_{check_id}",
                columns={"at": "Когда", "status": "Результат", "author": "Кто"},
                height=200,
            )

    st.markdown("**Лента обменов проверки**")
    runtime = state.get_runtime()
    journal.render_feed(
        state.journal_records(),
        key=f"{KEY}_feed_{check_id}",
        label=check_id,
        base_url=runtime.settings.base_url if runtime is not None else "",
    )


def _render_payload(session: TestSession, check_id: str, row: Mapping[str, Any]) -> None:
    """Что уйдёт на стенд: предпросмотр `__TEST__`-данных и параметры пункта (`FR-P-20`)."""
    st.subheader("Что будет отправлено")
    spec = catalog.find(check_id)
    if spec is None:
        st.caption("Проверки нет в каталоге: предпросмотр невозможен.")
        return
    params = dict(row.get("params") or {})
    for line in preview_lines(payload_previews(spec, session, params=params)):
        st.markdown(line)
    if params:
        st.caption("Параметры пункта: " + json.dumps(params, ensure_ascii=False))


def _render_evidence(row: Mapping[str, Any]) -> None:
    """Доказательства результата: поля записи и карточка запуска (`FR-P-33`)."""
    st.subheader("Доказательства")
    rows = evidence_rows(row)
    if not rows:
        st.caption("Доказательств нет: результат ещё не получен.")
        return
    layout.rows_table(
        rows,
        key=f"{KEY}_evidence",
        columns={"field": "Поле", "value": "Значение"},
        height=220,
    )
    card = dict((row.get("evidence") or {}).get("run_card") or {})
    if card:
        st.caption(
            "Карточка запуска: "
            + layout.join_parts(
                str(card.get("data") or ""),
                str(card.get("responsible") or ""),
                str(card.get("artifacts") or ""),
            )
        )


def _render_mark(session: TestSession, check_id: str, spec: Any) -> None:
    """Отметка оператора: ручная/пропущена/прервана — всегда с заключением (`FR-P-32`)."""
    st.subheader("Отметка оператора")
    st.caption(
        "Ручная отметка — результат, который ставит человек: машинный сценарий её не заменяет. "
        f"Сценарий проверки: `{spec.automation or 'нет'}`."
    )
    mark = st.selectbox("Отметка", MARK_OPTIONS, key=f"{KEY}_mark_{check_id}")
    note = st.text_area("Заключение (обязательно)", key=f"{KEY}_mark_note_{check_id}", height=80)
    if st.button("Записать отметку", key=f"{KEY}_mark_apply_{check_id}", width="stretch"):
        _apply_mark(session, check_id, mark, note)


def _apply_mark(session: TestSession, check_id: str, mark: str, note: str) -> None:
    """Записывает ручную отметку в сессию: без заключения отметка не принимается."""
    if not str(note or "").strip():
        st.error(
            "Отметка без заключения не принимается: объясните, почему проверка не выполнена "
            "машиной (`AC-P-15`)."
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
    flash.success(f"{check_id}: отметка «{mark}» записана в сессию")
    st.rerun()


def _render_neighbours(queue: Queue, check_id: str) -> None:
    """Соседние пункты очереди и переходы экрана (клавиши `→`/`←` из макета)."""
    st.divider()
    previous, following = neighbour_ids(queue.check_ids, check_id)
    left, right = st.columns(2)
    if left.button(
        f"← {previous}" if previous else "←",
        key=f"{KEY}_prev",
        width="stretch",
        disabled=not previous,
    ):
        st.session_state[state.KEY_CHECK] = previous
        st.rerun()
    if right.button(
        f"{following} →" if following else "→",
        key=f"{KEY}_next_item",
        width="stretch",
        disabled=not following,
    ):
        st.session_state[state.KEY_CHECK] = following
        st.rerun()
    if st.button("→ К прогону (SCR-301)", key=f"{KEY}_to_run", width="stretch"):
        state.go_to(RUN_SCREEN)
    if st.button("→ К программе (SCR-102)", key=f"{KEY}_to_programme", width="stretch"):
        state.go_to("scr102_programme")
