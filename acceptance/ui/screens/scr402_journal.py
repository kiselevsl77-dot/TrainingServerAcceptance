"""`SCR-402` Журнал обмена — Результаты (Ф1–Ф4).

Макет экрана — `docs/16`, раздел «SCR-402. Журнал обмена»; требования ТЗ: `FR-P-28`, `FR-P-57`,
`DR-P-4`.

Назначение: полный машинный след испытаний — все обмены со стендом, разбор после прогона и
выгрузка `JSONL` для внешнего анализа и воспроизведения. Экран не для повседневной работы
(прогон — `SCR-301`), а для разбора: выборка по метке проверки даёт полный след пункта.

Чего экран **не делает**: не хранит второй статус проверки (`DR-P-5`) и не запускает проверки —
повтор пункта выполняется единственной командой «▶ Следующая» на `SCR-301` (`IR-P-4`).

Данные: журнал процесса пульта (`http_log.Journal` — то же, что видит лента на «Прогоне») и
файл `JSONL` текущей сессии (`logs/session_<id>.jsonl`, `FR-P-28`). Тела усечены лимитом
настройки с пометкой (`NFR-P-4`), а не потеряны.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import streamlit as st

from acceptance.exchange import DETAIL_LEVELS, exchange_curl
from acceptance.http_log import HttpExchange
from acceptance.paths import ARTIFACT_DIR, ensure_dirs
from acceptance.session import TestSession, add_artifact
from acceptance.ui import state
from acceptance.ui.components import flash, journal, layout
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr402_journal"

#: Куда ведут действия разбора: карточка пункта, повтор на прогоне, замечания.
CHECK_SCREEN = "scr302_check"
RUN_SCREEN = "scr301_run"
NOTES_SCREEN = "scr403_notes"

#: Значение фильтра «без ограничения».
ANY = "все"

#: Пустая ячейка, когда значение неизвестно (объём тела не записан).
DASH = "—"

#: Подпись записи без метки проверки: обмен сделан вне прогона (подготовка, консоль).
NO_LABEL = journal.NO_LABEL

#: Сколько записей журнала показывать в таблице экрана.
TABLE_LIMIT = 200

#: Уровни детализации ядра: запрос показывается целиком, ответ — с телом (`DR-P-4`).
REQUEST_LEVEL = "полностью"
RESPONSE_LEVEL = "тело"

#: Содержимое экрана: блоки, состояния и переходы макета (`docs/16`).
CONTENT = ScreenContent(
    purpose="Полный машинный след: все обмены со стендом и выгрузка для внешнего анализа.",
    blocks=(
        "Фильтры: только ошибки, метка проверки, поиск",
        "Таблица обменов: номер, статус, метод, путь, метка, длительность",
        "Детали выбранного обмена: заголовки, тела запроса и ответа, curl",
        "Выгрузка JSONL сессии и сохранение выборки",
    ),
    states=(
        "Пусто: обменов ещё не было",
        "Много записей: постраничный вывод и ограничение выборки",
        "Ошибки обмена: выделены и посчитаны",
    ),
    transitions=(
        ("SCR-301", "Прогон"),
        ("SCR-302", "Карточка проверки"),
        ("SCR-403", "Замечания к API"),
    ),
    requirements=(
        "FR-P-28",
        "FR-P-57",
        "DR-P-4",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def bytes_text(value: Any) -> str:
    """Объём в читаемом виде: `128 КБ`, `1.2 МБ`; неизвестный объём — пустая строка."""
    if value is None:
        return ""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return "0 Б"
    for limit, unit in ((1024 * 1024, "МБ"), (1024, "КБ")):
        if size >= limit:
            share = size / limit
            return f"{share:.1f} {unit}".replace(".0 ", " ")
    return f"{size} Б"


def label_options(records: Sequence[HttpExchange]) -> tuple[str, ...]:
    """Варианты фильтра по метке проверки: «все» и метки обменов по алфавиту."""
    labels = sorted({record.label for record in records if record.label})
    return (ANY, *labels)


def filter_records(
    records: Sequence[HttpExchange],
    *,
    label: str = "",
    only_errors: bool = False,
    needle: str = "",
    limit: int = TABLE_LIMIT,
) -> list[HttpExchange]:
    """Выборка журнала: метка проверки, «только ошибки», подстрока (`DR-P-4`, `FR-P-57`).

    Порядок — свежие сверху (как в ленте «Прогона»): разбор начинается с последнего обмена.
    """
    filter_label = "" if str(label or "") in ("", ANY) else str(label)
    return journal.select(
        records, label=filter_label, only_errors=only_errors, needle=needle, limit=limit
    )


def detail_level(wanted: str, *, fallback: int) -> str:
    """Уровень детализации из ядра (`exchange.DETAIL_LEVELS`); неизвестный — заменяется.

    Ядро владеет уровнями подробности (`DR-P-4`), а экран лишь просит «запрос целиком» и
    «ответ с телом»: если состав уровней изменится, экран покажет ближайший доступный,
    а не упадёт на незнакомом значении.
    """
    levels = list(DETAIL_LEVELS)
    if wanted in levels:
        return wanted
    index = min(max(int(fallback), 0), len(levels) - 1)
    return levels[index]


def screen_rows(records: Sequence[HttpExchange]) -> list[dict[str, Any]]:
    """Строки таблицы журнала: номер, время, знак исхода, обмен, метка, длительность, объёмы."""
    rows: list[dict[str, Any]] = []
    for record in records:
        icon = "❌" if record.is_error else "✅"
        outcome = str(record.status) if record.status is not None else "нет ответа"
        if record.error:
            outcome = record.error
        rows.append(
            {
                "seq": record.seq,
                "at": layout.short_time(record.started_at, "%H:%M:%S"),
                "mark": icon,
                "status": outcome,
                "method": record.method,
                "path": record.url,
                "label": record.label or NO_LABEL,
                "duration": f"{record.duration_ms:.0f} мс",
                "size": layout.join_parts(
                    f"→ {bytes_text(record.request_bytes) or DASH}",
                    f"← {bytes_text(record.response_bytes) or DASH}",
                ),
                "error": record.error or "",
            }
        )
    return rows


def summary_note(records: Sequence[HttpExchange]) -> str:
    """Сводка журнала: сколько обменов, ошибок, сколько занял самый долгий и объём (макет)."""
    summary = journal.summary_items(records)
    return layout.join_parts(
        f"Обменов {summary['total']}",
        f"ошибок {summary['errors']}",
        f"макс {summary['slowest_ms']:.0f} мс",
        f"передано {bytes_text(summary['request_bytes'])}",
        f"получено {bytes_text(summary['response_bytes'])}",
    )


def truncation_note(records: Sequence[HttpExchange]) -> str:
    """Сколько тел усечено лимитом настройки (`NFR-P-4`) — «усечено», а не «потеряно»."""
    cut = [record for record in records if bool(record.body_truncated)]
    if not cut:
        return ""
    return (
        f"Тела усечены лимитом настройки: {layout.plural(len(cut), ('обмен', 'обмена', 'обменов'))} "
        "— полный текст воспроизводится повтором (`NFR-P-4`)"
    )


def records_jsonl(records: Sequence[HttpExchange]) -> str:
    """Записи выборки в `JSONL`: по одному обмену на строку (`FR-P-28`)."""
    return "\n".join(json.dumps(record.as_dict(), ensure_ascii=False) for record in records)


def trace_text(session_log: Path | None, records: Sequence[HttpExchange]) -> str:
    """`JSONL`-след сессии: файл журнала, если он есть, иначе записи ленты построчно.

    Файл сессии полнее журнала процесса (в нём и события пульта), поэтому при наличии
    берётся он; если файла нет (новая сессия, чистая установка), выгружаются обмены.
    """
    if session_log is not None:
        try:
            text = Path(str(session_log)).read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text.strip():
            return text
    return records_jsonl(records)


def trace_name(session: TestSession | None) -> str:
    """Имя файла выгрузки следа: `session_<id>.jsonl` (без сессии — `journal.jsonl`)."""
    if session is None:
        return "journal.jsonl"
    return f"session_{session.session_id}.jsonl"


def save_selection(
    session: TestSession,
    records: Sequence[HttpExchange],
    *,
    label: str = "",
) -> Path:
    """Сохраняет выборку журнала в `artifacts/` и регистрирует её в сессии (`FR-P-28`)."""
    ensure_dirs()
    name = f"journal_{session.session_id}"
    if label:
        name += "_" + "".join(char if char.isalnum() or char in "-_" else "_" for char in label)
    path = ARTIFACT_DIR / f"{name}.jsonl"
    path.write_text(records_jsonl(records) + "\n", encoding="utf-8")
    add_artifact(
        session,
        kind="journal_jsonl",
        path=path,
        note=f"журнал обмена: {len(records)} записей" + (f", метка {label}" if label else ""),
    )
    return path


# ---------------------------------------------------------------------------
# Экран: выборка журнала, детали обмена, выгрузки и переходы
# ---------------------------------------------------------------------------
def render() -> None:
    """Рисует журнал обмена: фильтры, таблицу выборки, детали записи и выгрузки."""
    session = state.current_session()
    runtime = state.get_runtime()
    records = state.journal_records()
    layout.render_header(KEY, summary_note(records) or "обменов ещё не было")
    flash.render()

    if not records:
        st.warning(
            "Обменов ещё не было: пульт обращается к стенду только по вашим действиям — "
            "начните с проверки стенда (`SCR-202`) или прогона (`SCR-301`)."
        )
        if st.button("→ Стенд и проверка связи (SCR-202)", key=f"{KEY}_goto_stand", type="primary"):
            state.go_to("scr202_stand")
        return

    workspace, context = layout.zones()
    with workspace:
        chosen = _render_table(records)
        _render_details(chosen, base_url=runtime.settings.base_url if runtime else "")
    with context:
        _render_exports(session, records, chosen)
        _render_transitions(chosen)


def _render_table(records: Sequence[HttpExchange]) -> list[HttpExchange]:
    """Фильтры и таблица выборки журнала; возвращает выборку для деталей и выгрузки."""
    st.subheader(f"Журнал обмена ({len(records)})")
    left, right = st.columns(2)
    label = left.selectbox("Метка проверки", label_options(records), key=f"{KEY}_label")
    only_errors = left.checkbox("только ошибки", key=f"{KEY}_errors")
    needle = right.text_input("поиск (путь, метка, текст)", key=f"{KEY}_needle")
    chosen = filter_records(
        records, label=str(label), only_errors=bool(only_errors), needle=str(needle)
    )
    st.caption(summary_note(chosen) if chosen else "под фильтр ничего не подошло")
    cut = truncation_note(chosen)
    if cut:
        st.caption(cut)
    layout.rows_table(
        screen_rows(chosen),
        key=f"{KEY}_table",
        columns={
            "seq": "#",
            "at": "Время",
            "mark": "",
            "status": "Статус",
            "method": "Метод",
            "path": "Путь",
            "label": "Метка",
            "duration": "мс",
            "size": "Объём",
        },
        height=360,
    )
    if len(records) > TABLE_LIMIT:
        st.caption(f"показаны последние {TABLE_LIMIT} обменов из {len(records)}")
    return chosen


def _render_details(records: Sequence[HttpExchange], *, base_url: str) -> None:
    """Детали выбранного обмена: запрос и ответ раздельно, `curl` для воспроизведения (`DR-P-4`)."""
    st.divider()
    st.subheader("Детали выбранного обмена")
    if not records:
        st.caption("Выборка пуста: детали показывать нечего.")
        return
    numbers = [record.seq for record in records]
    chosen = int(st.selectbox("Обмен", numbers, key=f"{KEY}_record"))
    record = next(item for item in records if item.seq == chosen)
    st.caption(journal.feed_caption(record))
    request, response = st.columns(2)
    with request:
        st.markdown("**Запрос**")
        level = detail_level(REQUEST_LEVEL, fallback=1)
        st.code("\n".join(journal.request_lines(record, level=level)) or "—", language="http")
    with response:
        st.markdown("**Ответ**")
        level = detail_level(RESPONSE_LEVEL, fallback=3)
        st.code("\n".join(journal.response_lines(record, level=level)) or "—", language="http")
    st.caption("Команда `curl` — воспроизведение обмена вне пульта (`FR-P-57`):")
    st.code(exchange_curl(record, base_url), language="bash")
    if record.body_truncated:
        st.warning("Одно из тел усечено лимитом настройки (`NFR-P-4`).")


def _render_exports(
    session: TestSession | None,
    records: Sequence[HttpExchange],
    chosen: Sequence[HttpExchange],
) -> None:
    """Выгрузки журнала: `JSONL` следа сессии и сохранение выборки в артефакты (`FR-P-28`)."""
    st.subheader("Выгрузки")
    artifacts = state.ensure_logging(state.log_level(), state.current_session_id())
    trace = trace_text(artifacts.session_log, records)
    st.download_button(
        "⬇ JSONL сессии",
        data=trace,
        file_name=trace_name(session),
        mime="application/x-ndjson",
        key=f"{KEY}_trace",
    )
    if session is None:
        st.caption(
            "Без сессии выборка не прикладывается к артефактам: выберите сессию (`SCR-203`)."
        )
        return
    if st.button(
        f"💾 Сохранить выборку ({len(chosen)})",
        key=f"{KEY}_save",
        disabled=not chosen,
        width="stretch",
    ):
        path = save_selection(session, chosen)
        state.store_session(session)
        flash.success(f"Выборка журнала сохранена в артефакты: {path.name}")
        st.rerun()


def _render_transitions(chosen: Sequence[HttpExchange]) -> None:
    """Переходы разбора: повтор — только «Следующая» на «Прогоне» (`IR-P-4`)."""
    st.divider()
    st.subheader("Разбор")
    st.caption(
        "Повтор пункта выполняет единственная команда — «▶ Следующая» на `SCR-301` (`IR-P-4`): "
        "второй команды запуска в журнале нет."
    )
    label = str(chosen[0].label or "") if chosen else ""
    if st.button(
        "→ К прогону (SCR-301)",
        key=f"{KEY}_goto_run",
        type="primary",
        width="stretch",
    ):
        state.go_to(RUN_SCREEN)
    if label:
        if st.button(
            f"→ Карточка проверки {label} (SCR-302)", key=f"{KEY}_goto_check", width="stretch"
        ):
            st.session_state[state.KEY_CHECK] = label
            state.go_to(CHECK_SCREEN)
    if st.button("→ Замечания к API (SCR-403)", key=f"{KEY}_goto_notes", width="stretch"):
        state.go_to(NOTES_SCREEN)
