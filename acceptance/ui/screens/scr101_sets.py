"""`SCR-101` Наборы проверок — Планирование испытаний (Ф0).

Макет экрана — `docs/16`, раздел «SCR-101. Наборы проверок»; требования ТЗ: `FR-P-65…FR-P-67`,
`DR-P-13`, `IR-P-17`, `IR-P-18`, `IR-P-21`.

Экран отвечает на вопрос руководителя «что и в каком объёме мы испытываем»: слева каталог
проверок с фильтрами и статусами текущей сессии, справа состав набора с порядком, KPI и
ревизиями. Набор — документ планирования, поэтому живут здесь и утверждение, и новая ревизия.

Чего экран **не делает** (anti-goals макета): ни одной команды запуска (`IR-P-18`) и ни одного
статуса «успех/отказ» как своего результата — статусы строк каталога только **читаются** из
`results.py` (`DR-P-5`). Каталог проверок не редактируется: это источник состава (`docs/02`).

Данные экрана: библиотека наборов (`sets.py`, файл `acceptance_data/check_sets.json`) и текущая
сессия — только для чтения статусов. Правка набора сохраняется целиком (`save_sets`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import streamlit as st

from acceptance import results as results_api
from acceptance import sets as sets_api
from acceptance.checks import catalog
from acceptance.checks.registry import CheckClass
from acceptance.session import TestSession
from acceptance.ui import state
from acceptance.ui.components import flash, layout, status
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr101_sets"

#: Куда ведёт экран: план собирается в программу сессии (`SCR-102`).
PROGRAMME_SCREEN = "scr102_programme"

#: Экран проверки состава каталога (`docs/02` — источник, а не правка).
STAND_SCREEN = "scr202_stand"

#: Значение фильтров «без ограничения».
ANY = "все"

#: Членство проверки в наборе — фильтр каталога (`IR-P-17`).
MEMBERSHIP_ALL = ANY
MEMBERSHIP_IN = "в наборе"
MEMBERSHIP_OUT = "не в наборе"
MEMBERSHIP_CHOICES: tuple[str, ...] = (MEMBERSHIP_ALL, MEMBERSHIP_IN, MEMBERSHIP_OUT)

#: Пометка строки каталога и состава.
MARK_IN = "☑"
MARK_OUT = "☐"
MARK_MANDATORY = "★"

#: Пометка строки, включённой в набор, но заблокированной дефектом API.
BLOCKED_NOTE = "в наборе, но блокирована API"

#: Целевые объёмы набора (поле набора, `FR-P-65`).
SCOPES = sets_api.SCOPES

#: Содержимое экрана: блоки, состояния и переходы макета (`docs/16`).
CONTENT = ScreenContent(
    purpose="Собрать и утвердить наборы проверок: каталог слева, состав набора справа.",
    blocks=(
        "Каталог проверок: фильтры по разделу и классу, поиск, статус сессии у строки",
        "Массовый выбор по фильтру («выбрать все отфильтрованные»)",
        "Состав набора и порядок (↑ ↓, «в начало»), обязательный смоук-минимум",
        "KPI набора: объём и оценка карточек запуска для live/heavy",
        "Ревизии и утверждение: сохранение ревизии, автор, история ревизий",
        "Перенос: экспорт/импорт json, копирование, удаление черновика",
    ),
    states=(
        "Пусто: «Наборов нет: создайте набор из каталога или возьмите шаблон „Смоук“»",
        "Утверждён: правки запрещены — нужна новая ревизия",
        "Нет сессии: статусы сессии не показываются, набор проектируется без привязки",
    ),
    transitions=(
        ("SCR-102", "Программа сессии"),
        ("SCR-202", "Стенд"),
    ),
    requirements=(
        "FR-P-65…FR-P-67",
        "DR-P-13",
        "IR-P-17",
        "IR-P-18",
        "IR-P-21",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def catalog_rows(
    selected: sets_api.CheckSet | None = None,
    results: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Строки каталога проверок: раздел, класс, членство в наборе и статус сессии.

    Статус — только **чтение** результата текущей сессии (`DR-P-5`): планирование его не
    ставит. Проверка, включённая в набор, но заблокированная дефектом API, помечается
    отдельно — об этом руководитель должен знать до прогона (`FR-P-19`).
    """
    states = dict(results or {})
    member_ids = set(selected.check_ids) if selected is not None else set()
    rows: list[dict[str, Any]] = []
    for spec in catalog.CHECKS:
        state_row = states.get(spec.check_id) or {}
        status_text = str(state_row.get("status") or results_api.STATUS_NOT_RUN)
        icon = str(state_row.get("icon") or status.status_icon(status_text))
        member = spec.check_id in member_ids
        rows.append(
            {
                "check_id": spec.check_id,
                "title": spec.title,
                "section": catalog.group_of(spec.check_id) or spec.module,
                "check_class": spec.class_label,
                "class_raw": str(spec.check_class),
                "requirement": spec.requirement,
                "member": member,
                "mark": MARK_IN if member else MARK_OUT,
                "status": f"{icon} {status_text}",
                "status_raw": status_text,
                "blocked": bool(spec.blocked_by_api),
                "note": BLOCKED_NOTE if member and spec.blocked_by_api else "",
            }
        )
    return rows


def filter_catalog(
    rows: Sequence[Mapping[str, Any]],
    *,
    section: str = "",
    check_class: str = "",
    membership: str = MEMBERSHIP_ALL,
    search: str = "",
) -> list[dict[str, Any]]:
    """Выборка каталога по фильтрам макета (`IR-P-17`).

    Пустое значение (или `ANY`) — без ограничения. Поиск идёт по идентификатору, названию и
    требованию: так находят проверку и по номеру, и по формулировке требования.
    """
    needle = str(search or "").strip().lower()
    selected: list[dict[str, Any]] = []
    for row in rows:
        if section and section != ANY and str(row["section"]) != section:
            continue
        if check_class and check_class != ANY and str(row["check_class"]) != check_class:
            continue
        if membership == MEMBERSHIP_IN and not row["member"]:
            continue
        if membership == MEMBERSHIP_OUT and row["member"]:
            continue
        if needle:
            haystack = f"{row['check_id']} {row['title']} {row['requirement']}".lower()
            if needle not in haystack:
                continue
        selected.append(dict(row))
    return selected


def section_options(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Варианты фильтра по разделу испытаний: «все» и разделы по алфавиту."""
    return (ANY, *sorted({str(row["section"]) for row in rows}))


def class_options(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Варианты фильтра по классу проверки: «все» и классы по алфавиту."""
    return (ANY, *sorted({str(row["check_class"]) for row in rows}))


def filtered_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Идентификаторы отфильтрованных проверок — то, что забирает массовый выбор (`IR-P-17`)."""
    return [str(row["check_id"]) for row in rows]


def member_rows(
    check_set: sets_api.CheckSet,
    results: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Строки состава набора: порядок, обязательность, класс, статус сессии и примечание."""
    states = dict(results or {})
    rows: list[dict[str, Any]] = []
    for item in check_set.items:
        spec = catalog.find(item.check_id)
        state_row = states.get(item.check_id) or {}
        status_text = str(state_row.get("status") or results_api.STATUS_NOT_RUN)
        icon = str(state_row.get("icon") or status.status_icon(status_text))
        if spec is None:
            note = "нет в каталоге: исполнена быть не может"
        elif spec.blocked_by_api:
            note = "блокирована API"
        else:
            note = ""
        rows.append(
            {
                "order": item.order,
                "mark": MARK_MANDATORY if item.mandatory else "",
                "check_id": item.check_id,
                "title": spec.title if spec is not None else "",
                "section": catalog.group_of(item.check_id),
                "check_class": spec.class_label if spec is not None else "",
                "requirement": spec.requirement if spec is not None else "",
                "status": f"{icon} {status_text}",
                "note": note,
            }
        )
    return rows


def class_counts(check_ids: Sequence[str]) -> dict[str, int]:
    """Число проверок набора по классам (`tech`/`live`/`heavy`/`manual`)."""
    counts: dict[str, int] = {str(item): 0 for item in CheckClass}
    for check_id in check_ids:
        spec = catalog.find(str(check_id))
        if spec is not None:
            key = str(spec.check_class)
            counts[key] = counts.get(key, 0) + 1
    return counts


def set_kpi(check_set: sets_api.CheckSet) -> tuple[tuple[str, Any], ...]:
    """KPI набора: объём, состав по классам и число обязательных пунктов (`FR-P-65`)."""
    counts = class_counts(check_set.check_ids)
    return (
        ("Пунктов", check_set.size),
        ("tech", counts.get(str(CheckClass.TECH), 0)),
        ("live", counts.get(str(CheckClass.LIVE), 0)),
        ("heavy", counts.get(str(CheckClass.HEAVY), 0)),
        ("manual", counts.get(str(CheckClass.MANUAL), 0)),
        ("Обязательный смоук", len(check_set.mandatory_ids)),
    )


def run_cards_note(check_ids: Sequence[str]) -> str:
    """Сколько пунктов набора потребуют карточки запуска (`FR-P-19`)."""
    needing = [
        spec
        for spec in (catalog.find(str(check_id)) for check_id in check_ids)
        if spec is not None and spec.is_confirmation_required
    ]
    if not needing:
        return "Карточки запуска: не требуются — в наборе нет live/heavy проверок"
    return (
        f"Карточки запуска: {layout.plural(len(needing), ('пункт', 'пункта', 'пунктов'))} "
        f"(live/heavy — {', '.join(spec.check_id for spec in needing[:5])}"
        + ("…" if len(needing) > 5 else "")
        + ") — без них прогон отклоняется (`FR-P-19`)"
    )


def membership_warning(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
) -> str:
    """Пересечение набора с другими наборами: в программе пункт учитывается один раз (`FR-P-68`)."""
    others = [item for item in library.sets if item.set_id != check_set.set_id]
    shared = {item.set_id: len(set(item.check_ids) & set(check_set.check_ids)) for item in others}
    overlapping = {key: value for key, value in shared.items() if value}
    if not overlapping:
        return ""
    listed = ", ".join(f"{key} — {value}" for key, value in sorted(overlapping.items())[:3])
    total = len(set(check_set.check_ids) & set().union(*(set(item.check_ids) for item in others)))
    return (
        f"Пересечение с другими наборами: "
        f"{layout.plural(total, ('проверка', 'проверки', 'проверок'))} ({listed}) — "
        "в программе они учитываются один раз (`FR-P-68`)"
    )


def mandatory_note(check_set: sets_api.CheckSet) -> str:
    """Напоминание об обязательном смоуке набора: он идёт первыми пунктами (`FR-P-67`)."""
    if not check_set.mandatory_ids:
        return "Обязательный смоук не отмечен: отметьте пункты, которые не пропускаются"
    first = check_set.check_ids[: len(check_set.mandatory_ids)]
    ordered = first == check_set.mandatory_ids
    return f"Обязательный смоук: {len(check_set.mandatory_ids)} — " + (
        "идёт первыми пунктами набора"
        if ordered
        else "порядок состава: смоук не первым — проверьте"
    )


def session_note(session: TestSession | None, library: sets_api.SetsLibrary) -> str:
    """Состояние экрана: есть ли сессия для статусов и наборы в библиотеке (`IR-P-8`)."""
    summary = library.summary()
    return layout.join_parts(
        (
            "статусы текущей сессии показаны"
            if session is not None
            else "нет сессии: статусы сессии не показываются, набор проектируется без привязки"
        ),
        f"наборов: {summary['sets']}",
        f"утверждено: {summary['approved']}",
        f"проверок в наборах: {summary['checks']}",
    )


def set_from_json(text: str) -> sets_api.CheckSet:
    """Читает набор из выгрузки `json` (импорт, `FR-P-70`).

    Raises:
        ValueError: если текст не JSON или в нём нет состава набора.
    """
    try:
        payload = json.loads(str(text or ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"импорт: файл не читается как JSON ({exc.msg})") from exc
    if not isinstance(payload, Mapping) or not payload.get("set_id"):
        raise ValueError("импорт: в файле нет набора (ожидается выгрузка кнопкой «⬇ json»)")
    return sets_api.CheckSet.from_dict(dict(payload))


def set_label(check_set: sets_api.CheckSet) -> str:
    """Подпись набора в списке выбора: идентификатор, название, объём и статус (`FR-P-66`)."""
    return layout.join_parts(
        f"{check_set.set_id} · {check_set.title}",
        f"{check_set.size} проверок",
        f"{check_set.status}, рев. {check_set.revision}",
        check_set.scope_label,
    )


# ---------------------------------------------------------------------------
# Экран: каталог, состав набора, KPI, ревизии и перенос
# ---------------------------------------------------------------------------
def render() -> None:
    """Рисует экран наборов: каталог слева, состав набора справа, ревизии в контексте."""
    session = state.current_session()
    library = state.sets_library()
    layout.render_header(KEY, session_note(session, library))
    flash.render()

    if not library.sets:
        _render_empty(session)
        return

    check_set = _render_picker(library, session)
    results = state.results_state() if session is not None else {}
    workspace, context = layout.zones()
    with workspace:
        _render_catalog(library, check_set, results)
        _render_members(library, check_set, results)
    with context:
        _render_revisions(library, check_set, session)
        _render_transfer(library, check_set)


def _author(session: TestSession | None) -> str:
    """Автор правки: ФИО оператора сессии, иначе пусто (набор живёт и без сессии)."""
    if session is None:
        return ""
    return str(session.info.operator_fio or "")


def _saved(library: sets_api.SetsLibrary, message: str) -> None:
    """Сохраняет библиотеку наборов, сообщает оператору и перерисовывает экран."""
    state.save_sets_library(library)
    flash.success(message)
    st.rerun()


def _render_empty(session: TestSession | None) -> None:
    """Состояние «наборов нет»: стартовая библиотека создаётся кнопкой, а не молча (`IR-P-8`)."""
    st.warning("Наборов нет: создайте набор из каталога или возьмите стартовый набор «Смоук».")
    author = _author(session)
    if st.button("🧩 Создать стартовые наборы из каталога", key=f"{KEY}_seed", type="primary"):
        library = state.seed_sets_library(author=author)
        flash.success(
            f"Создано наборов: {len(library.sets)} — наборы черновики, выбирайте и правьте"
        )
        st.rerun()
    if st.button("→ К программе сессии (SCR-102)", key=f"{KEY}_goto_empty", width="stretch"):
        state.go_to(PROGRAMME_SCREEN)


def _render_picker(library: sets_api.SetsLibrary, session: TestSession | None) -> sets_api.CheckSet:
    """Выбор набора и создание нового (идентификатор выводится из раздела)."""
    chosen = st.selectbox(
        "Набор",
        library.ids(),
        key=f"{KEY}_pick",
        format_func=lambda set_id: set_label(library.find(set_id) or library.sets[0]),
    )
    with st.expander("➕ Новый набор из раздела каталога", expanded=False):
        section = st.selectbox("Раздел", sets_api.section_options(), key=f"{KEY}_new_section")
        title = st.text_input(
            "Название",
            value=f"Набор «{sets_api.section_title(str(section))}»",
            key=f"{KEY}_new_title",
        )
        scope = st.selectbox("Объём", SCOPES, key=f"{KEY}_new_scope")
        if st.button("Создать набор", key=f"{KEY}_create", width="stretch"):
            _create_set(
                library,
                section=str(section),
                title=str(title),
                scope=str(scope),
                author=_author(session),
            )
    return library.find(str(chosen)) or library.sets[0]


def _create_set(
    library: sets_api.SetsLibrary,
    *,
    section: str,
    title: str,
    scope: str,
    author: str,
) -> None:
    """Создаёт пустой набор раздела: состав собирается фильтрами каталога (`FR-P-65`)."""
    try:
        created = library.create(title=title, section=section, scope=scope, author=author)
    except ValueError as exc:
        st.error(str(exc))
        return
    _saved(library, f"Набор {created.set_id} создан: добавьте проверки из каталога")


def _render_catalog(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
    results: Mapping[str, Any],
) -> None:
    """Каталог проверок: фильтры, массовый выбор и добавление в набор (`IR-P-17`)."""
    rows = catalog_rows(check_set, results)
    st.subheader(f"Каталог проверок ({len(rows)})")
    left, right = st.columns(2)
    section = left.selectbox("Раздел", section_options(rows), key=f"{KEY}_section")
    check_class = right.selectbox("Класс", class_options(rows), key=f"{KEY}_class")
    membership = left.radio("Состав", MEMBERSHIP_CHOICES, horizontal=True, key=f"{KEY}_membership")
    search = right.text_input("Поиск (идентификатор, название, требование)", key=f"{KEY}_search")
    shown = filter_catalog(
        rows,
        section=str(section),
        check_class=str(check_class),
        membership=str(membership),
        search=str(search),
    )
    st.caption(f"Отфильтровано: {len(shown)} из {len(rows)}")
    layout.rows_table(
        shown,
        key=f"{KEY}_catalog",
        columns={
            "mark": "",
            "check_id": "Проверка",
            "title": "Название",
            "section": "Раздел",
            "check_class": "Класс",
            "status": "Статус сессии",
            "note": "",
        },
        height=340,
    )
    if check_set.is_approved:
        st.info("Набор утверждён: правки состава запрещены — выпустите новую ревизию (`FR-P-67`).")
        return
    ids = filtered_ids(shown)
    if not ids:
        st.caption("Под фильтр ничего не подошло: ослабьте фильтры или очистите поиск.")
        return
    picked = st.multiselect("Добавить в набор", ids, key=f"{KEY}_add_pick")
    first, second = st.columns(2)
    if first.button(
        f"➕ Добавить выбранные ({len(picked)})",
        key=f"{KEY}_add",
        width="stretch",
        disabled=not picked,
    ):
        _add_to_set(library, check_set, [str(item) for item in picked])
    if second.button(f"➕ Все отфильтрованные ({len(ids)})", key=f"{KEY}_add_all", width="stretch"):
        _add_to_set(library, check_set, ids)


def _render_members(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
    results: Mapping[str, Any],
) -> None:
    """Состав набора: порядок, обязательные пункты и правка (`DR-P-13`, `FR-P-66`)."""
    st.divider()
    st.subheader(f"{check_set.icon} {check_set.title} ({check_set.size})")
    st.caption(
        layout.join_parts(
            f"{check_set.set_id} · раздел: {check_set.section or 'не задан'}",
            f"объём: {check_set.scope_label}",
            f"автор: {check_set.author or '—'}",
            f"ревизия {check_set.revision} · {check_set.status}",
        )
    )
    layout.kpi(set_kpi(check_set), columns=3)
    st.caption(run_cards_note(check_set.check_ids))
    st.caption(mandatory_note(check_set))
    warning = membership_warning(library, check_set)
    if warning:
        st.warning(warning)
    layout.rows_table(
        member_rows(check_set, results),
        key=f"{KEY}_members",
        columns={
            "order": "№",
            "mark": "",
            "check_id": "Проверка",
            "title": "Название",
            "section": "Раздел",
            "check_class": "Класс",
            "status": "Статус сессии",
            "note": "",
        },
        height=300,
    )
    if check_set.is_approved:
        st.info("Набор утверждён: правки состава запрещены — выпустите новую ревизию (`FR-P-67`).")
        return
    ids = check_set.check_ids
    if not ids:
        st.caption("Состав пуст: добавьте проверки из каталога слева.")
        return
    chosen = str(st.selectbox("Пункт состава", ids, key=f"{KEY}_member_pick"))
    cells = st.columns(4)
    if cells[0].button("↑ выше", key=f"{KEY}_up", width="stretch"):
        _move_in_set(library, check_set, chosen, -1)
    if cells[1].button("↓ ниже", key=f"{KEY}_down", width="stretch"):
        _move_in_set(library, check_set, chosen, 1)
    if cells[2].button("⤒ в начало", key=f"{KEY}_front", width="stretch"):
        _move_in_set(library, check_set, chosen, -len(ids))
    if cells[3].button("← убрать", key=f"{KEY}_remove", width="stretch"):
        _remove_from_set(library, check_set, chosen)
    mandatory_row = next(
        (row for row in member_rows(check_set) if row["check_id"] == chosen), {"mark": ""}
    )
    st.caption(f"обязательность пункта: {mandatory_row['mark'] or 'нет'}")
    left, right = st.columns(2)
    if left.button("★ Сделать обязательным", key=f"{KEY}_mandatory_on", width="stretch"):
        _set_mandatory(library, check_set, chosen, True)
    if right.button("☆ Снять обязательность", key=f"{KEY}_mandatory_off", width="stretch"):
        _set_mandatory(library, check_set, chosen, False)


def import_note(check_set: sets_api.CheckSet) -> str:
    """Что пришло в импортированном наборе: идентификатор, объём и раздел."""
    return layout.join_parts(
        f"{check_set.set_id} · {check_set.title}",
        f"пунктов: {check_set.size}",
        f"раздел: {check_set.section or 'не задан'}",
    )


def _render_revisions(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
    session: TestSession | None,
) -> None:
    """Ревизии и утверждение набора: документ планирования, а не рабочий черновик (`FR-P-66`)."""
    st.subheader("Ревизии и утверждение")
    author = _author(session)
    if st.button("💾 Сохранить ревизию", key=f"{KEY}_revise", width="stretch"):
        _snapshot(library, check_set, author)
    if check_set.is_approved:
        if st.button("✎ Новая ревизия", key=f"{KEY}_reopen", width="stretch"):
            _reopen(library, check_set, author)
    elif st.button("✅ Утвердить набор", key=f"{KEY}_approve", width="stretch"):
        _approve(library, check_set, author)

    rows = check_set.revision_rows()
    layout.rows_table(
        rows,
        key=f"{KEY}_revisions",
        columns={
            "revision": "№",
            "at": "Когда",
            "author": "Автор",
            "size": "Пунктов",
            "comment": "Комментарий",
            "current": "",
        },
        height=180,
    )
    archive = [int(row["revision"]) for row in rows if not row["current"]]
    if not archive:
        return
    chosen = int(st.selectbox("Архивная ревизия", archive, key=f"{KEY}_restore_pick"))
    if st.button(f"↩ Восстановить ревизию {chosen}", key=f"{KEY}_restore", width="stretch"):
        _restore(library, check_set, chosen, author)


def _render_transfer(library: sets_api.SetsLibrary, check_set: sets_api.CheckSet) -> None:
    """Перенос набора: выгрузка и импорт `json`, копирование, удаление (`FR-P-70`)."""
    st.divider()
    st.subheader("Перенос и библиотека")
    st.download_button(
        "⬇ json",
        data=json.dumps(check_set.to_dict(), ensure_ascii=False, indent=2),
        file_name=f"{check_set.set_id}.json",
        mime="application/json",
        key=f"{KEY}_export",
    )
    text = st.text_area("Импорт набора (json)", key=f"{KEY}_import_text", height=100)
    if st.button("⧇ Импорт", key=f"{KEY}_import", width="stretch"):
        _import_set(library, str(text))
    if st.button("⧉ Копировать набор", key=f"{KEY}_copy", width="stretch"):
        _copy_set(library, check_set)
    if st.button("🗑 Удалить черновик", key=f"{KEY}_delete", width="stretch"):
        _delete_set(library, check_set)
    st.divider()
    if st.button(
        "→ К программе сессии (SCR-102)",
        key=f"{KEY}_goto_programme",
        type="primary",
        width="stretch",
    ):
        state.go_to(PROGRAMME_SCREEN)
    if st.button("→ Стенд и каталог (SCR-202)", key=f"{KEY}_goto_stand", width="stretch"):
        state.go_to(STAND_SCREEN)


# ---------------------------------------------------------------------------
# Правки библиотеки: каждая сохраняет набор и объясняет результат
# ---------------------------------------------------------------------------
def _add_to_set(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
    check_ids: Sequence[str],
) -> None:
    """Добавляет проверки в состав набора (`FR-P-65`)."""
    try:
        added = check_set.add(check_ids)
    except ValueError as exc:
        st.error(str(exc))
        return
    if not added:
        st.info("Все выбранные проверки уже в наборе.")
        return
    _saved(library, f"{check_set.set_id}: в набор добавлено {len(added)}")


def _remove_from_set(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
    check_id: str,
) -> None:
    """Убирает проверку из состава набора (`DR-P-13`)."""
    try:
        removed = check_set.remove([check_id])
    except ValueError as exc:
        st.error(str(exc))
        return
    if not removed:
        st.info(f"{check_id} в наборе нет.")
        return
    _saved(library, f"{check_set.set_id}: {check_id} убран из набора")


def _move_in_set(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
    check_id: str,
    delta: int,
) -> None:
    """Меняет порядок пункта набора: порядок состава — часть документа (`DR-P-13`)."""
    try:
        moved = check_set.move(check_id, delta)
    except ValueError as exc:
        st.error(str(exc))
        return
    if not moved:
        st.info(f"{check_id}: пункт уже на краю списка.")
        return
    _saved(library, f"{check_set.set_id}: порядок пункта {check_id} изменён")


def _set_mandatory(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
    check_id: str,
    mandatory: bool,
) -> None:
    """Отмечает пункт обязательным (смоук-минимум) или снимает отметку (`FR-P-70`)."""
    try:
        changed = check_set.set_mandatory(check_id, mandatory)
    except ValueError as exc:
        st.error(str(exc))
        return
    if not changed:
        st.info(f"{check_id} в наборе нет.")
        return
    mark = "обязательный" if mandatory else "обычный"
    _saved(library, f"{check_set.set_id}: пункт {check_id} — {mark}")


def _snapshot(library: sets_api.SetsLibrary, check_set: sets_api.CheckSet, author: str) -> None:
    """Сохраняет ревизию набора: предыдущая остаётся читаемой (`FR-P-66`)."""
    try:
        revision = check_set.snapshot(author=author, comment="сохранение ревизии")
    except ValueError as exc:
        st.error(str(exc))
        return
    _saved(library, f"{check_set.set_id}: сохранена ревизия {revision.revision}")


def _approve(library: sets_api.SetsLibrary, check_set: sets_api.CheckSet, author: str) -> None:
    """Утверждает набор: после этого состав правится только новой ревизией (`FR-P-67`)."""
    try:
        revision = check_set.approve(author=author, comment="утверждение набора")
    except ValueError as exc:
        st.error(str(exc))
        return
    _saved(
        library,
        f"{check_set.set_id}: набор утверждён (ревизия {revision.revision}, "
        f"{layout.plural(check_set.size, ('пункт', 'пункта', 'пунктов'))})",
    )


def _reopen(library: sets_api.SetsLibrary, check_set: sets_api.CheckSet, author: str) -> None:
    """Открывает новую ревизию утверждённого набора под правки (`FR-P-66`)."""
    try:
        revision = check_set.reopen(author=author, comment="правка после утверждения")
    except ValueError as exc:
        st.error(str(exc))
        return
    _saved(library, f"{check_set.set_id}: открыта ревизия {revision.revision} (черновик)")


def _restore(
    library: sets_api.SetsLibrary,
    check_set: sets_api.CheckSet,
    number: int,
    author: str,
) -> None:
    """Восстанавливает состав архивной ревизии как новую ревизию набора (`FR-P-66`)."""
    try:
        revision = check_set.restore_revision(number, author=author, comment="восстановление")
    except ValueError as exc:
        st.error(str(exc))
        return
    _saved(library, f"{check_set.set_id}: состав ревизии {number} восстановлен ({revision.size})")


def _copy_set(library: sets_api.SetsLibrary, check_set: sets_api.CheckSet) -> None:
    """Копирует набор под новым идентификатором: переиспользование состава (`FR-P-70`)."""
    try:
        clone = library.copy(check_set.set_id)
    except ValueError as exc:
        st.error(str(exc))
        return
    _saved(library, f"Создана копия набора: {clone.set_id}")


def _delete_set(library: sets_api.SetsLibrary, check_set: sets_api.CheckSet) -> None:
    """Удаляет набор-черновик: утверждённый набор удалить нельзя (`FR-P-67`)."""
    try:
        removed = library.remove(check_set.set_id)
    except ValueError as exc:
        st.error(str(exc))
        return
    if not removed:
        st.info(f"Набор {check_set.set_id} не найден в библиотеке.")
        return
    _saved(library, f"Набор {check_set.set_id} удалён из библиотеки")


def _import_set(library: sets_api.SetsLibrary, text: str) -> None:
    """Импортирует набор из выгрузки `json`: занятый идентификатор заменяется свободным (`FR-P-70`)."""
    try:
        imported = set_from_json(text)
    except ValueError as exc:
        st.error(str(exc))
        return
    if library.find(imported.set_id) is not None:
        imported = imported.copy(
            set_id=sets_api.suggested_set_id(imported.section, taken=library.ids()),
            title=f"{imported.title} (импорт)",
        )
    library.upsert(imported)
    _saved(library, f"Импортирован набор: {import_note(imported)}")
