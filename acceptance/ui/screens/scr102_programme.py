"""`SCR-102` Программа сессии — Планирование испытаний (Ф0, Ф3).

Макет экрана — `docs/16`, раздел «SCR-102. Программа сессии»; требования ТЗ: `FR-P-41`,
`FR-P-68…FR-P-70`, `DR-P-14`, `IR-P-19`, `IR-P-21`.

Экран собирает программу испытаний: объединяет выбранные наборы по логическому **ИЛИ**
с дедупликацией и происхождением (`FR-P-68`), показывает покрытие каждого раздела против
целевого объёма (`FR-P-69`), ведёт ревизии (`DR-P-14`) и утверждает ту, которую исполнит
прогон.

Чего экран **не делает** (`IR-P-18`): не содержит ни одной команды запуска и не меняет
состав наборов — состав наборов правится на `SCR-101`. Очередь прогона здесь только
**собирается** как снимок утверждённой ревизии, а запускается она на `SCR-301`.

Правила утверждения не дублируются: экран вызывает `Programme.approve` из ядра и показывает
его текст ошибки (пустая программа, сокращение без обоснования).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import streamlit as st

from acceptance import programme as programme_api
from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance import sets as sets_api
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme
from acceptance.session import TestSession
from acceptance.ui import state
from acceptance.ui.components import flash, layout, status
from acceptance.ui.components.screen import ScreenContent

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr102_programme"

#: Статусы, из которых собирается «регресс после исправления» (`FR-P-70`).
REGRESS_STATUSES: tuple[str, ...] = (str(CheckStatus.FAILED), str(CheckStatus.BLOCKED))

#: Подписи шаблонов программы для селектора (`FR-P-70`).
TEMPLATE_HINTS = programme_api.TEMPLATE_HINTS

#: Ключ маршрута экрана «Прогон» (переход «к прогону»).
RUN_SCREEN = "scr301_run"

#: Тексты правок состава — попадают в историю сессии и в сообщение экрана.
EDIT_TEXTS: dict[str, str] = {
    "up": "пункт поднят выше",
    "down": "пункт опущен ниже",
    "star": "изменена обязательность пункта",
    "exclude": "пункт исключён из программы",
}

#: Содержимое скелета: что известно об экране из макета до реализации.
CONTENT = ScreenContent(
    purpose="Объединить наборы по ИЛИ, показать покрытие разделов и утвердить ревизию.",
    blocks=(
        "Выбор наборов сессии и шаблоны: полная / смоук / регресс",
        "Объединение по логическому ИЛИ, дедупликация и происхождение пункта",
        "Покрытие разделов против целевого объёма, непокрытые разделы",
        "Порядок пунктов: обязательные первыми, ручная правка порядка",
        "Ревизии: сохранение, дельта с предыдущей, восстановление архивной",
        "Утверждение ревизии и обоснование сокращения (`FR-P-69`)",
    ),
    states=(
        "Пусто: наборы не выбраны — программа не собрана",
        "Черновик: состав правится, прогон по черновику помечен (решение A1)",
        "Непокрытые разделы: утверждение требует обоснования сокращения",
    ),
    transitions=(
        ("SCR-101", "Наборы проверок"),
        ("SCR-301", "Прогон"),
        ("SCR-401", "Протокол проверок"),
    ),
    requirements=(
        "FR-P-41",
        "FR-P-68…FR-P-70",
        "DR-P-14",
        "IR-P-19",
        "IR-P-21",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def set_rows(library: sets_api.SetsLibrary) -> list[dict[str, Any]]:
    """Наборы библиотеки строками: идентификатор, раздел, объём, состав, статус."""
    return [
        {
            "set_id": item.set_id,
            "title": item.title,
            "section": item.section or "—",
            "scope": item.scope,
            "size": item.size,
            "mandatory": len(item.mandatory_ids),
            "status": item.status,
        }
        for item in library.sets
    ]


def set_choice_label(row: Mapping[str, Any]) -> str:
    """Подпись набора в списке выбора: «SET-FILE Файлы · 14 проверок · стандарт»."""
    return layout.join_parts(
        str(row.get("set_id") or ""),
        str(row.get("title") or ""),
        layout.plural(int(row.get("size") or 0), ("проверка", "проверки", "проверок")),
        str(row.get("scope") or ""),
    )


def items_by_set(programme: Programme) -> dict[str, int]:
    """Сколько пунктов программы пришло из каждого набора (происхождение, `FR-P-68`)."""
    counts: dict[str, int] = {}
    for item in programme.items:
        for set_id in item.source_ids:
            counts[set_id] = counts.get(set_id, 0) + 1
    return counts


def union_caption(programme: Programme) -> str:
    """«Включено по ИЛИ: 41 пункт (SET-FILE 14 · …) · пересечений: 3»."""
    counts = items_by_set(programme)
    detail = " · ".join(f"{set_id} {count}" for set_id, count in counts.items())
    return layout.join_parts(
        f"Включено по ИЛИ: {layout.plural(programme.size, ('пункт', 'пункта', 'пунктов'))}",
        f"({detail})" if detail else "",
        f"пересечений: {sum(1 for item in programme.items if len(item.sources) > 1)}",
    )


def coverage_mark(row: Mapping[str, Any]) -> str:
    """Пометка раздела: ✅ покрыт, ◐ частично, ⛔ не покрыт (пиктограммы макета)."""
    if not row.get("uncovered"):
        return "✅"
    return "⛔" if int(row.get("in_programme") or 0) == 0 else "◐"


def coverage_rows(programme: Programme) -> list[dict[str, str]]:
    """Покрытие разделов: «в программе / цель», объём набора и пометка (`IR-P-19`).

    В первой колонке — ключ раздела вместе с названием: у модуля может быть своё имя
    («TC-SYS · System»), и по одной подписи разделы не различить.
    """
    return [
        {
            "section": layout.join_parts(str(row["section"]), str(row["title"])),
            "in_programme": f"{row['in_programme']}/{row['target']}",
            "mark": coverage_mark(row),
            "implemented": f"{row['in_programme']}/{row['implemented']}",
            "planned": str(row["planned"]),
            "missing": str(row["missing"]),
            "sets": ", ".join(str(item) for item in row["sets"]) or "—",
        }
        for row in programme.coverage()
    ]


def short_ids(sign: str, ids: Sequence[str], head: int = 4) -> str:
    """Короткий список идентификаторов: «+4 (TC-DS-03, TC-DS-04, … всего 4)»."""
    if not ids:
        return ""
    shown = ", ".join(ids[:head])
    tail = f", … всего {len(ids)}" if len(ids) > head else ""
    return f"{sign}{len(ids)} ({shown}{tail})"


def delta_caption(programme: Programme) -> str:
    """«Дельта с ревизией 1 · +4 (…) · −1 (…)» — что изменилось с прошлой ревизии."""
    delta = programme.delta()
    if delta.get("missing"):
        return "ревизия для сравнения не найдена"
    if delta.get("revision") is None:
        return "первая ревизия: сравнивать не с чем"
    return layout.join_parts(
        f"Дельта с ревизией {delta['revision']}",
        short_ids("+", [str(item) for item in (delta.get("added") or [])]),
        short_ids("−", [str(item) for item in (delta.get("removed") or [])]),
    )


def programme_table_rows(
    programme: Programme,
    results: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Строки таблицы программы: порядок, обязательность, происхождение, статус."""
    return [
        {
            "order": row["order"],
            "mark": "★" if row["mandatory"] else "",
            "check_id": row["check_id"],
            "title": row["title"],
            "section": row["section_title"] or row["section"],
            "check_class": status.class_label(str(row["check_class"])),
            "sources": row["sources"],
            "note": "" if row["in_catalog"] else "нет в каталоге",
            "status": status.result_text(row) if row["status"] else "",
        }
        for row in programme.rows(results)
    ]


def revision_table_rows(programme: Programme) -> list[dict[str, Any]]:
    """История ревизий строками: номер, когда, состояние, автор, состав, комментарий."""
    return [
        {
            "revision": row["revision"],
            "at": layout.short_time(row["at"]),
            "state": "утверждена" if row["approved"] else "черновик",
            "current": "текущая" if row["current"] else "",
            "author": row["author"],
            "size": row["size"],
            "comment": str(row["comment"] or row["reduction"] or ""),
        }
        for row in programme.revision_rows()
    ]


def regress_source(session: TestSession) -> list[str]:
    """Отказы и блокировки сессии — источник шаблона «регресс» (`FR-P-70`)."""
    rows = results_api.state(session)
    return [
        check_id for check_id, row in rows.items() if str(row.get("status")) in REGRESS_STATUSES
    ]


def selection_defaults(programme: Programme) -> list[str]:
    """Что отмечено в списке наборов: наборы, из которых собрана текущая программа."""
    return programme.selected_set_ids()


# ---------------------------------------------------------------------------
# Экран: сборка, покрытие, состав, утверждение и ревизии
# ---------------------------------------------------------------------------
def render() -> None:
    """Рисует экран: сборка программы, покрытие разделов, состав и утверждение ревизии."""
    library = state.sets_library()
    programme = state.current_programme()
    layout.render_header(KEY, programme.status_label if programme.size else "программа не собрана")
    flash.render()

    session = state.current_session()
    if session is None:
        layout.render_no_session(what="Программа хранится в сессии (`DR-P-14`)")
        _render_library(library)
        return

    workspace, context = layout.zones()
    with workspace:
        _render_builder(session, library, programme)
        _render_coverage(programme)
        _render_items(session, programme)
    with context:
        _render_actions(session, library, programme)


def _render_library(library: sets_api.SetsLibrary) -> None:
    """Библиотека наборов — контекст сборки: видно, из чего собирается программа."""
    st.subheader("Наборы проверок")
    rows = set_rows(library)
    if not rows:
        st.info(
            "Библиотека наборов пуста: создайте наборы на экране «Наборы проверок» (`SCR-101`)."
        )
        return
    layout.rows_table(
        rows,
        key=f"{KEY}_library",
        columns={
            "set_id": "Набор",
            "title": "Название",
            "section": "Раздел",
            "scope": "Объём",
            "size": "Проверок",
            "status": "Статус",
        },
        height=240,
    )


def _render_builder(
    session: TestSession,
    library: sets_api.SetsLibrary,
    programme: Programme,
) -> None:
    """Выбор наборов, шаблоны и сборка объединения по ИЛИ (`FR-P-68`, `FR-P-70`)."""
    st.subheader("Наборы и сборка программы")
    if not library.sets:
        st.warning("Библиотека наборов пуста: без наборов программу не собрать.")
        if st.button("Создать стартовые наборы из каталога", key=f"{KEY}_seed", type="primary"):
            seeded = state.seed_sets_library(author=session.info.operator_fio)
            flash.success(
                f"Создано наборов: {len(seeded.sets)} — они наполнены проверками каталога"
            )
            st.rerun()
        return

    labels = {row["set_id"]: set_choice_label(row) for row in set_rows(library)}
    chosen = st.multiselect(
        "Наборы программы",
        list(labels),
        default=[set_id for set_id in selection_defaults(programme) if set_id in labels],
        format_func=lambda set_id: labels[set_id],
        key=f"{KEY}_selection_{session.session_id}",
        help=(
            "Программа — объединение наборов по логическому ИЛИ: проверка из пересечения "
            "учитывается один раз, происхождение видно в составе (FR-P-68)"
        ),
    )
    template = st.selectbox(
        "Шаблон программы",
        list(TEMPLATE_HINTS),
        key=f"{KEY}_template",
    )
    st.caption(TEMPLATE_HINTS.get(template, ""))
    build, from_template = st.columns(2)
    if build.button(
        "🧩 Собрать программу (ИЛИ)",
        key=f"{KEY}_build",
        type="primary",
        width="stretch",
    ):
        _rebuild(session, library, chosen)
    if from_template.button(
        "✨ Собрать по шаблону",
        key=f"{KEY}_template_build",
        width="stretch",
    ):
        _build_from_template(session, library, template)
    st.caption(union_caption(programme))


def _rebuild(session: TestSession, library: sets_api.SetsLibrary, set_ids: Sequence[str]) -> None:
    """Собирает состав программы объединением наборов и сохраняет его в сессию."""
    if not set_ids:
        st.error("Не выбран ни один набор: программа осталась бы пустой.")
        return
    programme = state.current_programme()
    if programme.is_approved:
        st.error(
            f"Программа утверждена (ревизия {programme.revision}): изменение состава — "
            "новой ревизией (`FR-P-68`). Нажмите «➕ Новая ревизия»."
        )
        return
    try:
        report = programme.rebuild(library, list(set_ids))
    except ValueError as exc:
        st.error(str(exc))
        return
    state.store_programme(
        programme,
        event="programme_rebuilt",
        message=f"Программа собрана: {programme.size} пунктов из наборов {', '.join(set_ids)}",
    )
    flash.success(
        layout.join_parts(
            f"Программа собрана: {layout.plural(programme.size, ('пункт', 'пункта', 'пунктов'))}",
            f"добавлено {len(report['added'])}",
            f"убрано {len(report['removed'])}",
            f"пересечений {report['intersections']}",
        )
    )
    st.rerun()


def _build_from_template(
    session: TestSession,
    library: sets_api.SetsLibrary,
    name: str,
) -> None:
    """Собирает программу по шаблону «полная / смоук / регресс» (`FR-P-70`)."""
    programme = state.current_programme()
    if programme.size and programme.is_approved:
        st.error(
            f"Программа утверждена (ревизия {programme.revision}): шаблон её не переписывает. "
            "Сначала выпустите новую ревизию."
        )
        return
    failed = regress_source(session) if name == programme_api.TEMPLATE_REGRESS else []
    built = programme_api.template_programme(
        name,
        library,
        failed_ids=failed,
        author=session.info.operator_fio,
    )
    state.store_programme(
        built,
        event="programme_template",
        message=f"Программа по шаблону «{name}»: {built.size} пунктов",
    )
    flash.success(
        layout.join_parts(
            f"Программа «{name}» собрана: "
            f"{layout.plural(built.size, ('пункт', 'пункта', 'пунктов'))}",
            f"отказов сессии в источнике: {len(failed)}" if failed else "",
        )
    )
    st.rerun()


def _render_coverage(programme: Programme) -> None:
    """Покрытие разделов против целевого объёма и непокрытые разделы (`IR-P-19`)."""
    st.subheader("Покрытие разделов")
    if not programme.size:
        st.caption("Программа пуста: покрытие появится после сборки.")
        return
    layout.rows_table(
        coverage_rows(programme),
        key=f"{KEY}_coverage",
        columns={
            "section": "Раздел",
            "in_programme": "В программе / цель",
            "mark": "",
            "implemented": "Описанных",
            "planned": "По программе",
            "missing": "Недобор",
            "sets": "Наборы",
        },
        height=260,
    )
    uncovered = programme.uncovered_sections()
    if uncovered:
        st.warning(
            "Не покрыты разделы: "
            + ", ".join(str(row["title"]) for row in uncovered)
            + " — при утверждении потребуется обоснование сокращения (`FR-P-69`)."
        )


def _render_items(session: TestSession, programme: Programme) -> None:
    """Состав программы: порядок, происхождение и ручная правка (`FR-P-68`, `FR-P-70`)."""
    st.subheader("Состав программы")
    if not programme.size:
        st.caption("В программе нет ни одной проверки: выберите наборы и соберите программу.")
        return
    layout.rows_table(
        programme_table_rows(programme, state.results_state(programme.check_ids)),
        key=f"{KEY}_items",
        columns={
            "order": "№",
            "mark": "",
            "check_id": "Проверка",
            "title": "Название",
            "section": "Раздел",
            "check_class": "Класс",
            "sources": "Происхождение",
            "note": "",
            "status": "Результат сессии",
        },
        height=320,
    )
    _render_item_edit(session, programme)
    if st.button("→ К прогону (SCR-301)", key=f"{KEY}_goto_run", width="stretch"):
        state.go_to(RUN_SCREEN)


def _render_item_edit(session: TestSession, programme: Programme) -> None:
    """Ручная правка состава: порядок, обязательность и исключение пункта.

    Правка доступна только у черновика: утверждённую программу меняет новая ревизия
    (`FR-P-68`), а обязательные пункты не исключаются и остаются первыми (`FR-P-70`).
    """
    if programme.is_approved:
        st.caption("Программа утверждена: правка состава — только новой ревизией (`FR-P-68`).")
        return
    chosen = st.selectbox(
        "Выбранный пункт",
        programme.check_ids,
        format_func=lambda check_id: _item_label(programme, check_id),
        key=f"{KEY}_item_pick",
    )
    up, down, star, drop = st.columns(4)
    if up.button("⬆ Выше", key=f"{KEY}_up", width="stretch"):
        _apply(session, "up", chosen)
    if down.button("⬇ Ниже", key=f"{KEY}_down", width="stretch"):
        _apply(session, "down", chosen)
    if star.button("★ Обязательность", key=f"{KEY}_star", width="stretch"):
        _apply(session, "star", chosen)
    if drop.button("✖ Исключить", key=f"{KEY}_drop", width="stretch"):
        _apply(session, "exclude", chosen)


def _item_label(programme: Programme, check_id: str) -> str:
    """Подпись пункта в списке правки: «4 ★ TC-FILE-13 Round-trip»."""
    item = programme.find(check_id)
    if item is None:
        return check_id
    return layout.join_parts(
        str(item.order),
        "★" if item.mandatory else "",
        item.check_id,
        item.title,
    )


def _apply(session: TestSession, action: str, check_id: str) -> None:
    """Выполняет правку состава программы и объясняет отказ вместо молчания."""
    programme = state.current_programme()
    try:
        if action == "up":
            changed = programme.move(check_id, -1)
        elif action == "down":
            changed = programme.move(check_id, 1)
        elif action == "star":
            item = programme.find(check_id)
            changed = programme.set_mandatory(check_id, not bool(item and item.mandatory))
        elif action == "exclude":
            programme.exclude([check_id])
            changed = True
        else:
            st.error(f"Неизвестная правка программы: {action}")
            return
    except ValueError as exc:
        st.error(str(exc))
        return
    if not changed:
        st.info(
            "Пункт не перемещён: обязательные пункты идут первыми и не исключаются "
            "из программы (`FR-P-70`)."
        )
        return
    state.store_programme(
        programme,
        event="programme_edited",
        message=f"Правка программы: {EDIT_TEXTS.get(action, action)} — {check_id}",
    )
    flash.success(f"Программа изменена: {EDIT_TEXTS.get(action, action)} — {check_id}")
    st.rerun()


# ---------------------------------------------------------------------------
# Панель контекста: утверждение, ревизии, предупреждения и очередь прогона
# ---------------------------------------------------------------------------
def _render_actions(
    session: TestSession,
    library: sets_api.SetsLibrary,
    programme: Programme,
) -> None:
    """Утверждение ревизии, обоснование сокращения, история ревизий и очередь прогона."""
    st.subheader("Утверждение и ревизии")
    author = st.text_input(
        "Кто утверждает",
        value=session.info.operator_fio,
        key=f"{KEY}_author",
    )
    comment = st.text_input("Комментарий к ревизии", key=f"{KEY}_comment")
    reduction = st.text_area(
        "Обоснование сокращения (FR-P-69)",
        value=programme.reduction,
        key=f"{KEY}_reduction",
        height=80,
        disabled=programme.is_approved,
    )
    st.caption(
        layout.join_parts(
            f"пунктов: {programme.size}",
            f"обязательных: {len(programme.mandatory_ids)}",
            f"наборов: {len(programme.set_refs)}",
            programme.status_label,
        )
    )
    if programme.is_approved:
        st.success(
            "Утверждена: "
            + layout.join_parts(
                str(programme.approved.get("author") or "автор не указан"),
                layout.short_time(programme.approved.get("at")),
                str(programme.reduction or ""),
            )
        )
    if st.button(
        "✅ Утвердить программу",
        key=f"{KEY}_approve",
        type="primary",
        width="stretch",
        disabled=programme.is_approved,
    ):
        _approve(session, author=author, comment=comment, reduction=reduction)
    if st.button("➕ Новая ревизия", key=f"{KEY}_revise", width="stretch"):
        _revise(session, author=author, comment=comment)
    _render_warnings(library, programme)
    st.caption(delta_caption(programme))
    _render_revisions(session, programme, author)
    _render_queue(session, programme)


def _approve(
    session: TestSession,
    *,
    author: str,
    comment: str,
    reduction: str,
) -> None:
    """Утверждает ревизию программы; правила проверяет ядро (`Programme.approve`)."""
    if not str(author).strip():
        st.error("Укажите, кто утверждает: утверждение фиксируется в сессии (`FR-P-67`).")
        return
    programme = state.current_programme()
    try:
        programme.approve(by=author, comment=comment, reduction=reduction)
    except ValueError as exc:
        st.error(str(exc))
        return
    state.store_programme(
        programme,
        event="programme_approved",
        message=f"Программа утверждена: ревизия {programme.revision}, {programme.size} пунктов",
    )
    flash.success(
        layout.join_parts(
            f"Программа утверждена: ревизия {programme.revision}",
            layout.plural(programme.size, ("пункт", "пункта", "пунктов")),
            "с сокращением — обоснование записано" if programme.reduction else "",
        )
    )
    st.rerun()


def _revise(session: TestSession, *, author: str, comment: str) -> None:
    """Выпускает новую ревизию программы: текущая уходит в архив (`DR-P-14`)."""
    programme = state.current_programme()
    if not programme.size:
        st.error("Программу не собрали: выпускать новую ревизию не из чего.")
        return
    programme.revise(author=author, comment=comment)
    state.store_programme(
        programme,
        event="programme_revised",
        message=f"Выпущена ревизия {programme.revision} (черновик)",
    )
    flash.success(
        f"Выпущена ревизия {programme.revision}: состав снова правится, результаты прежней "
        "ревизии сохранены (`DR-P-14`)"
    )
    st.rerun()


def _render_warnings(library: sets_api.SetsLibrary, programme: Programme) -> None:
    """Предупреждения программы перед утверждением (`FR-P-68`, `FR-P-69`)."""
    notes = programme.warnings(library)
    if not notes:
        st.caption("Предупреждений нет: программа собрана без замечаний.")
        return
    for note in notes:
        st.warning(note)


def _render_revisions(session: TestSession, programme: Programme, author: str) -> None:
    """История ревизий: выгрузка программы и восстановление архивного состава."""
    rows = revision_table_rows(programme)
    with st.expander(f"Ревизии ({len(rows)})", expanded=False):
        layout.rows_table(
            rows,
            key=f"{KEY}_revisions",
            columns={
                "revision": "№",
                "at": "Когда",
                "state": "Состояние",
                "current": "",
                "author": "Автор",
                "size": "Пунктов",
                "comment": "Комментарий",
            },
            height=200,
        )
        st.download_button(
            "⬇ Выгрузить программу (json)",
            data=json.dumps(programme.to_dict(), ensure_ascii=False, indent=2),
            file_name=f"programme_{session.session_id}.json",
            mime="application/json",
            key=f"{KEY}_download",
        )
        archive = [int(row["revision"]) for row in rows if not row["current"]]
        if not archive:
            return
        chosen = st.selectbox("Архивная ревизия", archive, key=f"{KEY}_restore_pick")
        if st.button(f"↩ Восстановить состав ревизии {chosen}", key=f"{KEY}_restore"):
            _restore(session, int(chosen), author=author)


def _restore(session: TestSession, number: int, *, author: str) -> None:
    """Восстанавливает состав архивной ревизии как новую ревизию (`DR-P-14`)."""
    programme = state.current_programme()
    try:
        programme.restore_revision(number, author=author)
    except ValueError as exc:
        st.error(str(exc))
        return
    state.store_programme(
        programme,
        event="programme_restored",
        message=f"Состав ревизии {number} восстановлен как ревизия {programme.revision}",
    )
    flash.success(f"Состав ревизии {number} восстановлен в ревизии {programme.revision} (черновик)")
    st.rerun()


def _render_queue(session: TestSession, programme: Programme) -> None:
    """Очередь прогона: снимок ревизии программы (`FR-P-13`); запуск — на `SCR-301`."""
    st.divider()
    current = state.current_queue()
    if current.size:
        st.caption(
            layout.join_parts(
                f"очередь прогона: {layout.plural(current.size, ('пункт', 'пункта', 'пунктов'))}",
                f"ревизия {current.revision}",
                "по черновику" if current.draft else "по утверждённой",
            )
        )
    if st.button("🧾 Собрать очередь прогона", key=f"{KEY}_queue", width="stretch"):
        if not programme.size:
            st.error("Программа пуста: очередь собирать не из чего.")
            return
        built = queue_api.build_queue(programme, author=session.info.operator_fio)
        state.store_queue(
            built,
            event="queue_built",
            message=f"Очередь собрана по ревизии {built.revision}: {built.size} пунктов",
        )
        flash.success(
            layout.join_parts(
                f"Очередь собрана: {layout.plural(built.size, ('пункт', 'пункта', 'пунктов'))}",
                f"ревизия {built.revision}",
                "прогон будет помечен как прогон по черновику"
                if built.draft
                else "прогон по утверждённой программе",
            )
        )
        st.rerun()
    if st.button("→ Открыть прогон (SCR-301)", key=f"{KEY}_run_link", width="stretch"):
        state.go_to(RUN_SCREEN)
