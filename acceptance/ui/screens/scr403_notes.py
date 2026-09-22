"""`SCR-403` Замечания к API и перспективные требования — Результаты (Ф2–Ф3).

Макет экрана — `docs/16`, раздел «SCR-403. Замечания к API»; требования ТЗ: `FR-P-31`,
`FR-P-37…FR-P-40`, `FR-P-44`, `DR-P-6`.

Экран ведёт дефекты и пробелы API: что нашли, как воспроизвести, что передать разработчику и
как закрыть после проверки новой сборкой. Замечание создаётся **из записи ленты, из проверки и
вручную** (`FR-P-38`) — связи «замечание ↔ проверка ↔ обмены» сохраняются в самой записи.

Чего экран **не делает** (anti-goals макета): не меняет статусы проверок (`DR-P-5`) и не
запускает прогон — проверка исправления идёт единственной командой «▶ Следующая» на `SCR-301`
(`IR-P-4`), а набор «регресс после исправления» собирается на `SCR-102` (`FR-P-41`, `FR-P-44`).

Замечание без воспроизведения помечается: такое замечание **блокирует готовность отчёта**
(`FR-P-48`), и об этом экран говорит прямо.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from acceptance import notes as notes_api
from acceptance import report as report_api
from acceptance import results as results_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckStatus
from acceptance.paths import ARTIFACT_DIR, ensure_dirs
from acceptance.session import TestSession, add_artifact, add_note_to_session
from acceptance.ui import state
from acceptance.ui.components import flash, layout
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr403_notes"

#: Куда ведут действия экрана (запуска среди них нет — `IR-P-4`).
CHECK_SCREEN = "scr302_check"
JOURNAL_SCREEN = "scr402_journal"
REPORT_SCREEN = "scr404_report"
COMPARE_SCREEN = "scr405_compare"
RUN_SCREEN = "scr301_run"
PROGRAMME_SCREEN = "scr102_programme"

#: Значение фильтров «без ограничения».
ANY = "все"

#: Пустая ячейка таблицы.
DASH = "—"

#: Вкладки экрана: замечания и перспективные требования (бэклог, `FR-P-39`).
TAB_NOTES = "Замечания"
TAB_PROSPECTIVE = "Перспективные требования"
TABS: tuple[str, ...] = (TAB_NOTES, TAB_PROSPECTIVE)

#: Пометка неполного замечания: без воспроизведения его нельзя передать разработчику (`FR-P-48`).
INCOMPLETE_MARK = "нужно воспроизведение"

#: Статусы проверок, по которым предлагается создать замечание (`FR-P-31`, `FR-P-38`).
DEFECT_STATUSES: tuple[str, ...] = (str(CheckStatus.FAILED), str(CheckStatus.BLOCKED))

#: Содержимое экрана: блоки, состояния и переходы макета (`docs/16`).
CONTENT = ScreenContent(
    purpose="Вести реестр замечаний к API и перспективных требований, собирать комплект.",
    blocks=(
        "Реестр замечаний: приоритет P0/P1/P2, модуль, эндпоинт, статус",
        "Карточка замечания: факт, ожидание, воспроизведение (curl), доказательства",
        "Перспективные требования к API отдельным реестром (`FR-P-39`)",
        "Комплект для разработчика: md/json/csv со ссылками на обмены",
        "Перепрогон по замечаниям через набор «регресс»",
    ),
    states=(
        "Пусто: замечаний нет",
        "Открытые P0: показаны в панели «Внимание» (SCR-201)",
        "Без воспроизведения: замечание помечено как неполное",
    ),
    transitions=(
        ("SCR-402", "Журнал обмена"),
        ("SCR-405", "Сравнение сессий"),
    ),
    requirements=(
        "FR-P-31",
        "FR-P-37…FR-P-40",
        "FR-P-44",
        "DR-P-6",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def is_complete(note: notes_api.ApiNote) -> bool:
    """True, если замечание можно передавать: есть воспроизведение (`FR-P-48`)."""
    return bool(str(note.reproduction or "").strip())


def note_links(note: notes_api.ApiNote) -> str:
    """Связи замечания: проверка и обмены (`FR-P-38`), иначе — пустая строка."""
    return layout.join_parts(
        f"проверка {note.check_id}" if note.check_id else "",
        f"обмены {note.evidence}" if str(note.evidence or "").strip() else "",
    )


def note_rows(notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> list[dict[str, Any]]:
    """Строки реестра замечаний: приоритет, статус, модуль, проверка, эндпоинт и связи."""
    rows: list[dict[str, Any]] = []
    for note in notes_api.sorted_notes(notes):
        rows.append(
            {
                "note_id": note.note_id,
                "priority": note.priority,
                "status": note.status,
                "module": note.module,
                "check_id": note.check_id or DASH,
                "title": note.title,
                "endpoint": note.endpoint or DASH,
                "source": note.source,
                "links": note_links(note) or DASH,
                "mark": "" if is_complete(note) else INCOMPLETE_MARK,
            }
        )
    return rows


def filter_notes(
    notes: Sequence[dict[str, Any] | notes_api.ApiNote],
    *,
    priority: str = "",
    module: str = "",
    status_value: str = "",
    search: str = "",
    only_incomplete: bool = False,
) -> list[notes_api.ApiNote]:
    """Выборка реестра замечаний: приоритет, модуль, статус, поиск (`DR-P-6`)."""
    needle = str(search or "").strip().lower()
    selected: list[notes_api.ApiNote] = []
    for note in notes_api.sorted_notes(notes):
        if priority and priority != ANY and note.priority != priority:
            continue
        if module and module != ANY and note.module != module:
            continue
        if status_value and status_value != ANY and note.status != status_value:
            continue
        if only_incomplete and is_complete(note):
            continue
        if needle:
            haystack = " ".join(
                (
                    note.title,
                    note.module,
                    note.endpoint,
                    note.fact,
                    note.expected,
                    str(note.check_id or ""),
                )
            ).lower()
            if needle not in haystack:
                continue
        selected.append(note)
    return selected


def priority_options(notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> tuple[str, ...]:
    """Варианты фильтра по приоритету: «все» и приоритеты выборки по старшинству."""
    present = {note.priority for note in notes_api.sorted_notes(notes)}
    return (ANY, *[item for item in notes_api.PRIORITIES if item in present])


def module_options(notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> tuple[str, ...]:
    """Варианты фильтра по модулю: «все» и модули выборки по алфавиту."""
    return (ANY, *sorted({note.module for note in notes_api.sorted_notes(notes)}))


def status_filter_options(
    notes: Sequence[dict[str, Any] | notes_api.ApiNote],
) -> tuple[str, ...]:
    """Варианты фильтра по статусу замечания: «все» и статусы выборки (`FR-P-37`)."""
    present = {note.status for note in notes_api.sorted_notes(notes)}
    return (ANY, *[item for item in notes_api.NOTE_STATUSES if item in present])


def incomplete_note(notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> str:
    """Сколько замечаний без воспроизведения: они блокируют готовность отчёта (`FR-P-48`)."""
    waiting = [note for note in notes_api.sorted_notes(notes) if not is_complete(note)]
    if not waiting:
        return ""
    listed = ", ".join(f"{note.priority} · {note.title[:40]}" for note in waiting[:3])
    return (
        f"{INCOMPLETE_MARK}: "
        f"{layout.plural(len(waiting), ('замечание', 'замечания', 'замечаний'))} ({listed}"
        + ("…)" if len(waiting) > 3 else ")")
        + " — без воспроизведения замечание блокирует готовность отчёта (`FR-P-48`)"
    )


def defects_note(notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> str:
    """Сводка реестра: сколько замечаний и как они распределены по статусам (`DR-P-6`)."""
    items = notes_api.sorted_notes(notes)
    if not items:
        return "Замечаний нет: расхождения фиксируются при выполнении проверок (`FR-P-31`)"
    counters = [
        f"{status}: {sum(1 for note in items if note.status == status)}"
        for status in notes_api.NOTE_STATUSES
    ]
    return layout.join_parts(
        f"Замечаний {len(items)}",
        *counters,
        f"без воспроизведения: {sum(1 for note in items if not is_complete(note))}",
    )


def note_card_lines(note: notes_api.ApiNote) -> list[str]:
    """Карточка замечания: приоритет, статус, факт, ожидание, воспроизведение, связи."""
    lines = [
        f"**{note.title}**",
        layout.join_parts(
            f"приоритет: {note.priority} ({notes_api.PRIORITY_HINTS.get(note.priority, '')})",
            f"статус: {note.status}",
            f"модуль: {note.module}",
            f"источник: {note.source}",
        ),
    ]
    if note.endpoint:
        lines.append(f"эндпоинт: `{note.endpoint}`")
    if note.fact:
        lines.append(f"факт: {note.fact}")
    if note.expected:
        lines.append(f"ожидание: {note.expected}")
    if note.reproduction:
        lines.append(f"воспроизведение: `{note.reproduction}`")
    else:
        lines.append(f"воспроизведение: нет — {INCOMPLETE_MARK} (`FR-P-48`)")
    if note.evidence:
        lines.append(f"доказательства: обмены {note.evidence}")
    links = note_links(note)
    if links:
        lines.append(f"связи: {links}")
    if note.status_note:
        lines.append(f"комментарий статуса: {note.status_note}")
    return lines


def defect_candidates(session: TestSession) -> list[str]:
    """Проверки сессии с отказом или блокировкой — кандидаты в замечания (`FR-P-31`)."""
    return [
        str(record.get("check_id") or "")
        for record in results_api.records_of(session)
        if str(record.get("status") or "") in DEFECT_STATUSES
    ]


def note_from_record(check_id: str, *, evidence: str = "") -> notes_api.ApiNote:
    """Замечание из проверки: описание берётся из каталога и реестра дефектов (`FR-P-38`).

    Если проверки нет в каталоге, замечание всё равно создаётся: пункт программы должен быть
    объяснён, даже когда каталог изменился.
    """
    spec = catalog.find(check_id)
    known = notes_api.defect_for(check_id)
    module = spec.module if spec is not None else "Прочее"
    endpoint = ", ".join(spec.endpoints[:1]) if spec is not None and spec.endpoints else ""
    expected = spec.expected if spec is not None else ""
    fact = f"проверка {check_id} завершилась расхождением: разбор в журнале обмена"
    if known:
        module = str(known.get("module") or module)
        endpoint = str(known.get("endpoint") or endpoint)
        fact = str(known.get("fact") or fact)
        expected = str(known.get("expected") or expected)
    return notes_api.new_note(
        f"Расхождение по проверке {check_id}",
        module=module,
        endpoint=endpoint,
        priority=priority_for(check_id),
        fact=fact,
        expected=expected or "поведение по требованиям сессии",
        reproduction="",
        check_id=check_id,
        evidence=evidence,
    )


def priority_for(check_id: str) -> str:
    """Приоритет замечания по проверке: у известного дефекта берётся его приоритет (`FR-P-31`)."""
    known = notes_api.defect_for(check_id)
    return str(known.get("priority") or "P1") if known else "P1"


def prospective_rows() -> list[dict[str, Any]]:
    """Перспективные требования к API отдельным реестром (`FR-P-39`)."""
    return [
        {"title": str(item.get("title") or ""), "why": str(item.get("why") or "")}
        for item in notes_api.prospective_requirements()
    ]


def bundle_markdown(notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> str:
    """Комплект для разработчика в `md`: карточки замечаний со связями (`FR-P-40`)."""
    items = notes_api.sorted_notes(notes)
    lines = ["# Замечания к API", ""]
    if not items:
        lines.append("_замечаний нет_")
    for note in items:
        lines.append(f"## {note.note_id} · {note.priority} · {note.status} · {note.module}")
        lines.append("")
        for line in note_card_lines(note):
            lines.append(f"- {line}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def bundle_json(notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> str:
    """Комплект для разработчика в `json`: реестр замечаний целиком (`FR-P-40`)."""
    items = [note.to_dict() for note in notes_api.sorted_notes(notes)]
    return json.dumps({"notes": items, "count": len(items)}, ensure_ascii=False, indent=2)


def bundle_csv(notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> str:
    """Комплект для разработчика в `csv`: колонки приложения отчёта (`FR-P-40`)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(report_api.NOTE_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for note in notes_api.sorted_notes(notes):
        row = note.to_dict()
        writer.writerow({column: row.get(column, "") for column in report_api.NOTE_COLUMNS})
    return buffer.getvalue()


def bundle_name(session: TestSession) -> str:
    """Имя комплекта замечаний: `notes_<сессия>` (расширение добавляет выгрузка)."""
    return f"notes_{session.session_id}"


def save_bundle(session: TestSession, notes: Sequence[dict[str, Any] | notes_api.ApiNote]) -> Path:
    """Сохраняет комплект замечаний в `artifacts/` и регистрирует файлы в сессии (`FR-P-40`)."""
    ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{bundle_name(session)}_{stamp}"
    files: dict[str, tuple[Path, str, str]] = {
        "notes_md": (ARTIFACT_DIR / f"{stem}.md", bundle_markdown(notes), "utf-8"),
        "notes_json": (ARTIFACT_DIR / f"{stem}.json", bundle_json(notes), "utf-8"),
        "notes_csv": (ARTIFACT_DIR / f"{stem}.csv", bundle_csv(notes), "utf-8-sig"),
    }
    note = f"комплект замечаний к API: {layout.plural(len(notes), ('запись', 'записи', 'записей'))}"
    for kind, (path, text, encoding) in files.items():
        path.write_text(text, encoding=encoding)
        add_artifact(session, kind=kind, path=path, note=note)
    return files["notes_md"][0]


def note_label(note: notes_api.ApiNote) -> str:
    """Подпись замечания в списке выбора: приоритет, номер, статус, заголовок (`FR-P-37`)."""
    return layout.join_parts(f"{note.priority} · {note.note_id}", note.status, note.title[:60])


# ---------------------------------------------------------------------------
# Экран: реестр, карточка, создание замечаний и комплект
# ---------------------------------------------------------------------------
def render() -> None:
    """Рисует замечания: фильтры, реестр, карточку, создание, комплект и переходы."""
    session = state.current_session()
    if session is None:
        layout.render_header(KEY, "сессия не выбрана")
        flash.render()
        layout.render_no_session(what="Замечания ведутся в сессии (`DR-P-6`)")
        return

    notes = list(session.notes)
    layout.render_header(KEY, defects_note(notes))
    flash.render()
    tab = st.radio("Раздел", TABS, horizontal=True, key=f"{KEY}_tab")
    if tab == TAB_PROSPECTIVE:
        _render_prospective()
        return

    workspace, context = layout.zones()
    with workspace:
        shown = _render_registry(notes)
        _render_card(session, shown)
        _render_create(session)
    with context:
        _render_exports(session, notes)
        _render_context(session, notes)


def _render_registry(notes: Sequence[dict[str, Any]]) -> list[notes_api.ApiNote]:
    """Реестр замечаний: фильтры по приоритету, модулю, статусу и поиск (`DR-P-6`)."""
    st.subheader(f"Реестр замечаний ({len(notes)})")
    left, right = st.columns(2)
    priority = left.selectbox("Приоритет", priority_options(notes), key=f"{KEY}_priority")
    module = right.selectbox("Модуль", module_options(notes), key=f"{KEY}_module")
    status_value = left.selectbox("Статус", status_filter_options(notes), key=f"{KEY}_status")
    only_incomplete = right.checkbox("только без воспроизведения", key=f"{KEY}_incomplete")
    search = st.text_input("поиск (заголовок, модуль, эндпоинт, факт)", key=f"{KEY}_search")
    shown = filter_notes(
        notes,
        priority=str(priority),
        module=str(module),
        status_value=str(status_value),
        search=str(search),
        only_incomplete=bool(only_incomplete),
    )
    st.caption(defects_note(shown) if shown else "под фильтр ничего не подошло")
    warning = incomplete_note(shown)
    if warning:
        st.warning(warning)
    layout.rows_table(
        note_rows(shown),
        key=f"{KEY}_table",
        columns={
            "priority": "P",
            "note_id": "№",
            "status": "Статус",
            "module": "Модуль",
            "check_id": "Проверка",
            "title": "Замечание",
            "endpoint": "Эндпоинт",
            "source": "Источник",
            "links": "Связи",
            "mark": "",
        },
        height=300,
    )
    return shown


def _render_card(session: TestSession, shown: Sequence[notes_api.ApiNote]) -> None:
    """Карточка замечания и смена статуса с комментарием (`FR-P-37`, `DR-P-6`)."""
    st.divider()
    st.subheader("Карточка замечания")
    if not shown:
        st.caption("В выборке нет замечаний: измените фильтры или создайте замечание ниже.")
        return
    ids = [note.note_id for note in shown]
    chosen = str(
        st.selectbox(
            "Замечание",
            ids,
            key=f"{KEY}_pick",
            format_func=lambda note_id: note_label(
                next(item for item in shown if item.note_id == note_id)
            ),
        )
    )
    note = next(item for item in shown if item.note_id == chosen)
    for line in note_card_lines(note):
        st.markdown(line)
    status = st.selectbox("Новый статус", list(notes_api.NOTE_STATUSES), key=f"{KEY}_new_status")
    comment = st.text_area(
        "Комментарий к статусу (обязателен при закрытии)",
        key=f"{KEY}_status_note",
        height=70,
    )
    if st.button("Записать статус", key=f"{KEY}_apply_status", width="stretch"):
        _apply_status(session, note, str(status), str(comment))
    left, right = st.columns(2)
    if left.button("→ Журнал обмена (SCR-402)", key=f"{KEY}_goto_journal", width="stretch"):
        state.go_to(JOURNAL_SCREEN)
    if note.check_id and right.button(
        f"→ Карточка проверки {note.check_id} (SCR-302)",
        key=f"{KEY}_goto_check",
        width="stretch",
    ):
        st.session_state[state.KEY_CHECK] = str(note.check_id)
        state.go_to(CHECK_SCREEN)


def _render_create(session: TestSession) -> None:
    """Создание замечания: из проверки, из известного дефекта и вручную (`FR-P-38`)."""
    st.divider()
    st.subheader("Новое замечание")
    left, right = st.columns(2)
    candidates = defect_candidates(session)
    with left.expander("Из проверки сессии", expanded=False):
        if not candidates:
            st.caption(
                "Проверок с отказом или блокировкой в сессии нет: замечание можно создать вручную."
            )
        else:
            check_id = st.selectbox("Проверка", candidates, key=f"{KEY}_from_check")
            evidence = st.text_input(
                "Доказательства (номера обменов, например #88, #92)",
                key=f"{KEY}_from_check_evidence",
            )
            if st.button("Создать по проверке", key=f"{KEY}_add_check", width="stretch"):
                _add_note(session, note_from_record(str(check_id), evidence=str(evidence)))
    with right.expander("Из известного дефекта", expanded=False):
        known = notes_api.known_defect_notes()
        titles = [note.title for note in known]
        title = st.selectbox("Дефект", titles, key=f"{KEY}_known_title")
        if st.button("Добавить в реестр", key=f"{KEY}_add_known", width="stretch"):
            _add_note(session, next(note for note in known if note.title == str(title)))
    with st.expander("Вручную", expanded=False):
        _render_manual_form(session)


def _render_manual_form(session: TestSession) -> None:
    """Ручное замечание: все поля `FR-P-37` в одной форме; без заголовка не создаётся."""
    title = st.text_input("Заголовок (обязательно)", key=f"{KEY}_new_title")
    left, right = st.columns(2)
    module = left.selectbox("Модуль", notes_api.MODULES, key=f"{KEY}_new_module")
    priority = left.selectbox("Приоритет", notes_api.PRIORITIES, key=f"{KEY}_new_priority")
    endpoint = right.text_input("Эндпоинт", key=f"{KEY}_new_endpoint")
    check_id = right.text_input("Проверка (идентификатор)", key=f"{KEY}_new_check")
    fact = st.text_area("Факт", key=f"{KEY}_new_fact", height=70)
    expected = st.text_area("Ожидание", key=f"{KEY}_new_expected", height=70)
    reproduction = st.text_area("Воспроизведение (curl)", key=f"{KEY}_new_repro", height=70)
    evidence = st.text_input("Доказательства (номера обменов)", key=f"{KEY}_new_evidence")
    if st.button("Создать замечание", key=f"{KEY}_add_manual", width="stretch"):
        _create_manual(
            session,
            title=str(title),
            module=str(module),
            priority=str(priority),
            endpoint=str(endpoint),
            check_id=str(check_id),
            fact=str(fact),
            expected=str(expected),
            reproduction=str(reproduction),
            evidence=str(evidence),
        )


def _create_manual(
    session: TestSession,
    *,
    title: str,
    module: str,
    priority: str,
    endpoint: str,
    check_id: str,
    fact: str,
    expected: str,
    reproduction: str,
    evidence: str,
) -> None:
    """Создаёт ручное замечание через ядро (`notes.new_note`) и сохраняет сессию (`FR-P-37`)."""
    if not str(title or "").strip():
        st.error("Замечание без заголовка не принимается: заголовок попадает в отчёт и комплект.")
        return
    _add_note(
        session,
        notes_api.new_note(
            title,
            module=module,
            priority=priority,
            endpoint=endpoint,
            check_id=str(check_id).strip().upper() or None,
            fact=fact,
            expected=expected,
            reproduction=reproduction,
            evidence=evidence,
        ),
    )


def _add_note(session: TestSession, note: notes_api.ApiNote) -> None:
    """Добавляет замечание в реестр сессии и сохраняет её (`FR-P-38`).

    Дубль по заголовку не создаётся: реестр — документ, второй экземпляр одного дефекта
    только запутает разбор (`notes.has_note`).
    """
    if notes_api.has_note(session.notes, note.title):
        st.info(f"Замечание «{note.title[:60]}» уже есть в реестре — обновите его статус.")
        return
    add_note_to_session(session, note)
    state.store_session(session)
    flash.success(f"{note.note_id}: замечание добавлено в реестр ({note.priority})")
    st.rerun()


def _apply_status(session: TestSession, note: notes_api.ApiNote, status: str, comment: str) -> None:
    """Меняет статус замечания в реестре; закрытие без комментария отклоняется (`FR-P-37`)."""
    try:
        updated = notes_api.apply_status(session.notes, note.note_id, status, comment=comment)
    except ValueError as exc:
        st.error(str(exc))
        return
    state.store_session(session)
    flash.success(f"{note.note_id}: статус «{updated['status']}» записан в реестр")
    st.rerun()


def _render_exports(session: TestSession, notes: Sequence[dict[str, Any]]) -> None:
    """Комплект для разработчика: `md`, `json`, `csv` и сохранение в артефакты (`FR-P-40`)."""
    st.subheader("Комплект разработчику")
    stem = bundle_name(session)
    st.download_button(
        "⬇ MD",
        data=bundle_markdown(notes),
        file_name=f"{stem}.md",
        mime="text/markdown",
        key=f"{KEY}_md",
    )
    st.download_button(
        "⬇ JSON",
        data=bundle_json(notes),
        file_name=f"{stem}.json",
        mime="application/json",
        key=f"{KEY}_json",
    )
    st.download_button(
        "⬇ CSV",
        data=bundle_csv(notes),
        file_name=f"{stem}.csv",
        mime="text/csv",
        key=f"{KEY}_csv",
    )
    st.caption(
        "Комплект содержит карточки замечаний со связями «проверка ↔ обмены» — разработчику "
        "не нужно собирать их вручную (`FR-P-38`, `FR-P-40`)."
    )
    if st.button(
        "📤 Сохранить комплект в артефакты",
        key=f"{KEY}_bundle",
        disabled=not notes,
        width="stretch",
    ):
        path = save_bundle(session, notes)
        state.store_session(session)
        flash.success(f"Комплект замечаний сохранён в артефакты: {path.name}")
        st.rerun()
    st.divider()
    if st.button("→ Отчёт испытаний (SCR-404)", key=f"{KEY}_goto_report", width="stretch"):
        state.go_to(REPORT_SCREEN)
    if st.button("→ Сравнение сессий (SCR-405)", key=f"{KEY}_goto_compare", width="stretch"):
        state.go_to(COMPARE_SCREEN)


def _render_context(session: TestSession, notes: Sequence[dict[str, Any]]) -> None:
    """Панель контекста: внимание по P0, готовность отчёта и перепрогон (`FR-P-44`, `FR-P-48`)."""
    st.subheader("Внимание")
    critical = [note for note in notes_api.sorted_notes(notes) if note.priority == "P0"]
    if critical:
        st.warning(
            "P0 без решения: "
            + ", ".join(f"{note.note_id} · {note.title[:40]}" for note in critical[:3])
            + ("…" if len(critical) > 3 else "")
        )
    else:
        st.caption("Открытых P0 нет.")
    waiting = incomplete_note(notes)
    if waiting:
        st.warning(waiting)
    readiness = report_api.readiness(session)
    st.caption(
        layout.join_parts(
            f"готовность отчёта: {'да' if readiness['ready'] else 'нет'}",
            f"замечаний: {readiness['notes']['total']}",
            f"P0: {readiness['notes']['p0']}",
            f"не выполнено проверок: {readiness['checks']['not_run']}",
        )
    )
    for warning in readiness["warnings"][:3]:
        st.caption(f"⚠️ {warning}")
    st.divider()
    st.subheader("Перепрогон после исправления")
    st.caption(
        "Проверка исправления — это прогон: набор «регресс после исправления» собирается на "
        "`SCR-102` из отказов и блокировок сессии (`FR-P-41`, `FR-P-44`), а запускает его "
        "единственная команда «▶ Следующая» на `SCR-301` (`IR-P-4`)."
    )
    if st.button(
        "→ Собрать регресс (SCR-102)",
        key=f"{KEY}_goto_programme",
        type="primary",
        width="stretch",
    ):
        state.go_to(PROGRAMME_SCREEN)
    if st.button("→ К прогону (SCR-301)", key=f"{KEY}_goto_run", width="stretch"):
        state.go_to(RUN_SCREEN)


def _render_prospective() -> None:
    """Перспективные требования к API отдельным реестром (`FR-P-39`)."""
    rows = prospective_rows()
    st.subheader(f"Перспективные требования к API ({len(rows)})")
    st.caption(
        "Перспективные требования — бэклог API: они не влияют на статусы проверок и на приёмку "
        "сессии (`FR-P-39`)."
    )
    layout.rows_table(
        rows,
        key=f"{KEY}_prospective",
        columns={"title": "Требование", "why": "Зачем нужно"},
        height=420,
    )
