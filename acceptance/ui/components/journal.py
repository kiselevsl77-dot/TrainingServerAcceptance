"""Лента обмена с испытуемым сервером: что ушло и что вернулось (FR-P-20, FR-T6).

Компонент один на три экрана (макет `docs/16`):

* `SCR-301` «Прогон» — лента под очередью: свежие обмены, фильтры и вердикт;
* `SCR-302` «Карточка проверки» — тот же след, но только по одной проверке (`IR-P-5`);
* `SCR-402` «Журнал обмена» — вся лента с поиском и выгрузкой.

Разделение обязанностей: записи берутся из журнала (`acceptance/http_log.py`),
текст запроса собирается `acceptance/exchange.py` (`exchange_curl`), а этот модуль
только **выбирает** записи и показывает их двумя половинами — «запрос» и «ответ»
раздельно (`DR-P-4`), не смешивая одно с другим.

Чистые функции (`select`, `feed_caption`, `request_lines`, `response_lines`) проверяются
unit-тестами: в них вся логика отбора и разбора записи журнала.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import streamlit as st

from acceptance.exchange import DETAIL_HINTS, DETAIL_LEVELS, exchange_curl
from acceptance.http_log import HttpExchange
from acceptance.ui.components import layout

#: Сколько записей ленты показывать по умолчанию (макет: «[25 ▾]»).
DEFAULT_LIMIT = 25

#: Варианты «сколько записей» в ленте.
LIMITS: tuple[int, ...] = (10, 25, 50, 100, 200)

#: Подпись записи без метки проверки: обмен сделан вне прогона (консоль, подготовка).
NO_LABEL = "без проверки"


def select(
    records: Iterable[HttpExchange],
    *,
    label: str = "",
    only_errors: bool = False,
    needle: str = "",
    limit: int = DEFAULT_LIMIT,
) -> list[HttpExchange]:
    """Отбирает записи ленты: по метке проверки, «только ошибки», по подстроке.

    Порядок в ленте — **свежие сверху**: оператор смотрит на то, что только что
    произошло, а не листает историю с начала сессии.
    """
    text = str(needle or "").strip().lower()
    chosen = [record for record in records if not label or record.label == label]
    if only_errors:
        chosen = [record for record in chosen if record.is_error]
    if text:
        chosen = [record for record in chosen if _matches(record, text)]
    chosen.reverse()
    return chosen[: max(0, int(limit))]


def _matches(record: HttpExchange, needle: str) -> bool:
    """True, если подстрока встречается в методе, пути, запросе, ответе или метке."""
    haystack = " ".join(
        str(part or "")
        for part in (
            record.method,
            record.path,
            record.query,
            record.label,
            record.status,
            record.error,
            record.request_body,
            record.response_body,
        )
    ).lower()
    return needle in haystack


def feed_caption(record: HttpExchange) -> str:
    """Строка ленты: «#214 TC-FILE-13 · POST /api/data/file → 201 · 412 мс»."""
    outcome = str(record.status) if record.status is not None else "нет ответа"
    if record.error:
        outcome = record.error
    return layout.join_parts(
        f"#{record.seq}",
        record.label or NO_LABEL,
        f"{record.method} {record.url}",
        f"→ {outcome}",
        f"{record.duration_ms:.0f} мс",
        record.content_type or "",
    )


def request_lines(record: HttpExchange, *, level: str = "сводка") -> list[str]:
    """Строки половины «что ушло» на выбранном уровне подробности."""
    lines: list[str] = [f"{record.method} {record.url}"]
    if level in ("сводка", "заголовки", "тело", "полностью"):
        lines.append(
            layout.join_parts(
                f"размер: {_bytes(record.request_bytes)}",
                f"тип: {record.request_header_map.get('content-type', '')}",
                f"метка: {record.label or NO_LABEL}",
            )
        )
    if level in ("заголовки", "тело", "полностью"):
        lines.extend(f"{key}: {value}" for key, value in record.request_headers)
    if level in ("тело", "полностью") and record.request_body:
        lines.append("")
        lines.append(record.request_body)
    return lines


def response_lines(record: HttpExchange, *, level: str = "тело") -> list[str]:
    """Строки половины «что вернулось» на выбранном уровне подробности."""
    outcome = str(record.status) if record.status is not None else "нет ответа"
    lines: list[str] = [f"{outcome}{layout.SEPARATOR}{record.duration_ms:.0f} мс"]
    if record.error:
        lines.append(f"ошибка: {record.error}")
    if level in ("сводка", "заголовки", "тело", "полностью"):
        lines.append(
            layout.join_parts(
                f"размер: {_bytes(record.response_bytes)}",
                f"тип: {record.content_type or ''}",
                "тело усечено (лимит `PULT_BODY_LIMIT`)" if record.body_truncated else "",
            )
        )
    if level in ("заголовки", "тело", "полностью"):
        lines.extend(f"{key}: {value}" for key, value in record.response_headers)
    if level in ("тело", "полностью") and record.response_body:
        lines.append("")
        lines.append(record.response_body)
    return lines


def _bytes(value: int | None) -> str:
    """Размер в байтах или «неизвестно»."""
    return "неизвестно" if value is None else f"{int(value)} Б"


def error_share(records: Sequence[HttpExchange]) -> str:
    """Сводка ошибок ленты: «ошибок 2 из 40» (пустая строка, если записей нет)."""
    total = len(records)
    if not total:
        return ""
    errors = sum(1 for record in records if record.is_error)
    return f"ошибок {errors} из {total}"


def render_feed(
    records: Sequence[HttpExchange],
    *,
    key: str,
    label: str = "",
    limit: int = DEFAULT_LIMIT,
    only_errors: bool = False,
    needle: str = "",
    base_url: str = "",
    request_level: str = "сводка",
    response_level: str = "тело",
    show_controls: bool = True,
) -> list[HttpExchange]:
    """Рисует ленту обмена и возвращает показанные записи (свежие сверху).

    Args:
        key: префикс ключей виджетов (на экране лент может быть две).
        label: показывать только обмены этой проверки (`SCR-302`).
        request_level/response_level: уровни подробности из `exchange.DETAIL_LEVELS`.
    """
    if show_controls:
        request_level, response_level, only_errors, limit, needle = _controls(
            key, request_level, response_level, only_errors, limit
        )

    shown = select(records, label=label, only_errors=only_errors, needle=needle, limit=limit)
    if not shown:
        st.caption(
            "Обменов пока нет: прогон ещё не отправлял запросов."
            if not records
            else "Под фильтр не попала ни одна запись обмена."
        )
        return shown

    st.caption(
        layout.join_parts(
            f"показано {len(shown)} из {len(records)}",
            error_share(records),
            f"запрос: {request_level}",
            f"ответ: {response_level}",
        )
    )
    for record in shown:
        _render_record(
            record,
            key=key,
            base_url=base_url,
            request_level=request_level,
            response_level=response_level,
        )
    return shown


def _controls(
    key: str,
    request_level: str,
    response_level: str,
    only_errors: bool,
    limit: int,
) -> tuple[str, str, bool, int, str]:
    """Фильтры ленты: подробность запроса/ответа, «только ошибки», число записей, поиск."""
    query, errors, size = st.columns([2, 2, 1])
    request_level = query.selectbox(
        "Запрос",
        DETAIL_LEVELS,
        index=DETAIL_LEVELS.index(request_level) if request_level in DETAIL_LEVELS else 1,
        key=f"{key}_request_level",
        help=DETAIL_HINTS.get(request_level, ""),
    )
    response_level = query.selectbox(
        "Ответ",
        DETAIL_LEVELS,
        index=DETAIL_LEVELS.index(response_level) if response_level in DETAIL_LEVELS else 3,
        key=f"{key}_response_level",
    )
    only_errors = errors.checkbox("только ошибки", value=only_errors, key=f"{key}_only_errors")
    needle = errors.text_input("поиск", key=f"{key}_needle", placeholder="путь, метка, текст")
    limit = size.selectbox(
        "записей",
        LIMITS,
        index=LIMITS.index(limit) if limit in LIMITS else 1,
        key=f"{key}_limit",
    )
    return request_level, response_level, only_errors, int(limit), needle


def _render_record(
    record: HttpExchange,
    *,
    key: str,
    base_url: str,
    request_level: str,
    response_level: str,
) -> None:
    """Одна запись ленты: заголовок-раскрыватель и две половины — запрос и ответ."""
    icon = "❌" if record.is_error else "✅"
    with st.expander(f"{icon} {feed_caption(record)}", expanded=False):
        request, response = st.columns(2)
        with request:
            st.markdown("**Запрос**")
            st.code("\n".join(request_lines(record, level=request_level)) or "—", language="http")
        with response:
            st.markdown("**Ответ**")
            st.code("\n".join(response_lines(record, level=response_level)) or "—", language="http")
        st.caption("Команда `curl` — для воспроизведения запроса вне пульта:")
        st.code(exchange_curl(record, base_url), language="bash")


def render_record_panel(record: HttpExchange, *, base_url: str = "") -> None:
    """Панель контекста для одной записи: итог и команда `curl` (правый столбец макета)."""
    st.markdown(f"**#{record.seq} {record.label or NO_LABEL}**")
    st.caption(feed_caption(record))
    st.code(exchange_curl(record, base_url), language="bash")


def as_rows(records: Sequence[HttpExchange]) -> list[dict[str, Any]]:
    """Записи ленты строками — для таблицы журнала (`SCR-402`) и выгрузки."""
    return [
        {
            "seq": record.seq,
            "at": record.started_at,
            "label": record.label or "",
            "method": record.method,
            "path": record.url,
            "status": "" if record.status is None else record.status,
            "duration_ms": round(record.duration_ms, 1),
            "request_bytes": record.request_bytes,
            "response_bytes": record.response_bytes,
            "error": record.error or "",
        }
        for record in records
    ]


def summary_items(records: Sequence[HttpExchange]) -> dict[str, Any]:
    """Сводка ленты для KPI: всего, ошибки, медленнейший, объёмы (пустая — нули)."""
    if not records:
        return {"total": 0, "errors": 0, "slowest_ms": 0.0, "request_bytes": 0, "response_bytes": 0}
    return {
        "total": len(records),
        "errors": sum(1 for record in records if record.is_error),
        "slowest_ms": round(max(record.duration_ms for record in records), 1),
        "request_bytes": sum(record.request_bytes or 0 for record in records),
        "response_bytes": sum(record.response_bytes or 0 for record in records),
    }


def mapping_of(records: Sequence[HttpExchange]) -> Mapping[int, HttpExchange]:
    """Записи по номеру журнала (для ссылок «журнал #214–#217» внутри карточки)."""
    return {record.seq: record for record in records}
