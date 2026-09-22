"""Компонент «Монитор обмена»: вертикальная лента вызовов API и ответов (FR-T6).

Требование заказчика (21.09.2026, п. 1–2): вместо «отправлено всё залпом, а что именно —
ищи в журнале» приёмной комиссии нужна **лента обмена**: по одной записи на каждый
отправленный вызов и полученный ответ. Лента даёт:

    * степень подробности **запроса и ответа по отдельности** (`кратко` → `полностью`);
    * копирование запроса в виде `curl` (воспроизведение вне пульта, анализ);
    * **повтор с подкорректированными параметрами** прямо из записи;
    * переход к замечанию к API по записи (с готовым `curl` в «воспроизведении»);
    * метку проверки/плана, номер записи журнала и длительность — привязку к отчёту.

Компонент используется и на отдельном экране («Монитор обмена»), и встроенно — на
экранах «Чек-лист проверок» и «Консоль запросов», поэтому разметка одна и та же.
Оценка «соответствует ли вызов ожиданию» приходит снаружи (`verdicts`): её считает
программа испытаний (`acceptance.plan`), а лента только показывает результат.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode

import streamlit as st

from acceptance import endpoints as endpoints_api
from acceptance import exchange as exchange_api
from acceptance import notes as notes_api
from acceptance.exchange import RepeatRequest
from acceptance.http_log import HttpExchange
from acceptance.logging_setup import log_event
from acceptance.session import TestSession
from acceptance.ui import state
from acceptance.ui.common import api_notes
from acceptance.ui.common.flash import set_flash

DEFAULT_ROWS = 25

DETAIL_LEVELS = exchange_api.DETAIL_LEVELS

#: Уровень подробности ответа по умолчанию: тело ответа — то, что разбирает комиссия.
DEFAULT_REQUEST_DETAIL = "сводка"
DEFAULT_RESPONSE_DETAIL = "тело"


@dataclass
class MonitorSettings:
    """Настройки ленты: что показывать и насколько подробно."""

    request_detail: str = DEFAULT_REQUEST_DETAIL
    response_detail: str = DEFAULT_RESPONSE_DETAIL
    newest_first: bool = True
    only_errors: bool = False
    label: str = ""
    rows: int = DEFAULT_ROWS

    @property
    def request_hint(self) -> str:
        """Подсказка по выбранной подробности запроса."""
        return exchange_api.DETAIL_HINTS.get(self.request_detail, "")

    @property
    def response_hint(self) -> str:
        """Подсказка по выбранной подробности ответа."""
        return exchange_api.DETAIL_HINTS.get(self.response_detail, "")


def render_controls(
    *,
    key_prefix: str = "monitor",
    labels: Sequence[str] = (),
    rows_options: Sequence[int] = (10, 25, 50, 100),
) -> MonitorSettings:
    """Отрисовывает настройки ленты и возвращает их.

    Два независимых селектора подробности — требование заказчика: «возможность выбирать
    степень подробности запросов и ответов (по раздельности)».

    Args:
        key_prefix: префикс ключей виджетов (на экране может быть несколько лент).
        labels: доступные метки (id проверок/плана) для фильтра.
        rows_options: варианты «сколько записей показывать».
    """
    col_req, col_resp, col_order = st.columns([2, 2, 2])
    request_detail = str(
        col_req.selectbox(
            "Подробность запроса",
            list(DETAIL_LEVELS),
            index=list(DETAIL_LEVELS).index(DEFAULT_REQUEST_DETAIL),
            key=f"{key_prefix}_detail_request",
            help="Кратко — только строка вызова; полностью — все поля записи журнала.",
        )
    )
    response_detail = str(
        col_resp.selectbox(
            "Подробность ответа",
            list(DETAIL_LEVELS),
            index=list(DETAIL_LEVELS).index(DEFAULT_RESPONSE_DETAIL),
            key=f"{key_prefix}_detail_response",
            help="Подробность ответа выбирается независимо от подробности запроса.",
        )
    )
    newest_first = (
        col_order.radio(
            "Порядок",
            ("сначала новые", "сначала старые"),
            horizontal=True,
            key=f"{key_prefix}_order",
            help="Лента читается сверху вниз: для наблюдения за ходом прогона удобнее новые сверху.",
        )
        == "сначала новые"
    )

    col_only_errors, col_label, col_rows = st.columns([1, 2, 1])
    only_errors = col_only_errors.checkbox(
        "Только ошибки", value=False, key=f"{key_prefix}_only_errors"
    )
    label_options = ["— все —", *labels]
    label = str(
        col_label.selectbox(
            "Метка (проверка/план)",
            label_options,
            key=f"{key_prefix}_label",
            help="Метка ставится проверкой или пунктом плана: по ней видно, чей это вызов.",
        )
    )
    rows = col_rows.selectbox(
        "Записей",
        list(rows_options),
        index=list(rows_options).index(DEFAULT_ROWS) if DEFAULT_ROWS in rows_options else 0,
        key=f"{key_prefix}_rows",
    )

    return MonitorSettings(
        request_detail=request_detail,
        response_detail=response_detail,
        newest_first=newest_first,
        only_errors=only_errors,
        label="" if label.startswith("—") else label,
        rows=int(rows),
    )


def select_records(
    records: Sequence[HttpExchange],
    settings: MonitorSettings,
) -> list[HttpExchange]:
    """Отбирает записи для показа: фильтры + порядок + лимит (чистая функция)."""
    selected = [
        record
        for record in records
        if (not settings.only_errors or record.is_error)
        and (not settings.label or (record.label or "") == settings.label)
    ]
    if settings.newest_first:
        selected.reverse()
    return selected[: settings.rows]


def status_badge(record: HttpExchange) -> str:
    """Бейдж результата вызова: ✅ 2xx, ⚠️ 4xx, ❌ 5xx, 🚫 сетевая ошибка."""
    if record.error:
        return "🚫 нет ответа"
    status = record.status or 0
    if 200 <= status < 300:
        return f"✅ {status}"
    if 400 <= status < 500:
        return f"⚠️ {status}"
    return f"❌ {status}"


def render_feed(
    records: Sequence[HttpExchange],
    *,
    settings: MonitorSettings,
    session: TestSession | None = None,
    base_url: str = "",
    key_prefix: str = "monitor",
    allow_repeat: bool = True,
    allow_note: bool = True,
    verdicts: Mapping[int, tuple[str, str]] | None = None,
) -> None:
    """Отрисовывает ленту обмена: по контейнеру на каждый вызов.

    Args:
        records: записи журнала обмена (старые — первыми, как их отдаёт `Journal`).
        settings: настройки ленты (подробность, порядок, фильтры).
        session: сессия испытаний — для замечаний к API из записи.
        base_url: адрес стенда — для команды `curl`.
        key_prefix: префикс ключей виджетов.
        allow_repeat: показывать блок «Повторить с правками».
        allow_note: показывать блок «Замечание к API».
        verdicts: номер записи → (иконка, текст) — оценка соответствия ожиданию,
            её считает программа испытаний (`acceptance.plan`).
    """
    visible = select_records(records, settings)
    if not visible:
        st.info(
            "Лента пуста: под текущие фильтры не попал ни один обмен. "
            "Выполните вызов (консоль, проверка или пункт программы испытаний)."
        )
        return

    st.caption(
        f"Показано {len(visible)} из {len(records)} обменов текущего запуска пульта · "
        f"запрос: {settings.request_hint} · ответ: {settings.response_hint}"
    )
    for record in visible:
        _render_record(
            record,
            settings=settings,
            session=session,
            base_url=base_url,
            key_prefix=key_prefix,
            allow_repeat=allow_repeat,
            allow_note=allow_note,
            verdict=(verdicts or {}).get(record.seq),
        )


def _render_record(
    record: HttpExchange,
    *,
    settings: MonitorSettings,
    session: TestSession | None,
    base_url: str,
    key_prefix: str,
    allow_repeat: bool,
    allow_note: bool,
    verdict: tuple[str, str] | None,
) -> None:
    """Один обмен: заголовок, подробности запроса и ответа, действия."""
    with st.container(border=True):
        st.markdown(
            f"`#{record.seq}` **{record.method} {record.url}** → {status_badge(record)} "
            f"· {record.duration_ms:.0f} мс"
        )
        meta = [f"время: {record.started_at}", f"метка: {record.label or '—'}"]
        if verdict is not None:
            meta.append(f"{verdict[0]} {verdict[1]}")
        st.caption(" · ".join(meta))

        if settings.request_detail != "кратко":
            with st.expander("⬆ Запрос", expanded=settings.request_detail == "полностью"):
                _render_request(record, detail=settings.request_detail)
        if settings.response_detail != "кратко":
            with st.expander("⬇ Ответ", expanded=settings.response_detail == "полностью"):
                _render_response(record, detail=settings.response_detail)

        _render_actions(
            record,
            session=session,
            base_url=base_url,
            key_prefix=key_prefix,
            allow_repeat=allow_repeat,
            allow_note=allow_note,
        )


def _render_request(record: HttpExchange, *, detail: str) -> None:
    """Подробности запроса: сводка → заголовки → тело → все поля записи."""
    st.caption(
        f"{record.method} {record.url} · отправлено {record.request_bytes or 0} байт · "
        f"`Content-Type`: {record.request_header_map.get('content-type') or '—'}"
    )
    if detail in ("заголовки", "полностью"):
        st.markdown("**Заголовки запроса**")
        st.json(record.request_header_map or {})
    if detail in ("тело", "полностью"):
        st.markdown("**Тело запроса**")
        if record.request_body:
            st.code(record.request_body, language="json" if _request_is_json(record) else "text")
        else:
            st.caption("Тело в журнал не сохранялось: пустое, бинарное или выше лимита.")


def _render_response(record: HttpExchange, *, detail: str) -> None:
    """Подробности ответа: сводка → заголовки → тело → все поля записи."""
    st.caption(
        f"{status_badge(record)} · получено {record.response_bytes or 0} байт · "
        f"`Content-Type`: {record.content_type or '—'}"
    )
    if record.error:
        st.error(record.error)
    if detail in ("заголовки", "полностью"):
        st.markdown("**Заголовки ответа**")
        st.json(record.response_header_map or {})
    if detail in ("тело", "полностью"):
        st.markdown("**Тело ответа**")
        if record.response_body:
            st.code(record.response_body, language="json" if _response_is_json(record) else "text")
            if record.body_truncated:
                st.caption("Тело усечено по `PULT_BODY_LIMIT`; полностью — в JSONL сессии.")
        else:
            st.caption(
                "Тело не сохранено: пустой ответ, бинарные данные (только размер) "
                "или выключенный `PULT_LOG_BODIES`."
            )


def _request_is_json(record: HttpExchange) -> bool:
    """True, если сохранённое тело запроса — JSON."""
    return "json" in (record.request_header_map.get("content-type") or "").lower()


def _response_is_json(record: HttpExchange) -> bool:
    """True, если сохранённое тело ответа — JSON."""
    return "json" in (record.content_type or "").lower()


def _looks_like_json(text: str) -> bool:
    """True, если текст начинается как JSON-объект или массив."""
    stripped = text.strip()
    return stripped.startswith(("{", "["))


def _render_actions(
    record: HttpExchange,
    *,
    session: TestSession | None,
    base_url: str,
    key_prefix: str,
    allow_repeat: bool,
    allow_note: bool,
) -> None:
    """Действия с записью: копирование `curl`, повтор с правками, замечание к API."""
    with st.expander("📋 Запрос для копирования (curl)"):
        st.code(exchange_api.exchange_curl(record, base_url), language="bash")
        st.caption(
            "Команду можно скопировать и выполнить вне пульта: так комиссия убеждается, "
            "что вызов воспроизводим, и видит его содержимое без интерфейса."
        )
        if record.request_body and record.request_body.startswith("<multipart"):
            st.caption(f"Тело запроса: {record.request_body} — файл при повторе выбирается заново.")

    if allow_repeat:
        _render_repeat(record, base_url=base_url, key_prefix=key_prefix)
    if allow_note:
        _render_note(record, session=session, base_url=base_url, key_prefix=key_prefix)


def _render_repeat(record: HttpExchange, *, base_url: str, key_prefix: str) -> None:
    """Повторная отправка того же вызова с подкорректированными параметрами."""
    repeat = exchange_api.repeat_from_exchange(record)
    key = f"{key_prefix}_repeat_{record.seq}"
    with st.expander("🔁 Повторить с правками"):
        if repeat.multipart:
            st.caption(
                "Multipart-вызов повторяется только с выбранным файлом: откройте операцию "
                "в консоли запросов и выберите файл заново."
            )
            if st.button("📡 Открыть операцию в консоли", key=f"{key}_console"):
                _open_console(record)
            return

        path = st.text_input("Метод и путь не меняются; путь", value=repeat.path, key=f"{key}_path")
        query_text = st.text_input(
            "Query-строка (`k=v&k2=v2`)", value=_query_text(repeat), key=f"{key}_query"
        )
        body_text = st.text_area(
            "Тело запроса (JSON)",
            value=repeat.editable_body,
            height=160,
            key=f"{key}_body",
            help="Пусто — запрос без тела.",
        )
        st.caption(f"Метка повтора: `{repeat.label or '—'}` · команда для `curl` — выше.")
        if st.button("▶ Отправить повтор", key=f"{key}_send", type="primary"):
            _send_repeat(record, path=path, query_text=query_text, body_text=body_text)


def _query_text(repeat: RepeatRequest) -> str:
    """Query-строка для редактора повтора."""
    return urlencode(repeat.query) if repeat.query else ""


def _send_repeat(record: HttpExchange, *, path: str, query_text: str, body_text: str) -> None:
    """Отправляет повтор и сообщает результат (ошибки — полями результата, NFR-T3)."""
    client = state.console_client()
    runtime = state.get_runtime()
    if client is None or runtime is None:
        set_flash("error", "Клиент консоли недоступен: проверьте адрес стенда в `.env`.")
        st.rerun()

    json_body: dict[str, Any] | None = None
    if body_text.strip():
        try:
            parsed = json.loads(body_text)
        except ValueError as exc:
            set_flash("warning", f"Некорректный JSON тела повтора: {exc}")
            st.rerun()
        if not isinstance(parsed, dict):
            set_flash("warning", "Тело повтора должно быть JSON-объектом.")
            st.rerun()
        json_body = parsed

    request = RepeatRequest(
        method=record.method,
        path=path.strip() or record.path,
        query=dict(parse_qsl(query_text, keep_blank_values=True)),
        json_body=json_body,
        label=record.label or "",
    )
    with st.spinner("Повтор вызова…"):
        result = exchange_api.execute_repeat(
            client,
            request,
            body_limit=runtime.config.body_limit,
            journal=runtime.journal,
        )
    log_event(
        "monitor_repeat",
        f"Повтор из монитора: {record.method} {request.path} → {result.status_label}",
        level="INFO" if result.ok else "WARNING",
        module="monitor",
        check_id=record.label,
        payload={
            "origin_seq": record.seq,
            "repeat_seq": result.journal_seq,
            **result.as_dict(),
        },
    )
    set_flash(
        "success" if result.ok else "warning",
        f"Повтор {result.method} {request.path}: {result.status_label} за "
        f"{result.duration_ms:.0f} мс"
        + (f" · запись журнала #{result.journal_seq}" if result.journal_seq else ""),
    )
    st.rerun()


def _render_note(
    record: HttpExchange,
    *,
    session: TestSession | None,
    base_url: str,
    key_prefix: str,
) -> None:
    """Замечание к API по записи обмена (факт и `curl` подставлены по умолчанию)."""
    key = f"{key_prefix}_note_{record.seq}"
    with st.expander("✍ Замечание к API по этой записи"):
        if session is None:
            st.caption("Сессия не выбрана: замечание не попадёт в отчёт испытаний.")
        modules = list(notes_api.MODULES)
        default_module = _module_for_path(record.path, modules)
        col_title, col_priority, col_module = st.columns([2, 1, 1])
        title = col_title.text_input(
            "Заголовок", value=_default_note_title(record), key=f"{key}_title"
        )
        priority = col_priority.selectbox(
            "Приоритет",
            list(notes_api.PRIORITIES),
            index=list(notes_api.PRIORITIES).index("P1"),
            key=f"{key}_priority",
        )
        module = col_module.selectbox(
            "Модуль",
            modules,
            index=modules.index(default_module),
            key=f"{key}_module",
        )
        fact = st.text_area("Факт (что произошло)", value="", height=80, key=f"{key}_fact")
        reproduction = st.text_area(
            "Воспроизведение (команда `curl`)",
            value=exchange_api.exchange_curl(record, base_url),
            height=110,
            key=f"{key}_repro",
        )
        if st.button("✍ Создать замечание", key=f"{key}_create"):
            note = api_notes.manual_note(
                title.strip() or _default_note_title(record),
                module=module,
                endpoint=f"{record.method} {record.path}",
                priority=priority,
                fact=fact.strip() or _default_note_fact(record),
                expected="",
                reproduction=reproduction,
                check_id=record.label,
                evidence=f"запись журнала #{record.seq} ({record.one_line})",
            )
            api_notes.create_note(note, session=session, module="monitor")


def _default_note_title(record: HttpExchange) -> str:
    """Заголовок замечания по умолчанию: метод, путь и статус."""
    status = record.status if record.status is not None else "нет ответа"
    return f"{record.method} {record.path}: ответ {status}"


def _default_note_fact(record: HttpExchange) -> str:
    """Факт по умолчанию: что пульт увидел в этом обмене."""
    if record.error:
        return f"Вызов завершился ошибкой соединения: {record.error}"
    return (
        f"Сервер ответил статусом {record.status} за {record.duration_ms:.0f} мс, "
        f"`Content-Type`: {record.content_type or '—'}, размер ответа: "
        f"{record.response_bytes or 0} байт."
    )


def _module_for_path(path: str, modules: Sequence[str]) -> str:
    """Модуль замечания по пути запроса (по умолчанию — «Прочее»)."""
    lowered = path.lower()
    candidates = (
        ("/api/tasks", "Task service"),
        ("/api/datasets", "Datasets"),
        ("/api/loads", "Loads"),
        ("/api/ml_models/models/", "ML models"),
        ("/api/ml_models", "ML models"),
        ("/api/data", "File Import"),
        ("/api/health", "Система"),
        ("/version", "Система"),
    )
    for marker, module in candidates:
        if marker in lowered and module in modules:
            return module
    return "Прочее"


def _open_console(record: HttpExchange) -> None:
    """Открывает операцию записи в консоли запросов (multipart повторяют там же)."""
    spec = endpoints_api.find(f"{record.method.lower()} {record.path}")
    if spec is None:
        set_flash(
            "warning",
            f"Операция `{record.method} {record.path}` не найдена в реестре консоли.",
        )
        st.rerun()
    st.session_state["console_module"] = spec.module
    st.session_state[f"console_op_{spec.module}"] = spec.menu_label
    st.session_state["pult_screen"] = "console"
    st.rerun()
