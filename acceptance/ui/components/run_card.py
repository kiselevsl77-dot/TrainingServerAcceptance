"""Карточка запуска: подтверждение изменяющих и ресурсоёмких проверок (`FR-P-19`).

Правило испытаний (`FR-P-19`, `NFR-P-5`, `AC-P-13`): проверка класса `live`
(реальные данные) или `heavy` (ресурсоёмкая) **не запускается**, пока оператор не
заполнил карточку запуска — что проверяем, какими данными, кто отвечает, что
делать с артефактами — и не подтвердил расход ресурсов. Проверки класса `tech`
идут без карточки (`runner.needs_confirmation` возвращает `False`).

Заполненная карточка попадает в доказательства результата
(`RunOutcome.check_result.evidence["run_card"]`, пишет `runner.execute_check`),
поэтому из протокола и отчёта видно, на каких данных получен результат.

Модуль чистый в части проверки полей (`defaults`, `validate`, `evidence`) — эти
функции проверяются unit-тестами; `render` только рисует форму.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import streamlit as st

from acceptance.labels import head_text
from acceptance.programme import ProgrammeItem
from acceptance.runner import needs_confirmation
from acceptance.session import now_iso

#: Обязательные поля карточки запуска (без них запуск не подтверждается).
REQUIRED: tuple[str, ...] = ("goal", "data", "responsible")

#: Подписи полей карточки (для формы и для доказательств результата).
FIELD_LABELS: dict[str, str] = {
    "goal": "Цель проверки",
    "data": "Данные, на которых выполняется",
    "responsible": "Ответственный",
    "artifacts": "Что делать с артефактами",
    "confirm": "Подтверждаю расход ресурсов",
}

#: Варианты действий с артефактами после проверки (макет `docs/16`, `SCR-301`).
ARTIFACT_ACTIONS: tuple[str, ...] = (
    "удалить после проверки",
    "оставить для разбора",
    "выгрузить в доказательства",
)


def defaults(item: ProgrammeItem, *, session_id: str = "", author: str = "") -> dict[str, Any]:
    """Заготовка карточки: цель и данные подставляются из пункта программы.

    Подстановка нужна, чтобы оператор правил, а не набирал: пустая форма на
    изменяющей проверке — лишний повод ошибиться в данных.
    """
    return {
        "goal": f"{item.check_id}: {item.title}".strip(": "),
        "data": f"__TEST__ (пульт), сессия {session_id}" if session_id else "__TEST__ (пульт)",
        "responsible": str(author),
        "artifacts": ARTIFACT_ACTIONS[0],
        "confirm": False,
    }


def validate(values: Mapping[str, Any]) -> list[str]:
    """Список незаполненных обязательных полей карточки (пустой — можно запускать)."""
    missing: list[str] = []
    for field in REQUIRED:
        if not str(values.get(field) or "").strip():
            missing.append(FIELD_LABELS[field])
    if not bool(values.get("confirm")):
        missing.append(FIELD_LABELS["confirm"])
    return missing


def evidence(values: Mapping[str, Any], *, author: str = "", at: str = "") -> dict[str, Any]:
    """Карточка запуска как доказательство результата (идёт в `evidence["run_card"]`)."""
    payload: dict[str, Any] = {
        "at": at or now_iso(),
        "author": str(author),
        "goal": str(values.get("goal") or "").strip(),
        "data": str(values.get("data") or "").strip(),
        "responsible": str(values.get("responsible") or "").strip(),
        "artifacts": str(values.get("artifacts") or "").strip(),
        "confirm": bool(values.get("confirm")),
        "missing": validate(values),
    }
    payload["filled"] = not payload["missing"]
    return payload


def render(
    item: ProgrammeItem,
    *,
    key: str,
    session_id: str = "",
    author: str = "",
    reason: str = "",
) -> dict[str, Any] | None:
    """Рисует карточку запуска; возвращает подтверждение или None (не подтверждено).

    Args:
        item: пункт программы — проверка, для которой запрашивается подтверждение.
        key: префикс ключей виджетов (карточка может быть не одна на экране).
        reason: пояснение, почему прогон остановлен (`RunOutcome.stop_reason`).
    """
    need, note = needs_confirmation(item)
    st.markdown(f"**Карточка запуска: {item.check_id}** — {head_text(item.title, 60)}")
    if reason:
        st.warning(reason)
    if need and note:
        st.caption(note)

    values = defaults(item, session_id=session_id, author=author)
    with st.form(key=f"{key}_form", clear_on_submit=False):
        values = {
            "goal": st.text_input(FIELD_LABELS["goal"] + " *", value=str(values["goal"])),
            "data": st.text_input(FIELD_LABELS["data"] + " *", value=str(values["data"])),
            "responsible": st.text_input(
                FIELD_LABELS["responsible"] + " *",
                value=str(values["responsible"] or author),
            ),
            "artifacts": st.selectbox(FIELD_LABELS["artifacts"], ARTIFACT_ACTIONS),
            "confirm": st.checkbox(FIELD_LABELS["confirm"] + " *", value=False),
        }
        submitted = st.form_submit_button("Подтвердить запуск", type="primary")

    if not submitted:
        return None
    missing = validate(values)
    if missing:
        st.error("Карточка запуска заполнена не полностью: " + ", ".join(missing))
        return None
    return evidence(values, author=author)
