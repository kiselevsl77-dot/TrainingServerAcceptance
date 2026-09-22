"""Снимки стенда: фиксация состояния реестров «до» и «после» сессии (`DR-P-8`).

Один компонент на два экрана макета (`docs/16`):

* `SCR-202` «Стенд» — снимок, счётчики реестров и разница «до/после»;
* `SCR-203` «Сессия испытаний» — снимки в панели состояния сессии.

Снимок делает `acceptance/api.take_stand_snapshot`: он собирает версию сервера и
счётчики реестров, а ошибки отдельных запросов складывает в раздел `errors` — снимок
не прерывается и остаётся честным: в нём видно, что именно не удалось получить
(состояние «частичный снимок» из макета).

Чистые функции (`labels`, `current`, `counters_rows`, `delta_rows`, `delta_csv`)
проверяются unit-тестами: это вся арифметика сравнения снимков.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import streamlit as st

from acceptance import glossary
from acceptance import session as session_api
from acceptance.api import take_stand_snapshot
from acceptance.labels import build_label
from acceptance.session import TestSession
from acceptance.ui import state
from acceptance.ui.components import flash, layout

#: Подписи счётчиков реестров (ключи — из `api.take_stand_snapshot`).
COUNTER_LABELS: dict[str, str] = {
    "files": glossary.term("file"),
    "records": "Записи (RAW + markup)",
    "loads": glossary.term("load"),
    "datasets": glossary.term("dataset"),
    "models": glossary.term("model"),
    "tasks": glossary.term("task"),
}

#: Фаза снимка → подпись (макет: «Начало» / «Окончание»).
PHASE_LABELS: dict[str, str] = {
    session_api.SNAP_START: "Начало",
    session_api.SNAP_END: "Окончание",
}


def labels(session: TestSession) -> list[tuple[str, str]]:
    """Снимки сессии: подпись фазы → время снимка (пустая строка — снимка нет)."""
    return [
        (title, str((session.snapshots.get(phase) or {}).get("at") or ""))
        for phase, title in PHASE_LABELS.items()
    ]


def current(session: TestSession | None, stand: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Снимок для показа: снимок «Начало» сессии, иначе результат проверки связи."""
    if session is not None:
        snapshot = session.snapshots.get(session_api.SNAP_START)
        if snapshot:
            return dict(snapshot)
    return dict(stand or {})


def counters_rows(snapshot: Mapping[str, Any]) -> list[dict[str, str]]:
    """Счётчики реестров снимка строками: реестр → значение («нет данных» вместо нуля)."""
    counts = dict(snapshot.get("counts") or {})
    rows = [
        {"counter": label, "value": "нет данных" if key not in counts else str(counts[key])}
        for key, label in COUNTER_LABELS.items()
    ]
    files = dict(snapshot.get("files") or {})
    if files:
        rows.append(
            {
                "counter": f"Объём {glossary.term('file')}",
                "value": (
                    f"{float(files.get('total_bytes') or 0) / 1e9:.2f} ГБ"
                    f" · дублей имён: {files.get('duplicate_names', 0)}"
                ),
            }
        )
    return rows


def delta_rows(
    start: Mapping[str, Any],
    end: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Разница снимков «до/после» по реестрам: сколько добавилось за сессию."""
    before = dict(start.get("counts") or {})
    after = dict(end.get("counts") or {})
    rows: list[dict[str, str]] = []
    for key, label in COUNTER_LABELS.items():
        if key not in before and key not in after:
            continue
        left = before.get(key)
        right = after.get(key)
        if left is None or right is None:
            difference = "нет данных"
        else:
            delta = int(right) - int(left)
            difference = f"+{delta}" if delta > 0 else str(delta)
        rows.append(
            {
                "counter": label,
                "before": "—" if left is None else str(left),
                "after": "—" if right is None else str(right),
                "delta": difference,
            }
        )
    return rows


def delta_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """CSV сравнения снимков для приложения к отчёту (`FR-P-47`).

    Разделитель — «;», как в выгрузках пульта для Excel с русской локалью.
    """
    header = ";".join(("Реестр", "Начало", "Окончание", "Разница"))
    body = [
        ";".join(
            (
                str(row.get("counter") or ""),
                str(row.get("before") or ""),
                str(row.get("after") or ""),
                str(row.get("delta") or ""),
            )
        )
        for row in rows
    ]
    return "\n".join((header, *body)) + "\n"


def caption(snapshot: Mapping[str, Any] | None, *, prefix: str = "снимок") -> str:
    """Подпись снимка: «снимок 21.09 10:12 · dev@83319ae»."""
    data = dict(snapshot or {})
    if not data:
        return ""
    return layout.join_parts(
        f"{prefix} {layout.short_time(data.get('at'))}",
        build_label(data.get("version")) if data.get("version") else "",
    )


def error_text(snapshot: Mapping[str, Any]) -> str:
    """Ошибки запросов внутри снимка одной строкой (пустая строка — ошибок нет)."""
    errors = dict(snapshot.get("errors") or {})
    if not errors:
        return ""
    return "Снимок частичный, ошибки запросов: " + "; ".join(
        f"{key}: {value}" for key, value in errors.items()
    )


def controls(session: TestSession, *, key: str) -> None:
    """Блок снимков экрана: что уже снято и кнопка «Снять снимок» (`DR-P-8`)."""
    for title, at in labels(session):
        st.caption(f"{title}: {layout.short_time(at) or 'не снят'}")
    phases = list(PHASE_LABELS)
    default = (
        session_api.SNAP_END
        if session_api.SNAP_START in session.snapshots
        else session_api.SNAP_START
    )
    phase = st.radio(
        "Что фиксируем",
        phases,
        index=phases.index(default),
        format_func=lambda value: PHASE_LABELS[value],
        horizontal=True,
        key=f"{key}_phase",
    )
    if st.button("📸 Снять снимок", key=f"{key}_take", width="stretch"):
        capture(session, phase)


def capture(session: TestSession, phase: str) -> None:
    """Снимает снимок стенда, сохраняет его в сессию и перерисовывает экран.

    Args:
        phase: `session.SNAP_START` («Начало») или `session.SNAP_END` («Окончание»).
    """
    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес стенда не задан: снимок снять нельзя (настройки — `SCR-501`).")
        return
    snapshot = take_stand_snapshot(runtime.apis)
    session_api.set_snapshot(session, phase, snapshot)
    state.store_session(session)
    state.set_stand_status(snapshot)
    problem = error_text(snapshot)
    if problem:
        flash.warning(problem)
    else:
        counts = ", ".join(
            f"{key}: {value}" for key, value in (snapshot.get("counts") or {}).items()
        )
        flash.success(
            f"Снимок «{PHASE_LABELS.get(phase, phase)}» снят ({counts or 'счётчики пусты'})"
        )
    st.rerun()
