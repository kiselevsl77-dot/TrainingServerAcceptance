"""Экран «Стенд» (FR-T1): доступность, сборка испытуемого сервера, снимок реестров.

Экран даёт «паспорт стенда», который попадает в отчёт об испытаниях:
`GET /health`, `GET /version` (все фактические поля сборки), адрес и таймауты,
уровень журналирования, пути журналов, а также снимки счётчиков реестров
на начало и конец сессии (`take_stand_snapshot`).

Специально: проверка сервера выполняется только по кнопке или при первом входе,
чтобы каждый запрос был осознанным действием и был виден в журнале.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from acceptance.api import take_stand_snapshot
from acceptance.logging_setup import log_event
from acceptance.session import SNAP_END, SNAP_START, TestSession, now_iso, set_snapshot
from acceptance.ui import state
from acceptance.ui.common.labels import build_label
from client.errors import ClientError

KEY_LAST_SNAPSHOT = "stand_last_snapshot"

VERSION_FIELDS = (
    "branch",
    "revision",
    "commit",
    "message",
    "author",
    "email",
    "build_date",
)

PHASES: dict[str, str] = {
    "Начало сессии": SNAP_START,
    "Окончание сессии": SNAP_END,
}


def render() -> None:
    """Отрисовывает экран «Стенд»."""
    st.title("Стенд")
    st.caption(
        "FR-T1 · доступность и сборка испытуемого сервера, снимок счётчиков реестров. "
        "Все обращения к серверу фиксируются в журнале (FR-T6)."
    )

    runtime = state.get_runtime()
    if runtime is None:
        st.error(
            "Base URL испытуемого сервера не задан. Укажите `TRAINING_SERVER_BASE_URL` "
            "в файле `.env` и перезапустите пульт."
        )
        return

    session = state.current_session()
    _render_system(runtime, session)
    st.divider()
    _render_journal(runtime)
    st.divider()
    _render_snapshot(runtime, session)


# ---------------------------------------------------------------------------
# Доступность и сборка сервера
# ---------------------------------------------------------------------------
def _check_status(runtime: state.Runtime) -> dict[str, Any]:
    """Выполняет `/health` + `/version` и кэширует результат в состоянии экрана."""
    payload: dict[str, Any] = {"at": now_iso(), "ok": False, "version": None, "error": None}
    try:
        runtime.apis.system.health()
        payload["ok"] = True
        payload["version"] = runtime.apis.system.version()
    except ClientError as exc:
        payload["error"] = str(exc)

    state.set_stand_status(payload)
    log_event(
        "stand_check",
        "Проверка доступности сервера",
        level="INFO" if payload["ok"] else "ERROR",
        module="stand",
        payload={"ok": payload["ok"], "error": payload["error"], "version": payload["version"]},
    )
    return payload


def _render_system(runtime: state.Runtime, session: TestSession | None) -> None:
    """Блок «Сервер»: индикатор, версия сборки, параметры подключения."""
    status = state.stand_status()
    if status is None:
        status = _check_status(runtime)

    col_action, col_state = st.columns([1, 3])
    if col_action.button("🔄 Проверить сервер", use_container_width=True):
        status = _check_status(runtime)

    if status.get("ok"):
        col_state.success(f"Сервер доступен · проверено {status.get('at', '—')}")
    else:
        col_state.error(
            f"Сервер недоступен: {status.get('error') or 'нет ответа'} · "
            f"проверено {status.get('at', '—')}"
        )

    settings = runtime.settings
    config = runtime.config
    logs = state.ensure_logging(state.log_level(), session.session_id if session else None)

    st.table(
        {
            "Base URL": settings.base_url,
            "Таймаут запросов, с": f"{settings.timeout}",
            "Таймаут скачивания, с": f"{settings.download_timeout}",
            "Порог скачивания, МБ": f"{settings.download_limit_mb}",
            "Уровень журнала": logs.level,
            "Текстовый журнал": logs.app_log.as_posix(),
            "Структурный журнал": logs.session_log.as_posix(),
            "Тела в журнале": "сохраняются" if config.log_bodies else "не сохраняются",
            "Лимит тела, символов": f"{config.body_limit}",
        }
    )

    version = status.get("version")
    if isinstance(version, dict) and version:
        st.markdown("**Сборка сервера (`GET /version`)**")
        known = {field: version.get(field) for field in VERSION_FIELDS if field in version}
        st.table(known or version)
        extra = {key: value for key, value in version.items() if key not in known}
        if extra:
            with st.expander("Прочие поля ответа `/version`"):
                st.json(extra)


# ---------------------------------------------------------------------------
# Журнал (краткая сводка)
# ---------------------------------------------------------------------------
def _render_journal(runtime: state.Runtime) -> None:
    """Сводка журнала обмена с сервером (полный журнал — на отдельном экране)."""
    summary = runtime.journal.summary()
    col_total, col_errors, col_slow, col_bytes = st.columns(4)
    col_total.metric("Запросов", summary["total"])
    col_errors.metric("С ошибкой", summary["errors"])
    col_slow.metric("Максимум, мс", f"{summary['slowest_ms']:.0f}")
    col_bytes.metric("Ответов, байт", summary["response_bytes"])

    if summary["total"]:
        st.caption(
            f"По методам: {summary['by_method']} · по статусам: {summary['by_status']} · "
            f"первый запрос {summary['first_at']} · последний {summary['last_at']}"
        )
    else:
        st.info("Журнал пуст: с момента запуска пульта запросов к серверу не было.")


# ---------------------------------------------------------------------------
# Снимок стенда
# ---------------------------------------------------------------------------
def _render_snapshot(runtime: state.Runtime, session: TestSession | None) -> None:
    """Снимок счётчиков реестров: сохранение в сессию и сравнение начала/окончания."""
    st.subheader("Снимок стенда")
    st.caption(
        "Счётчики реестров и сборка сервера на начало и окончание сессии входят в отчёт "
        "об испытаниях (раздел «Снимок стенда»)."
    )

    col_phase, col_take = st.columns([2, 1])
    phase_label = col_phase.radio(
        "Фаза снимка",
        list(PHASES),
        horizontal=True,
        key="stand_snapshot_phase",
    )
    phase = PHASES[phase_label]

    if col_take.button("📸 Снять снимок", use_container_width=True):
        snapshot = take_stand_snapshot(runtime.apis)
        st.session_state[KEY_LAST_SNAPSHOT] = snapshot
        if session is None:
            st.warning(
                "Сессия не выбрана: снимок сформирован, но в сессию не сохранён. "
                "Создайте сессию на экране «Сессия испытаний»."
            )
        else:
            set_snapshot(session, phase, snapshot)
            state.store_session(session)
            st.success(f"Снимок сохранён в сессию {session.session_id} ({phase_label}).")

    shown: dict[str, Any] | None = st.session_state.get(KEY_LAST_SNAPSHOT)
    if session is not None and phase in session.snapshots:
        stored = session.snapshots[phase]
        if shown is None or stored.get("at") != shown.get("at"):
            shown = stored

    if shown:
        _render_snapshot_data(shown)
    else:
        st.info("Снимок не снят. Нажмите «Снять снимок» — снимок хранится в сессии и журнале.")

    if session is not None:
        _render_snapshot_diff(session)


def _render_snapshot_data(snapshot: dict[str, Any]) -> None:
    """Показывает данные одного снимка: счётчики, файлы, ошибки, сборка."""
    counts = snapshot.get("counts") or {}
    files = snapshot.get("files") or {}

    st.caption(
        f"Снимок от {snapshot.get('at', '—')} · сборка: {build_label(snapshot.get('version'))}"
    )
    col_files, col_loads, col_models, col_datasets, col_tasks = st.columns(5)
    col_files.metric("Файлов", counts.get("files", "—"))
    col_loads.metric("Нагрузок", counts.get("loads", "—"))
    col_models.metric("Моделей", counts.get("models", "—"))
    col_datasets.metric("Датасетов", counts.get("datasets", "—"))
    col_tasks.metric("Задач", counts.get("tasks", "—"))

    if files:
        st.caption(
            f"Объём файлов: {files.get('total_bytes', 0)} байт · уникальных имён: "
            f"{files.get('unique_names', '—')} · имён с дублями: {files.get('duplicate_names', '—')} · "
            f"по типам: {files.get('by_type', {})}"
        )

    errors = snapshot.get("errors") or {}
    if errors:
        st.warning(
            "Часть снимка не получена: "
            + "; ".join(f"{key}: {value}" for key, value in errors.items())
        )


def _render_snapshot_diff(session: TestSession) -> None:
    """Сравнение снимков начала и окончания сессии."""
    start = session.snapshots.get(SNAP_START)
    end = session.snapshots.get(SNAP_END)
    if not start or not end:
        return

    start_counts = start.get("counts") or {}
    end_counts = end.get("counts") or {}
    rows: list[dict[str, Any]] = []
    for key in sorted(set(start_counts) | set(end_counts)):
        before = start_counts.get(key)
        after = end_counts.get(key)
        delta = (after - before) if isinstance(before, int) and isinstance(after, int) else "—"
        rows.append({"Реестр": key, "Начало": before, "Окончание": after, "Изменение": delta})

    st.markdown("**Изменение реестров за сессию**")
    st.table(rows)
