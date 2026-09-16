"""Экран «Записи (RAW + markup)» (FR-T2, этап T1).

Назначение: показать оператору **прозрачное** объединение RAW- и markup-файлов
в записи при том, что в API такой сущности нет:

    * вкладка «Записи» — карточки записей: обе части (`id`, `size`, `import_date`,
      `s3_path`), правило и уверенность объединения, кандидаты разметки;
    * «Дубли» — повторные загрузки одного имени: все версии рядом, отметка актуальной;
    * «Несопоставленные» — записи без разметки и разметка без RAW (обе стороны
      можно связать вручную);
    * «Прочие файлы» — модели и отчёты (в записи не попадают);
    * «Манифест» — выгрузка объединения (CSV/JSON) со **всеми** файлами реестра.

Ручные решения оператора (`acceptance/overrides.py`) приоритетнее эвристики, каждое
фиксируется в структурном журнале сессии вместе с ФИО оператора и комментарием.
Субдатасеты (группировка записей) в пульте **не используются** — решение заказчика.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from acceptance import overrides as overrides_api
from acceptance.http_log import label_context
from acceptance.logging_setup import log_event
from acceptance.paths import ARTIFACT_DIR
from acceptance.records import (
    STATS_BLOCKED,
    STATS_CALCULATED,
    MarkupStatsEntry,
    Record,
    RecordsOverview,
    build_records,
    save_manifest,
)
from acceptance.session import (
    TestSession,
    add_artifact,
    now_iso,
    set_markup_stats,
)
from acceptance.ui import state
from acceptance.ui.common import api_notes
from acceptance.ui.common.flash import render_flash, set_flash
from acceptance.ui.common.pagination import render_pagination
from client.errors import ClientError, NotFoundError
from client.schemas import FileMetadataResponse
from lib.markup_stats import parse_markup_stats
from lib.pagination import paginate
from lib.subdatasets import format_duration, format_size

#: Ключи состояния экрана (у каждой вкладки со списком — свои: ключи виджетов
#: должны быть уникальны в пределах прогона Streamlit).
KEY_PAGE = "records_page"
KEY_PAGE_UNPAIRED = "records_unpaired_page"
KEY_PAYLOAD = "records_payload"

#: Фильтры карточек записей.
FLAG_ATTENTION = "требуют внимания"
FLAG_DUPLICATES = "только дубли"
FLAG_NO_MARKUP = "только без разметки"
FLAG_MANUAL = "только с ручными решениями"
FLAG_EXCLUDED = "только исключённые"
FLAGS = (FLAG_ATTENTION, FLAG_DUPLICATES, FLAG_NO_MARKUP, FLAG_MANUAL, FLAG_EXCLUDED)

#: Метки проверок чек-листа для фильтрации журнала (TC-REC).
LABEL_OVERVIEW = "TC-REC-01"
LABEL_DUPLICATES = "TC-REC-02"
LABEL_MANUAL = "TC-REC-03"
LABEL_UNPAIRED = "TC-REC-04"
LABEL_MARKUP = "TC-REC-05"

#: Порог «крупного» RAW-файла: выгрузка пары требует явного подтверждения.
RAW_CONFIRM_LIMIT = 50 * 1024 * 1024

#: Число строк предпросмотра манифеста и записей в сводной таблице.
MANIFEST_PREVIEW_ROWS = 200
TABLE_PREVIEW_ROWS = 500

#: Шаблоны замечаний к API, которые создаются прямо с экрана записей.
NOTE_LINK_DEFECT = "Нет сущности «субдатасет»"
NOTE_DOWNLOAD_DEFECT = "Скачивание файлов с не-ASCII именами"


def render() -> None:
    """Отрисовывает экран «Записи (RAW + markup)»."""
    st.title("Записи (RAW + markup)")
    st.caption(
        "FR-T2 · в API записи нет: пульт объединяет RAW и markup по базовому имени и времени "
        "импорта и **показывает правило** объединения. Ручные решения оператора приоритетнее "
        "эвристики и фиксируются в журнале и истории сессии."
    )
    render_flash()

    runtime = state.get_runtime()
    if runtime is None:
        st.error(
            "Base URL испытуемого сервера не задан. Укажите `TRAINING_SERVER_BASE_URL` "
            "в файле `.env` и перезапустите пульт."
        )
        return

    col_refresh, col_hint = st.columns([1, 3])
    if col_refresh.button("🔄 Обновить реестр", use_container_width=True, key="records_refresh"):
        state.refresh_files()
        log_event("records_refreshed", "Кэш реестра файлов сброшен", module="records")
        set_flash("success", "Кэш реестра сброшен: список будет прочитан заново.")
        st.rerun()
    col_hint.caption(
        f"Сервер: {runtime.settings.base_url} · реестр читается с кэшем "
        f"{int(state.FILES_CACHE_TTL)} с (`GET /api/data/files`)."
    )

    files, error = state.load_files()
    if error:
        st.error(f"Реестр файлов не получен: {error}")
        return
    if not files:
        st.info("Реестр файлов пуст — загрузите RAW и markup через основной UI или консоль.")
        return

    session = state.current_session()
    overrides = overrides_api.load_overrides()
    overview = build_records(files, overrides=overrides, markup_stats=state.current_markup_stats())

    _log_overview(overview, session)
    _render_kpi(overview)

    tab_records, tab_duplicates, tab_unpaired, tab_other, tab_manifest = st.tabs(
        [
            f"📦 Записи ({len(overview.records)})",
            f"🔁 Дубли ({overview.stats.duplicate_bases})",
            f"❓ Несопоставленные ({overview.unpaired_files})",
            f"🗃️ Прочие файлы ({len(overview.other_files)})",
            "📄 Манифест",
        ]
    )

    with tab_records:
        _render_records_tab(overview, overrides=overrides, session=session, runtime=runtime)
    with tab_duplicates:
        _render_duplicates_tab(overview, overrides=overrides, session=session, runtime=runtime)
    with tab_unpaired:
        _render_unpaired_tab(overview, overrides=overrides, session=session, runtime=runtime)
    with tab_other:
        _render_other_tab(overview)
    with tab_manifest:
        _render_manifest_tab(overview, session=session)


def _log_overview(overview: RecordsOverview, session: TestSession | None) -> None:
    """Пишет в журнал результат объединения (DEBUG: вызывается при каждой перерисовке)."""
    log_event(
        "records_loaded",
        "Реестр объединён в записи",
        level="DEBUG",
        module="records",
        check_id=LABEL_OVERVIEW,
        payload={
            **overview.stats.to_dict(),
            "attention": len(overview.requires_attention),
            "session_id": session.session_id if session else None,
        },
    )


def _render_kpi(overview: RecordsOverview) -> None:
    """KPI объединения: записи, покрытие разметкой, дубли, несопоставленные файлы."""
    stats = overview.stats
    col_records, col_paired, col_dups, col_empty, col_markup, col_other = st.columns(6)
    col_records.metric("Записей", stats.records, help="Запись — RAW-файл вместе с его разметкой")
    col_paired.metric("С разметкой", f"{stats.paired} ({stats.coverage}%)")
    col_dups.metric("Дублей", stats.duplicates, help="Версии записи при повторных загрузках имени")
    col_empty.metric("Без разметки", stats.without_markup)
    col_markup.metric("Разметка без RAW", stats.unpaired_markup)
    col_other.metric("Прочих файлов", stats.other)

    st.caption(
        f"Файлов в реестре: {stats.files} · объём RAW: {format_size(stats.raw_bytes)} · "
        f"markup: {format_size(stats.markup_bytes)} · окно сопоставления по времени: "
        f"{format_duration(overview.window_seconds)} · объединено вручную: {stats.manual} · "
        f"исключено оператором: {stats.excluded}"
    )
    if stats.weak:
        st.warning(
            f"Объединений без подтверждения по времени: {stats.weak} — разметка импортирована "
            f"позже RAW более чем на {format_duration(overview.window_seconds)}. Проверьте "
            "карточки с пометкой ⚠️."
        )
    if stats.duplicates:
        st.info(
            "В реестре есть дубли: файлы с одинаковыми именами различаются только "
            "`id`/`size`/`import_date`/`s3_path` — в API нет `checksum` и `updated_at`, "
            "поэтому версии видны только в разделе «Дубли»."
        )


# ---------------------------------------------------------------------------
# Вкладка «Записи»
# ---------------------------------------------------------------------------
def _render_records_tab(
    overview: RecordsOverview,
    *,
    overrides: overrides_api.Overrides,
    session: TestSession | None,
    runtime: state.Runtime,
) -> None:
    """Список записей с фильтрами, сводной таблицей и карточками действий."""
    filters = _render_filters()
    selected = [record for record in overview.records if _matches_filters(record, filters)]

    st.caption(
        f"Показано {len(selected)} из {len(overview.records)} записей. "
        "Правило объединения и уверенность указаны в каждой карточке; кандидаты разметки "
        "приведены по близости времени импорта."
    )
    if not selected:
        st.warning("По заданным фильтрам записей нет.")
        return

    st.dataframe(
        [_row_for_table(record) for record in selected[:TABLE_PREVIEW_ROWS]],
        hide_index=True,
        use_container_width=True,
    )
    if len(selected) > TABLE_PREVIEW_ROWS:
        st.caption(f"В таблице первые {TABLE_PREVIEW_ROWS} строк — карточки доступны ниже.")

    page = paginate(
        selected,
        page=int(st.session_state.get(KEY_PAGE, 1)),
        per_page=int(st.session_state.get(f"{KEY_PAGE}_size", 25)),
    )
    for record in page.items:
        _render_record_card(
            record,
            prefix="records",
            overview=overview,
            overrides=overrides,
            session=session,
            runtime=runtime,
        )
    render_pagination(KEY_PAGE, page)


def _render_filters(prefix: str = "records") -> dict[str, Any]:
    """Фильтры списка записей (имя, признаки, актуальность).

    Args:
        prefix: префикс ключей виджетов — экран показывает фильтры на нескольких
            вкладках, ключи Streamlit обязаны быть уникальными.
    """
    col_name, col_flags, col_view = st.columns([2, 3, 2])
    needle = col_name.text_input("Поиск по имени (база, RAW или markup)", key=f"{prefix}_needle")
    flags = col_flags.multiselect("Показать", list(FLAGS), default=[], key=f"{prefix}_flags")
    only_current = col_view.checkbox(
        "Только актуальные версии",
        value=False,
        key=f"{prefix}_only_current",
        help="Дубли — повторные загрузки того же имени; по умолчанию показаны все версии.",
    )
    return {"needle": needle.strip().lower(), "flags": flags, "only_current": only_current}


def _matches_filters(record: Record, filters: dict[str, Any]) -> bool:
    """Проверяет запись на соответствие фильтрам экрана."""
    if filters["only_current"] and not record.is_current:
        return False

    needle = filters["needle"]
    if needle:
        names = [record.base_name.lower(), record.raw.file_name.lower()]
        if record.markup is not None:
            names.append(record.markup.file_name.lower())
        if not any(needle in name for name in names):
            return False

    flags = set(filters["flags"])
    checks = {
        FLAG_ATTENTION: record.needs_attention,
        FLAG_DUPLICATES: record.duplicate,
        FLAG_NO_MARKUP: not record.has_markup,
        FLAG_MANUAL: record.manual,
        FLAG_EXCLUDED: record.excluded,
    }
    return all(checks[flag] for flag in flags)


def _row_for_table(record: Record) -> dict[str, Any]:
    """Строка сводной таблицы записей."""
    return {
        "Запись": f"{record.confidence_icon} {record.base_name}",
        "Версия": f"{record.version}/{record.group_size}",
        "Актуальная": "да" if record.is_current else "",
        "RAW": record.raw.file_name,
        "Размер RAW": format_size(record.raw.size),
        "markup": record.markup.file_name if record.markup is not None else "—",
        "Δ импорта": record.delta_note,
        "Правило": record.rule,
        "Уверенность": record.confidence,
        "Записи/чанки": (
            f"{record.stats.records}/{record.stats.chunks}" if record.stats.is_calculated else "—"
        ),
        "Исключена": "да" if record.excluded else "",
    }


# ---------------------------------------------------------------------------
# Карточка записи
# ---------------------------------------------------------------------------
def _render_record_card(
    record: Record,
    *,
    prefix: str,
    overview: RecordsOverview,
    overrides: overrides_api.Overrides,
    session: TestSession | None,
    runtime: state.Runtime,
) -> None:
    """Карточка записи: файлы, правило объединения, кандидаты и действия оператора.

    Args:
        prefix: префикс ключей виджетов (карточка выводится на двух вкладках — ключи
            Streamlit должны быть уникальными).
    """
    marks: list[str] = []
    if record.duplicate:
        marks.append(f"дубль {record.version}/{record.group_size}")
    if record.is_current:
        marks.append("актуальная")
    if record.manual:
        marks.append("решение оператора")
    if record.excluded:
        marks.append("исключена")
    header = f"{record.confidence_icon} {record.raw.file_name} · {format_size(record.raw.size)}"
    if marks:
        header += " · " + ", ".join(marks)

    with st.expander(header, expanded=not record.has_markup):
        st.caption(
            f"База имени: `{record.base_name}` · запись `{record.record_id}` · правило: "
            f"{record.rule} · уверенность: {record.confidence} · {record.delta_note}"
        )
        if record.comment:
            st.info(f"Комментарий оператора: {record.comment}")

        col_raw, col_markup = st.columns(2)
        _render_file_card(col_raw, "RAW", record.raw)
        if record.markup is not None:
            _render_file_card(col_markup, "markup", record.markup)
        else:
            col_markup.warning(
                "Разметка не найдена: запись без разметки не может использоваться в обучении. "
                "Привяжите разметку вручную (действия ниже)."
            )

        _render_candidates(record)
        _render_stats_line(record)
        _render_actions(
            record,
            prefix=prefix,
            overrides=overrides,
            session=session,
            runtime=runtime,
            pool=_markup_pool(record, overview),
        )


def _render_file_card(container: Any, title: str, file: FileMetadataResponse) -> None:
    """Поля файла записи: имя, id, размер, дата импорта, путь в S3."""
    container.markdown(f"**{title}** · `{file.file_name}`")
    container.caption(
        f"id: `{file.id}` · тип: {file.file_type} · размер: {format_size(file.size)} "
        f"({file.size} байт) · импорт: {file.import_date:%d.%m.%Y %H:%M:%S}"
    )
    container.caption(f"s3_path: `{file.s3_path}`")


def _render_candidates(record: Record) -> None:
    """Кандидаты разметки по близости времени импорта (проверка дублей по фактам)."""
    if not record.candidates:
        return

    parts: list[str] = []
    for candidate in record.candidates[:3]:
        delta = (candidate.import_date - record.raw.import_date).total_seconds()
        parts.append(
            f"`{candidate.file_name}` ({format_size(candidate.size)}, Δ {format_duration(delta)}, "
            f"импорт {candidate.import_date:%d.%m.%Y %H:%M})"
        )
    st.caption(f"Кандидаты разметки ({len(record.candidates)}): " + "; ".join(parts))


def _render_stats_line(record: Record) -> None:
    """Строка характеристик разметки (записи/чанки) — клиентская оценка."""
    stats = record.stats
    if stats.is_calculated:
        st.success(
            f"Разметка: записей {stats.records}, чанков {stats.chunks} · {stats.status} · "
            f"{stats.at} · {stats.note}"
        )
    elif stats.status == STATS_BLOCKED:
        st.warning(f"Разбор разметки: {stats.status} · {stats.at} · {stats.note}")


def _markup_pool(record: Record, overview: RecordsOverview) -> list[FileMetadataResponse]:
    """Файлы разметки, доступные для ручной привязки: кандидаты + свободная разметка."""
    pool: list[FileMetadataResponse] = list(record.candidates)
    known = {str(file.id) for file in pool}
    for file in overview.unpaired_markup:
        if str(file.id) not in known:
            pool.append(file)
            known.add(str(file.id))
    return pool


def _file_option(file: FileMetadataResponse, raw: FileMetadataResponse) -> str:
    """Подпись варианта в списке разметки (с Δ времени импорта)."""
    delta = (file.import_date - raw.import_date).total_seconds()
    return (
        f"{file.file_name} · {format_size(file.size)} · Δ {format_duration(delta)} · "
        f"импорт {file.import_date:%d.%m.%Y %H:%M}"
    )


# ---------------------------------------------------------------------------
# Выгрузка файлов записи
# ---------------------------------------------------------------------------
def _payload_key(record: Record, kind: str) -> str:
    """Ключ состояния для результата выгрузки (файл уже скачан в память)."""
    return f"{KEY_PAYLOAD}_{record.record_id}_{kind}"


def _render_actions(
    record: Record,
    *,
    prefix: str,
    overrides: overrides_api.Overrides,
    session: TestSession | None,
    runtime: state.Runtime,
    pool: list[FileMetadataResponse],
) -> None:
    """Блок действий: выгрузка файлов, разбор разметки и ручные решения оператора."""
    st.markdown("**Выгрузка**")
    heavy = record.size_bytes > RAW_CONFIRM_LIMIT
    confirm = True
    if heavy:
        confirm = st.checkbox(
            f"Подтверждаю выгрузку {format_size(record.size_bytes)} (RAW "
            f"{format_size(record.raw.size)}"
            + (f" + markup {format_size(record.markup.size)}" if record.markup else "")
            + ")",
            key=f"{prefix}_confirm_{record.record_id}",
            help=f"Объём больше {format_size(RAW_CONFIRM_LIMIT)}: выгрузка занимает время и трафик.",
        )

    col_raw, col_markup, col_pair, col_stats = st.columns(4)
    if col_raw.button(
        "⬇ RAW",
        use_container_width=True,
        disabled=not confirm,
        key=f"{prefix}_get_raw_{record.record_id}",
    ):
        _download(record, "raw", runtime=runtime, session=session)

    if record.markup is not None:
        if col_markup.button(
            "⬇ markup",
            use_container_width=True,
            key=f"{prefix}_get_mk_{record.record_id}",
        ):
            _download(record, "markup", runtime=runtime, session=session)
        if col_pair.button(
            "⬇ пара (zip)",
            use_container_width=True,
            disabled=not confirm,
            key=f"{prefix}_get_pair_{record.record_id}",
        ):
            _download(record, "pair", runtime=runtime, session=session)
        if col_stats.button(
            "🧮 Записи и чанки",
            use_container_width=True,
            key=f"{prefix}_stats_{record.record_id}",
        ):
            _calculate_stats(record, runtime=runtime, session=session)
    else:
        col_markup.caption("markup отсутствует")
        col_pair.caption("пара недоступна")
        col_stats.caption("разбор невозможен")

    for kind in ("raw", "markup", "pair"):
        _render_payload(record, kind, prefix=prefix, session=session)

    if record.duplicate:
        st.caption(
            "Дубли версий: в API нет `checksum`/`updated_at`, поэтому различить версии можно "
            "только по `id`/`size`/`import_date`/`s3_path`."
        )
        if st.button(
            "✍ Замечание к API: нет связи «запись ↔ пара RAW+markup»",
            key=f"{prefix}_note_link_{record.record_id}",
        ):
            _create_note(
                NOTE_LINK_DEFECT,
                check_id=LABEL_DUPLICATES,
                evidence=(
                    f"Экран «Записи»: база {record.base_name}, версий {record.group_size}; "
                    f"файлы различаются только id/size/import_date/s3_path"
                ),
                session=session,
            )

    _render_manual_decisions(
        record,
        prefix=prefix,
        overrides=overrides,
        session=session,
        pool=pool,
    )


def _render_manual_decisions(
    record: Record,
    *,
    prefix: str,
    overrides: overrides_api.Overrides,
    session: TestSession | None,
    pool: list[FileMetadataResponse],
) -> None:
    """Ручные решения оператора: привязка/снятие разметки, исключение, возврат к эвристике."""
    st.markdown("**Ручные решения оператора**")
    st.caption(
        "Решение оператора приоритетнее эвристики: последнее решение выигрывает, а разметка "
        "принадлежит ровно одной записи. Каждое действие фиксируется в журнале сессии."
    )

    options: dict[str, FileMetadataResponse | None] = {"— не выбрано —": None}
    for file in pool:
        options[_file_option(file, record.raw)] = file

    col_choice, col_comment = st.columns([3, 2])
    choice = col_choice.selectbox(
        "Привязать markup", list(options), key=f"{prefix}_link_{record.record_id}"
    )
    comment = col_comment.text_input(
        "Комментарий к решению", key=f"{prefix}_comment_{record.record_id}"
    )

    col_link, col_unlink, col_exclude, col_reset = st.columns(4)
    if col_link.button(
        "🔗 Привязать",
        use_container_width=True,
        disabled=options[choice] is None,
        key=f"{prefix}_link_btn_{record.record_id}",
    ):
        chosen = options[choice]
        _save_override(
            record,
            overrides_api.ACTION_LINK,
            markup_id=str(chosen.id) if chosen else None,
            comment=comment,
            overrides=overrides,
            session=session,
        )
    if col_unlink.button(
        "✂ Снять привязку",
        use_container_width=True,
        disabled=record.markup is None,
        key=f"{prefix}_unlink_btn_{record.record_id}",
    ):
        _save_override(
            record,
            overrides_api.ACTION_UNLINK,
            comment=comment,
            overrides=overrides,
            session=session,
        )
    if col_exclude.button(
        "🚫 Исключить",
        use_container_width=True,
        disabled=record.excluded,
        key=f"{prefix}_exclude_btn_{record.record_id}",
    ):
        _save_override(
            record,
            overrides_api.ACTION_EXCLUDE,
            comment=comment,
            overrides=overrides,
            session=session,
        )
    if col_reset.button(
        "↩ К эвристике",
        use_container_width=True,
        disabled=not (record.manual or record.excluded),
        key=f"{prefix}_reset_btn_{record.record_id}",
    ):
        _reset_overrides(record, overrides=overrides, session=session)


def _download(
    record: Record,
    kind: str,
    *,
    runtime: state.Runtime,
    session: TestSession | None,
) -> None:
    """Скачивает файл записи (RAW/markup) или собирает пару в zip и запоминает результат."""
    label = {"raw": LABEL_OVERVIEW, "markup": LABEL_MARKUP, "pair": LABEL_DUPLICATES}[kind]
    file_ids = {
        "raw": [str(record.raw.id)],
        "markup": [str(record.markup.id)] if record.markup else [],
        "pair": [str(file.id) for file in (record.raw, record.markup) if file is not None],
    }[kind]
    log_event(
        "record_download",
        f"Выгрузка записи «{record.base_name}»: {kind}",
        module="records",
        check_id=label,
        payload={
            "record_id": record.record_id,
            "kind": kind,
            "file_ids": file_ids,
            "raw_size": record.raw.size,
            "markup_size": record.markup.size if record.markup else None,
        },
    )

    try:
        with st.spinner("Скачивание файлов с сервера…"):
            payload = _collect_payload(record, kind, runtime=runtime)
    except (NotFoundError, ClientError) as exc:
        _handle_download_error(record, kind, exc, session=session)
        return

    st.session_state[_payload_key(record, kind)] = payload
    st.rerun()


def _collect_payload(record: Record, kind: str, *, runtime: state.Runtime) -> dict[str, Any]:
    """Скачивает нужные файлы и (для пары) упаковывает их в zip в памяти."""
    targets: list[FileMetadataResponse] = []
    if kind in ("raw", "pair"):
        targets.append(record.raw)
    if kind in ("markup", "pair") and record.markup is not None:
        targets.append(record.markup)

    collected: list[tuple[FileMetadataResponse, bytes]] = []
    for file in targets:
        is_markup = record.markup is not None and file.id == record.markup.id
        with label_context(LABEL_MARKUP if is_markup else LABEL_OVERVIEW):
            response = runtime.apis.files.download(file.id)
        collected.append((file, response.content))

    if kind == "pair":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file, content in collected:
                archive.writestr(file.file_name, content)
        payload_content = buffer.getvalue()
        name = f"{record.base_name or record.raw.file_name}.zip"
        media = "application/zip"
    else:
        file, payload_content = collected[0]
        name = file.file_name
        media = "application/octet-stream"

    return {
        "kind": kind,
        "name": name,
        "content": payload_content,
        "media_type": media,
        "at": now_iso(),
        "files": [str(file.id) for file, _ in collected],
    }


def _handle_download_error(
    record: Record,
    kind: str,
    exc: ClientError,
    *,
    session: TestSession | None,
) -> None:
    """Фиксирует неудачную выгрузку и предлагает замечание к API (дефект latin-1)."""
    status = getattr(exc, "status_code", "")
    error = f"[{status}] {exc}" if status else str(exc)
    st.session_state[_payload_key(record, kind)] = {
        "kind": kind,
        "error": error,
        "at": now_iso(),
    }
    log_event(
        "record_download_failed",
        f"Выгрузка записи «{record.base_name}» не удалась: {error}",
        level="ERROR",
        module="records",
        check_id=LABEL_MARKUP,
        payload={"record_id": record.record_id, "kind": kind, "error": error},
    )

    file = record.markup if kind == "markup" else record.raw
    if file is not None and not str(file.file_name).isascii():
        _create_note(
            NOTE_DOWNLOAD_DEFECT,
            check_id=LABEL_MARKUP,
            evidence=f"{file.file_name}: {error}",
            session=session,
            silent=True,
        )
    st.rerun()


def _render_payload(
    record: Record,
    kind: str,
    *,
    prefix: str,
    session: TestSession | None,
) -> None:
    """Показывает результат выгрузки: ошибку либо кнопки сохранения файла."""
    data = st.session_state.get(_payload_key(record, kind))
    if not data:
        return

    title = {"raw": "RAW", "markup": "markup", "pair": "пара (zip)"}.get(kind, kind)
    if data.get("error"):
        st.error(f"{title}: выгрузка не удалась — {data['error']}")
        return

    content = data.get("content") or b""
    col_save, col_artifact, col_info = st.columns([2, 2, 3])
    col_save.download_button(
        f"💾 Сохранить {title} ({format_size(len(content))})",
        data=content,
        file_name=str(data.get("name") or "file.bin"),
        mime=str(data.get("media_type") or "application/octet-stream"),
        key=f"{prefix}_save_{record.record_id}_{kind}",
        use_container_width=True,
    )
    if col_artifact.button(
        "📥 В артефакты сессии",
        key=f"{prefix}_artifact_{record.record_id}_{kind}",
        use_container_width=True,
    ):
        _save_artifact(record, kind, data, session=session)
    col_info.caption(f"Получено {format_size(len(content))} в {data.get('at', '—')}")


def _save_artifact(
    record: Record,
    kind: str,
    data: dict[str, Any],
    *,
    session: TestSession | None,
) -> None:
    """Сохраняет выгруженный файл в `artifacts/` и регистрирует его в сессии."""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(str(data.get("name") or "file.bin"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = ARTIFACT_DIR / f"{record.record_id}_{kind}_{stamp}_{source.name}"
    content = data.get("content") or b""
    path.write_bytes(content)

    log_event(
        "artifact_saved",
        f"Артефакт сохранён: {path.name}",
        module="records",
        check_id=LABEL_OVERVIEW,
        payload={
            "record_id": record.record_id,
            "kind": kind,
            "path": path.as_posix(),
            "size_bytes": len(content),
        },
    )
    if session is None:
        set_flash(
            "warning",
            f"Файл сохранён: {path.as_posix()} — сессия не выбрана, в отчёт артефакт не попадёт.",
        )
    else:
        add_artifact(
            session,
            kind=f"record {kind}: {record.base_name}",
            path=path,
            size_bytes=len(content),
            note=f"Выгрузка с экрана «Записи» (запись {record.record_id})",
        )
        state.store_session(session)
        set_flash("success", f"Артефакт сохранён и зарегистрирован в сессии: {path.name}")
    st.rerun()


# ---------------------------------------------------------------------------
# Разбор разметки (записи/чанки) и ручные решения
# ---------------------------------------------------------------------------
def _calculate_stats(
    record: Record,
    *,
    runtime: state.Runtime,
    session: TestSession | None,
    notify: bool = True,
) -> MarkupStatsEntry | None:
    """Скачивает markup и считает записи/чанки (серверных агрегатов в API нет).

    При `notify=False` результат только сохраняется в сессии — вызывающий код сам
    решает, что показать оператору (например, при пакетном разборе версий дублей).
    """
    markup = record.markup
    if markup is None:
        return None

    try:
        with st.spinner(f"Скачивание и разбор {markup.file_name}…"):
            with label_context(LABEL_MARKUP):
                payload = runtime.apis.files.download(markup.id)
            parsed = parse_markup_stats(payload.content)
        stats = MarkupStatsEntry(
            records=parsed.records,
            chunks=parsed.chunks,
            status=STATS_CALCULATED,
            note=f"получено {format_size(len(payload.content))} (клиентская оценка)",
            at=now_iso(),
        )
    except (NotFoundError, ClientError) as exc:
        status = getattr(exc, "status_code", "")
        prefix = f"[{status}] " if status else ""
        stats = MarkupStatsEntry(
            status=STATS_BLOCKED,
            note=f"файл не скачивается через API: {prefix}{exc}",
            at=now_iso(),
        )
        if not str(markup.file_name).isascii():
            _create_note(
                NOTE_DOWNLOAD_DEFECT,
                check_id=LABEL_MARKUP,
                evidence=f"{markup.file_name}: {exc}",
                session=session,
                silent=True,
            )

    log_event(
        "record_markup_stats",
        f"Разбор разметки «{markup.file_name}»: {stats.status}",
        level="INFO" if stats.is_calculated else "WARNING",
        module="records",
        check_id=LABEL_MARKUP,
        payload={
            "record_id": record.record_id,
            "file_id": str(markup.id),
            "records": stats.records,
            "chunks": stats.chunks,
            "status": stats.status,
            "note": stats.note,
        },
    )
    if session is None:
        if notify:
            set_flash("warning", f"Разбор выполнен ({stats.status}), но сессия не выбрана.")
    else:
        set_markup_stats(
            session,
            markup.id,
            records=stats.records,
            chunks=stats.chunks,
            status=stats.status,
            note=stats.note,
        )
        state.store_session(session)
        if notify:
            set_flash("success", f"Разбор разметки сохранён в сессии: {stats.status}")
    if notify:
        st.rerun()
    return stats


def _save_override(
    record: Record,
    action: str,
    *,
    markup_id: str | None = None,
    comment: str = "",
    overrides: overrides_api.Overrides,
    session: TestSession | None,
) -> None:
    """Сохраняет ручное решение оператора (файл `records_overrides.json`) и пишет журнал."""
    entry = overrides.add(
        action=action,
        base_name=record.base_name,
        raw_id=str(record.raw.id),
        markup_id=markup_id,
        comment=comment,
        operator=session.info.operator_fio if session else "",
        session_id=session.session_id if session else "",
    )
    path = overrides_api.save_overrides(overrides)

    log_event(
        "record_override",
        f"Ручное решение: {entry.action_label} · {record.base_name}",
        module="records",
        check_id=LABEL_MANUAL,
        payload=entry.to_dict() | {"file": path.as_posix(), "record_id": record.record_id},
    )
    if session is not None:
        session.add_history(
            "record_override",
            f"{record.base_name}: {entry.action_label}",
            record_id=record.record_id,
            base_name=record.base_name,
            action=action,
            raw_id=str(record.raw.id),
            markup_id=entry.markup_id,
            comment=entry.comment,
            operator=entry.operator,
        )
        state.store_session(session)

    set_flash("success", f"Решение сохранено: {entry.action_label} ({record.base_name}).")
    st.rerun()


def _reset_overrides(
    record: Record,
    *,
    overrides: overrides_api.Overrides,
    session: TestSession | None,
) -> None:
    """Убирает решения оператора по записи — запись снова объединяется эвристикой."""
    removed = overrides.remove_for_raw(str(record.raw.id))
    overrides_api.save_overrides(overrides)
    log_event(
        "record_override_reset",
        f"Решения оператора сняты: {record.base_name}",
        level="WARNING",
        module="records",
        check_id=LABEL_MANUAL,
        payload={"record_id": record.record_id, "removed": removed},
    )
    if session is not None:
        session.add_history(
            "record_override_reset",
            f"{record.base_name}: возврат к эвристике",
            record_id=record.record_id,
            removed=removed,
        )
        state.store_session(session)
    set_flash("warning", f"Решения по записи сняты ({removed}) — действует эвристика по времени.")
    st.rerun()


def _create_note(
    title_part: str,
    *,
    check_id: str,
    evidence: str,
    session: TestSession | None,
    silent: bool = False,
) -> None:
    """Создаёт замечание к API из шаблона известного дефекта и добавляет его в сессию.

    Логика (поиск шаблона, тексты сообщений, дедупликация, запись журнала) — общий
    хелпер пульта `acceptance.ui.common.api_notes`, один для всех экранов.
    """
    api_notes.create_note_from_template(
        title_part,
        session=session,
        check_id=check_id,
        evidence=evidence,
        module="records",
        silent=silent,
    )


# ---------------------------------------------------------------------------
# Вкладки «Дубли», «Несопоставленные», «Прочие файлы», «Манифест»
# ---------------------------------------------------------------------------
def _version_option(record: Record) -> str:
    """Подпись версии записи в списках дублей."""
    markup = record.markup.file_name if record.markup is not None else "—"
    return (
        f"v{record.version}/{record.group_size} · RAW {format_size(record.raw.size)} · "
        f"{record.raw.import_date:%d.%m.%Y %H:%M} · markup: {markup}"
    )


def _render_duplicates_tab(
    overview: RecordsOverview,
    *,
    overrides: overrides_api.Overrides,
    session: TestSession | None,
    runtime: state.Runtime,
) -> None:
    """Дубли: все версии одной базы имени, выбор актуальной, сравнение версий."""
    bases = overview.duplicate_bases
    if not bases:
        st.success("Дублей нет: у каждой базы имени ровно одна версия записи.")
        return

    st.warning(
        "Дубль — повторная загрузка файла с тем же именем. В API нет `checksum`/`updated_at`, "
        "поэтому версии различаются только `id`/`size`/`import_date`/`s3_path`. Пульт показывает "
        "все версии и помечает актуальной последнюю по дате импорта (можно изменить вручную)."
    )

    for base in bases:
        records = overview.for_base(base)
        st.markdown(f"**{base}** · версий: {len(records)}")
        st.dataframe(
            [_row_for_table(record) for record in records],
            hide_index=True,
            use_container_width=True,
        )

        options = {_version_option(record): record for record in records}
        labels = list(options)
        current = next((label for label in labels if options[label].is_current), labels[0])
        chosen = st.radio(
            "Актуальная версия",
            labels,
            index=labels.index(current),
            key=f"records_current_{base}",
        )
        col_mark, col_note = st.columns(2)
        if col_mark.button(
            "📌 Отметить актуальной",
            key=f"records_current_btn_{base}",
            use_container_width=True,
        ):
            _save_override(
                options[chosen],
                overrides_api.ACTION_CURRENT,
                comment="Дубль: выбор актуальной версии",
                overrides=overrides,
                session=session,
            )
        if col_note.button(
            "✍ Замечание к API: дубли версий",
            key=f"records_note_dup_{base}",
            use_container_width=True,
        ):
            sizes = ", ".join(
                f"{record.raw.import_date:%Y-%m-%d}: {record.raw.size} Б" for record in records
            )
            _create_note(
                NOTE_LINK_DEFECT,
                check_id=LABEL_DUPLICATES,
                evidence=f"База {base}: версий {len(records)}; размеры RAW — {sizes}",
                session=session,
            )

        with st.expander(f"Сравнение версий записи «{base}» (TC-REC-06)"):
            st.dataframe(
                [
                    {
                        "Версия": f"{record.version}/{record.group_size}",
                        "Актуальная": "да" if record.is_current else "",
                        "RAW, байт": record.raw.size,
                        "markup, байт": record.markup.size if record.markup else "",
                        "Δ импорта, с": round(record.delta_seconds, 3)
                        if record.delta_seconds is not None
                        else "",
                        "Записи": record.stats.records if record.stats.records is not None else "",
                        "Чанки": record.stats.chunks if record.stats.chunks is not None else "",
                        "Разметка": record.stats.status,
                    }
                    for record in records
                ],
                hide_index=True,
                use_container_width=True,
            )
            st.caption(
                "Расхождение размеров одноимённых файлов при разных `id` и одинаковом имени — "
                "прямое следствие отсутствия проверки дублей на сервере (TC-REC-06)."
            )
            if st.button(
                "🧮 Считать записи и чанки для всех версий", key=f"records_stats_all_{base}"
            ):
                processed = 0
                for record in records:
                    if record.markup is None:
                        continue
                    _calculate_stats(record, runtime=runtime, session=session, notify=False)
                    processed += 1
                set_flash(
                    "success",
                    f"Разбор разметки выполнен для {processed} файлов записи «{base}» "
                    "(результаты сохранены в сессии).",
                )
                st.rerun()


def _render_unpaired_tab(
    overview: RecordsOverview,
    *,
    overrides: overrides_api.Overrides,
    session: TestSession | None,
    runtime: state.Runtime,
) -> None:
    """Несопоставленные: записи без разметки и разметка без RAW (связывание вручную)."""
    empty = [record for record in overview.records if not record.has_markup]
    st.markdown(f"**Записи без разметки: {len(empty)}**")
    st.caption(
        "Разметка не найдена ни по имени, ни по времени импорта. Такие записи нельзя "
        "использовать в обучении — привяжите разметку в карточке записи."
    )
    if empty:
        filters = _render_filters("records_unpaired")
        selected = [record for record in empty if _matches_filters(record, filters)]
        page = paginate(
            selected,
            page=int(st.session_state.get(KEY_PAGE_UNPAIRED, 1)),
            per_page=int(st.session_state.get(f"{KEY_PAGE_UNPAIRED}_size", 25)),
        )
        for record in page.items:
            _render_record_card(
                record,
                prefix="unpaired",
                overview=overview,
                overrides=overrides,
                session=session,
                runtime=runtime,
            )
        render_pagination(KEY_PAGE_UNPAIRED, page)
    else:
        st.success("Все записи имеют разметку.")

    st.divider()
    loose = overview.unpaired_markup
    st.markdown(f"**Разметка без RAW: {len(loose)}**")
    if not loose:
        st.success("Лишней разметки нет.")
        return

    st.caption(
        "Разметка есть, а RAW с таким именем в реестре отсутствует: RAW мог быть удалён "
        "или сохранён под другим именем. Файл можно привязать к любой записи вручную."
    )
    st.dataframe(
        [
            {
                "Файл разметки": file.file_name,
                "Размер": format_size(file.size),
                "Импорт": f"{file.import_date:%d.%m.%Y %H:%M:%S}",
                "id": str(file.id),
                "s3_path": file.s3_path,
            }
            for file in loose
        ],
        hide_index=True,
        use_container_width=True,
    )

    file_options = {
        f"{file.file_name} · {format_size(file.size)} · импорт {file.import_date:%d.%m.%Y %H:%M}": file
        for file in loose
    }
    record_options = {
        f"{record.raw.file_name} · v{record.version}/{record.group_size} · "
        f"разметка: {record.markup.file_name if record.markup else '—'}": record
        for record in overview.records
    }

    col_file, col_target = st.columns(2)
    chosen_file = col_file.selectbox("Файл разметки", list(file_options), key="records_loose_file")
    chosen_record = col_target.selectbox(
        "Записать в запись (RAW)", list(record_options), key="records_loose_target"
    )
    comment = st.text_input(
        "Комментарий к решению",
        key="records_loose_comment",
        placeholder="Например: RAW переименован вручную, разметка соответствует",
    )
    if st.button("🔗 Привязать разметку к записи", key="records_loose_link", type="primary"):
        _save_override(
            record_options[chosen_record],
            overrides_api.ACTION_LINK,
            markup_id=str(file_options[chosen_file].id),
            comment=comment,
            overrides=overrides,
            session=session,
        )


def _render_other_tab(overview: RecordsOverview) -> None:
    """Прочие файлы: модели, отчёты и всё, что не имеет маркеров `.raw`/`.markup`."""
    if not overview.other_files:
        st.success("Прочих файлов нет.")
        return

    st.caption(
        "Эти файлы не входят в записи: в имени нет маркеров `.raw`/`.markup` "
        "(модели `.h5`/`.onnx`, отчёты `.zip`, прочее). Они не «теряются» — попадают в манифест."
    )
    by_type: dict[str, int] = {}
    for file in overview.other_files:
        by_type[file.file_type] = by_type.get(file.file_type, 0) + 1
    st.caption(f"По типам: {by_type}")

    st.dataframe(
        [
            {
                "Файл": file.file_name,
                "Тип": file.file_type,
                "Размер": format_size(file.size),
                "Импорт": f"{file.import_date:%d.%m.%Y %H:%M:%S}",
                "id": str(file.id),
                "s3_path": file.s3_path,
            }
            for file in overview.other_files
        ],
        hide_index=True,
        use_container_width=True,
    )


def _render_manifest_tab(overview: RecordsOverview, *, session: TestSession | None) -> None:
    """Манифест объединения: все файлы реестра, показатели и выгрузка CSV/JSON."""
    rows = overview.manifest_rows()
    stats = overview.stats

    st.caption(
        f"Манифест содержит все файлы реестра: {stats.files} файлов = {stats.records} записей "
        f"(в том числе {stats.paired} с разметкой) + {stats.unpaired_markup} файлов разметки "
        f"без RAW + {stats.other} прочих. Строк манифеста: {len(rows)} — разметка записей "
        "указана в их же строках."
    )
    if overview.manifest_is_complete():
        st.success(
            "Контроль полноты пройден: каждый файл реестра попал в манифест ровно один раз "
            "(TC-REC-02)."
        )
    else:
        ids = overview.manifest_file_ids()
        st.error(
            f"Контроль полноты не пройден: файлов в реестре {stats.files}, в манифесте "
            f"{len(ids)} (уникальных {len(set(ids))})."
        )

    st.dataframe(rows[:MANIFEST_PREVIEW_ROWS], hide_index=True, use_container_width=True)
    if len(rows) > MANIFEST_PREVIEW_ROWS:
        st.caption(f"Показаны первые {MANIFEST_PREVIEW_ROWS} строк — полный файл в выгрузке.")

    with st.expander("Показатели объединения (входят в отчёт об испытаниях)"):
        st.table(overview.stats_rows())

    meta = {
        "session_id": session.session_id if session else None,
        "session_status": session.status if session else "сессия не выбрана",
        "operator": session.info.operator_fio if session else "",
        "server_build": session.server_build if session else "",
        "base_url": session.base_url if session else "",
    }
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    col_csv, col_json, col_save = st.columns(3)
    col_csv.download_button(
        "⬇ CSV (Excel, utf-8-sig)",
        data=overview.manifest_csv().encode("utf-8-sig"),
        file_name=f"records_manifest_{stamp}.csv",
        mime="text/csv",
        key="records_manifest_csv",
        use_container_width=True,
    )
    col_json.download_button(
        "⬇ JSON",
        data=overview.manifest_json(meta),
        file_name=f"records_manifest_{stamp}.json",
        mime="application/json",
        key="records_manifest_json",
        use_container_width=True,
    )
    if col_save.button(
        "💾 Сохранить в артефакты сессии",
        key="records_manifest_save",
        use_container_width=True,
    ):
        csv_path, json_path = save_manifest(overview, meta=meta)
        log_event(
            "records_manifest_saved",
            f"Манифест объединения сохранён: {csv_path.name}",
            module="records",
            check_id=LABEL_DUPLICATES,
            payload={
                "csv": csv_path.as_posix(),
                "json": json_path.as_posix(),
                "rows": len(rows),
                "session_id": meta["session_id"],
                "stats": stats.to_dict(),
            },
        )
        if session is None:
            set_flash(
                "warning",
                f"Манифест сохранён: {csv_path.name}, {json_path.name} — сессия не выбрана.",
            )
        else:
            add_artifact(
                session,
                kind="records_manifest (CSV)",
                path=csv_path,
                note=f"Объединение в записи: {len(rows)} строк, окно {overview.window_seconds} с",
            )
            add_artifact(
                session,
                kind="records_manifest (JSON)",
                path=json_path,
                note=f"Объединение в записи: {len(rows)} строк",
            )
            state.store_session(session)
            set_flash(
                "success",
                f"Манифест сохранён и зарегистрирован в сессии: {csv_path.name}, {json_path.name}",
            )
        st.rerun()
