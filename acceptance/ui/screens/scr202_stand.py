"""`SCR-202` Стенд (сервер, сборка, снимки) — Подготовка (Ф0, Ф4).

Макет экрана — `docs/16`, раздел «SCR-202. Стенд»; требования ТЗ: `FR-P-1`, `FR-P-6`,
`FR-P-47`, `DR-P-8`.

Экран отвечает на два вопроса: испытываем ли мы **ту сборку**, что записана в реквизитах,
и что стало с реестрами стенда за сессию. Это **не** панель настроек подключения
(настройки — `SCR-501`) и не монитор обменов: здесь только состояние стенда и снимки.

Проверка связи делается осознанным действием («🔄 Проверить»), а не на каждой перерисовке:
иначе экран превращается в генератор запросов к испытываемому серверу.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

from acceptance import session as session_api
from acceptance.labels import build_label
from acceptance.paths import ARTIFACT_DIR, ensure_dirs
from acceptance.session import TestSession, now_iso
from acceptance.ui import state
from acceptance.ui.components import flash, layout, snapshots, status
from acceptance.ui.components.screen import ScreenContent
from client.errors import ClientError

#: Ключ маршрута экрана (`acceptance/ui/nav.py`).
KEY = "scr202_stand"

#: Ширина колонки кнопки проверки в блоке «Сервер и сборка».
CHECK_COLUMN_RATIO = (1, 3)

#: Содержимое экрана по макету: объявленный контракт (трассировка — этап 4).
CONTENT = ScreenContent(
    purpose="Подтвердить сборку стенда и зафиксировать состояние реестров «до» и «после».",
    blocks=(
        "Сервер и сборка: health, версия, сообщение стенда",
        "Журналы запуска и сессии: пути и скачивание",
        "Реестры: $Файлы, записи, $Нагрузки, $Датасеты, $Модели, $Задачи",
        "Снимок «Начало»/«Окончание» и разница снимков за сессию",
        "Расхождение сборки стенда с реквизитами сессии",
    ),
    states=(
        "Ошибка связи: «Стенд недоступен: <причина>. Проверьте адрес в SCR-501»",
        "Частичный снимок: блок помечен «ошибка запроса», снимок не прерывается",
    ),
    transitions=(
        ("SCR-204", "Данные стенда"),
        ("SCR-501", "Консоль и настройки"),
    ),
    requirements=(
        "FR-P-1",
        "FR-P-6",
        "FR-P-47",
        "DR-P-8",
    ),
)


# ---------------------------------------------------------------------------
# Чистые помощники экрана (проверяются unit-тестами)
# ---------------------------------------------------------------------------
def build_note(session: TestSession | None, actual_build: str) -> str:
    """Итог сравнения сборок: что испытываем сейчас и что записано в сессии.

    Возвращает предупреждение (см. `status.build_warning`) либо пустую строку.
    """
    if session is None:
        return ""
    return status.build_warning(session, actual_build)


def logs_rows(session: TestSession | None) -> list[dict[str, str]]:
    """Журналы запуска и сессии строками: назначение → путь (`FR-6`)."""
    if session is None:
        return []
    return [
        {"journal": "журнал сессии (JSONL)", "path": session.logs.get("session_log", "")},
        {"journal": "журнал запуска пульта", "path": session.logs.get("app_log", "")},
        {"journal": "файл сессии", "path": session_api.session_path(session.session_id).as_posix()},
    ]


def check_caption(status_payload: Mapping[str, Any] | None) -> str:
    """Подпись проверки связи: «✅ 21.09 10:12 · dev@83319ae»."""
    data = dict(status_payload or {})
    if not data:
        return "проверка ещё не выполнялась"
    icon = "✅" if data.get("health") else "⚠"
    return layout.join_parts(
        icon,
        layout.short_time(data.get("at")),
        build_label(data.get("version")) if data.get("version") else "",
    )


def render() -> None:
    """Рисует экран: сервер и сборка, реестры стенда, снимки и разница «до/после»."""
    session = state.current_session()
    stand = state.stand_status() or {}
    layout.render_header(KEY, check_caption(stand))
    flash.render()

    workspace, context = layout.zones()
    with workspace:
        _render_server()
        _render_registries(session, stand)
        _render_delta(session)
    with context:
        _render_snapshots(session)
        _render_journals(session)
        _render_links()


def _render_server() -> None:
    """Адрес, проверка связи, сборка и подпись состояния (`FR-P-1`)."""
    runtime = state.get_runtime()
    st.subheader("Сервер и сборка")
    if runtime is None:
        st.error("Адрес стенда не задан (`TRAINING_SERVER_BASE_URL`): настройки — `SCR-501`.")
        return

    st.caption(
        layout.join_parts(
            f"Адрес: {runtime.settings.base_url}",
            f"таймаут: {runtime.settings.timeout} с",
            f"журнал в памяти: {runtime.config.journal_max} обменов",
        )
    )
    button, summary = st.columns(list(CHECK_COLUMN_RATIO))
    if button.button("🔄 Проверить", key=f"{KEY}_check", type="primary", width="stretch"):
        _check_stand()
    summary.caption(check_caption(state.stand_status()))

    session = state.current_session()
    warning = build_note(session, build_label((state.stand_status() or {}).get("version")))
    if warning:
        st.warning(warning)


def _check_stand() -> None:
    """Осознанная проверка связи: `/health` и `/version` (`FR-P-1`)."""
    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес стенда не задан (`TRAINING_SERVER_BASE_URL`).")
        return
    try:
        runtime.apis.system.health()
        version = runtime.apis.system.version()
    except (ClientError, httpx.HTTPError) as exc:
        state.clear_stand_status()
        flash.error(f"Стенд недоступен: {exc}. Проверьте адрес в `SCR-501` и повторите.")
        st.rerun()
    state.set_stand_status({"at": now_iso(), "health": True, "version": version})
    flash.success(f"Стенд доступен: {build_label(version)}")
    st.rerun()


def _render_registries(session: TestSession | None, stand: Mapping[str, Any]) -> None:
    """Реестры стенда: счётчики снимка «Начало» или проверки связи (`FR-P-6`)."""
    st.subheader("Реестры стенда")
    snapshot = snapshots.current(session, stand)
    if not snapshot:
        st.caption("Счётчиков нет: нажмите «🔄 Проверить» или снимите снимок «Начало».")
        return
    st.caption(
        layout.join_parts(
            snapshots.caption(snapshot),
            f"реестров в снимке: {len(snapshot.get('counts') or {})}",
        )
    )
    layout.rows_table(
        snapshots.counters_rows(snapshot),
        key=f"{KEY}_counters",
        columns={"counter": "Реестр", "value": "Значение"},
        height=250,
    )
    problem = snapshots.error_text(snapshot)
    if problem:
        st.warning(problem)


def _render_delta(session: TestSession | None) -> None:
    """Разница снимков «Начало»/«Окончание»: учёт ресурсов за сессию (`FR-P-47`)."""
    st.subheader("Разница «до/после»")
    if session is None:
        st.caption("Сессия не выбрана: сравнивать нечего.")
        return
    start = dict(session.snapshots.get(session_api.SNAP_START) or {})
    end = dict(session.snapshots.get(session_api.SNAP_END) or {})
    if not start or not end:
        st.caption(
            "Разница появится после снимка «Окончание» (`DR-P-8`): "
            "снимок «Начало» " + ("есть" if start else "не снят")
        )
        return
    rows = snapshots.delta_rows(start, end)
    layout.rows_table(
        rows,
        key=f"{KEY}_delta",
        columns={
            "counter": "Реестр",
            "before": "Начало",
            "after": "Окончание",
            "delta": "Разница",
        },
        height=250,
    )
    st.download_button(
        "⬇ Выгрузить сравнение (CSV)",
        data=snapshots.delta_csv(rows),
        file_name=f"stand_delta_{session.session_id}.csv",
        mime="text/csv",
        key=f"{KEY}_delta_csv",
    )
    if st.button("📎 Приложить сравнение к сессии", key=f"{KEY}_delta_artifact"):
        _attach_delta(session, rows)


def _attach_delta(session: TestSession, rows: Sequence[Mapping[str, Any]]) -> None:
    """Сохраняет сравнение снимков артефактом сессии (`FR-P-33`, `FR-P-47`)."""
    ensure_dirs()
    path = ARTIFACT_DIR / f"stand_delta_{session.session_id}.csv"
    path.write_text(snapshots.delta_csv(rows), encoding="utf-8")
    session_api.add_artifact(
        session,
        kind="stand_delta",
        path=path,
        note="сравнение снимков стенда «до/после»",
    )
    state.store_session(session)
    flash.success(f"Сравнение приложено к сессии: {path.name}")
    st.rerun()


def _render_snapshots(session: TestSession | None) -> None:
    """Снимки «Начало»/«Окончание»: состояние и кнопка снятия (`DR-P-8`)."""
    st.subheader("Снимки")
    if session is None:
        st.warning("Сессия испытаний не выбрана: снимок некуда записать.")
        if st.button("Открыть «Сессия испытаний» (SCR-203)", key=f"{KEY}_goto_session"):
            state.go_to("scr203_session")
        return
    snapshots.controls(session, key=KEY)


def _render_journals(session: TestSession | None) -> None:
    """Журналы запуска, сессии и файл сессии: где лежат (`FR-P-6`)."""
    st.divider()
    st.subheader("Журналы")
    rows = logs_rows(session)
    if not rows:
        st.caption("Сессия не выбрана: журналы появятся после её создания.")
        return
    for row in rows:
        st.caption(f"{row['journal']}: `{row['path'] or '—'}`")
    if session is not None:
        _download_log_button(session)


def _download_log_button(session: TestSession) -> None:
    """Кнопка выгрузки структурного журнала сессии (`logs/session_<id>.jsonl`)."""
    path = session_api.session_path(session.session_id)
    log_path = session.logs.get("session_log", "")
    if not log_path or not Path(log_path).exists():
        st.caption("Структурный журнал сессии ещё не создан.")
        return
    st.download_button(
        "⬇ Выгрузить журнал сессии (JSONL)",
        data=Path(log_path).read_text(encoding="utf-8"),
        file_name=Path(log_path).name,
        mime="application/x-ndjson",
        key=f"{KEY}_log_download",
    )
    st.caption(f"Файл сессии: `{path.as_posix()}`")


def _render_links() -> None:
    """Переходы экрана: данные стенда и настройки подключения (макет `SCR-202`)."""
    st.divider()
    if st.button("→ Данные стенда (SCR-204)", key=f"{KEY}_goto_data", width="stretch"):
        state.go_to("scr204_data")
    if st.button("→ Консоль и настройки (SCR-501)", key=f"{KEY}_goto_tools", width="stretch"):
        state.go_to("scr501_tools")
