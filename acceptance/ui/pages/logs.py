"""Экран «Журнал» (FR-T6): обмен с сервером, файлы журналов, выгрузка.

Экран отвечает за прозрачность испытаний: оператор видит **каждый** запрос пульта
к серверу (метод, путь, статус, длительность, размеры, тела в пределах лимита),
может отфильтровать записи по метке проверки (id вида `TC-FILE-01`), по ошибкам и
по подстроке пути, посмотреть детали обмена и выгрузить журнал.

Файлы журналов:
    * `acceptance_data/logs/session_<id>.jsonl` — структурный журнал сессии
      (машинный след испытаний, попадает в отчёт);
    * `acceptance_data/logs/app.log` — текстовый журнал приложения (с ротацией).
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from acceptance.http_log import HttpExchange
from acceptance.logging_setup import list_session_logs, log_event, tail_log
from acceptance.session import TestSession, now_iso
from acceptance.ui import state

DEFAULT_ROWS = 100
APP_LOG_TAIL_LINES = 200


def render() -> None:
    """Отрисовывает экран «Журнал»."""
    st.title("Журнал")
    st.caption(
        "FR-T6 · каждое обращение пульта к серверу, шаги проверок и действия оператора. "
        "Структурный JSONL-журнал сессии — машинный след испытаний и основа отчёта."
    )

    runtime = state.get_runtime()
    if runtime is None:
        st.error("Base URL испытуемого сервера не задан — журнал нечем наполнять.")
        return

    session = state.current_session()
    _render_summary(runtime)
    st.divider()
    _render_records(runtime)
    st.divider()
    _render_files(session, runtime)


def _render_summary(runtime: state.Runtime) -> None:
    """Сводка журнала: счётчики, методы, статусы, метки проверок."""
    summary = runtime.journal.summary()
    col_total, col_errors, col_slow, col_out, col_in = st.columns(5)
    col_total.metric("Записей", summary["total"])
    col_errors.metric("С ошибкой", summary["errors"])
    col_slow.metric("Максимум, мс", f"{summary['slowest_ms']:.0f}")
    col_out.metric("Отправлено, байт", summary["request_bytes"])
    col_in.metric("Получено, байт", summary["response_bytes"])

    if summary["labels"]:
        st.caption(f"Запросы по меткам проверок: {summary['labels']}")
    else:
        st.caption("Метки проверок пока не использовались (проверки чек-листа — этапы T3/T4).")


def _render_records(runtime: state.Runtime) -> None:
    """Таблица обменов с фильтрами и просмотром деталей записи."""
    records = runtime.journal.records
    if not records:
        st.info("Журнал пуст: с момента запуска пульта запросов к серверу не было.")
        return

    col_errors, col_label, col_text, col_rows = st.columns([1, 2, 2, 1])
    only_errors = col_errors.checkbox("Только ошибки", key="logs_only_errors")
    label = col_label.text_input("Метка (id проверки)", key="logs_label")
    needle = col_text.text_input("Поиск по пути/ошибке", key="logs_text")
    rows_limit = col_rows.number_input(
        "Строк",
        min_value=10,
        max_value=1000,
        value=DEFAULT_ROWS,
        step=10,
        key="logs_rows",
    )

    selected = [
        record
        for record in reversed(records)
        if _matches(record, only_errors=only_errors, label=label, needle=needle)
    ][: int(rows_limit)]

    if not selected:
        st.warning("По заданным фильтрам записей нет.")
        return

    st.table(
        [
            {
                "#": record.seq,
                "Время": record.started_at,
                "Запрос": f"{record.method} {record.url}",
                "Статус": record.status if record.status is not None else "—",
                "мс": f"{record.duration_ms:.0f}",
                "Запрос, байт": record.request_bytes if record.request_bytes is not None else "—",
                "Ответ, байт": record.response_bytes if record.response_bytes is not None else "—",
                "Метка": record.label or "—",
                "Ошибка": record.error or "",
            }
            for record in selected
        ]
    )

    options = {f"#{record.seq} · {record.one_line}": record for record in selected}
    choice = st.selectbox("Детали записи", list(options), key="logs_detail")
    _render_details(options[choice])

    col_clear, col_download = st.columns(2)
    if col_clear.button("🧹 Очистить журнал в памяти", use_container_width=True, key="logs_clear"):
        runtime.journal.clear()
        log_event("journal_cleared", "Журнал в памяти очищен", level="WARNING", module="logs")
        st.rerun()

    col_download.download_button(
        "⬇ Журнал (JSON)",
        data=json.dumps(runtime.journal.to_dicts(), ensure_ascii=False, indent=2),
        file_name=f"journal_{now_iso().replace(':', '')}.json",
        mime="application/json",
        use_container_width=True,
        key="logs_download",
    )


def _render_details(record: HttpExchange) -> None:
    """Детали одного обмена: метаданные и (если сохранены) тела."""
    with st.expander(f"Обмен #{record.seq}: {record.one_line}", expanded=True):
        st.json(record.as_dict())
        if record.body_truncated:
            st.caption("Тело усечено по лимиту `PULT_BODY_LIMIT`.")


def _render_files(session: TestSession | None, runtime: state.Runtime) -> None:
    """Файлы журналов: структурный журнал сессии и текстовый журнал приложения."""
    st.subheader("Файлы журналов")

    logs = state.ensure_logging(state.log_level(), session.session_id if session else None)
    session_log = (
        Path(session.logs["session_log"]) if session and session.logs.get("session_log") else None
    )

    if session_log is not None and session_log.exists():
        st.caption(f"Журнал текущей сессии: {session_log.as_posix()}")
        st.download_button(
            "⬇ Структурный журнал сессии (JSONL)",
            data=session_log.read_bytes(),
            file_name=session_log.name,
            mime="application/x-ndjson",
            key="logs_session_download",
        )
    else:
        st.caption(
            "Сессия не выбрана: структурный журнал ведётся в "
            f"`{logs.session_log.as_posix()}` (для сессии — `session_<id>.jsonl`)."
        )

    available = list_session_logs()
    if available:
        options = {path.name: path for path in available}
        chosen = st.selectbox("Файлы журналов сессий", list(options), key="logs_file_choice")
        st.download_button(
            "⬇ Выбранный журнал (JSONL)",
            data=options[chosen].read_bytes(),
            file_name=chosen,
            mime="application/x-ndjson",
            key="logs_file_download",
        )

    st.markdown(f"**Хвост `app.log` (последние {APP_LOG_TAIL_LINES} строк)**")
    lines = tail_log(logs.app_log, lines=APP_LOG_TAIL_LINES)
    st.code("\n".join(lines) if lines else "(журнал пуст)", language="text")


def _matches(
    record: HttpExchange,
    *,
    only_errors: bool,
    label: str,
    needle: str,
) -> bool:
    """Проверяет запись журнала на соответствие фильтрам экрана."""
    if only_errors and not record.is_error:
        return False

    expected_label = label.strip()
    if expected_label and (record.label or "") != expected_label:
        return False

    text = needle.strip().lower()
    if not text:
        return True

    haystack = " ".join(
        filter(
            None,
            (record.path, record.query, record.error or "", record.content_type or ""),
        )
    ).lower()
    return text in haystack
