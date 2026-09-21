"""Экран «Датасеты» (FR-4, UC-09…UC-14; этап T5).

Назначение: работать с модулем «Datasets» так, как он описан в контракте 17.09.2026,
и честно показывать его ограничения:

    * **реестр** — фильтры `name`/`description`/`type`/`creation_date`, объёмы и выгрузка
      перечня в артефакты отчёта;
    * **создание** — только `__TEST__`-датасеты (`type = direct_fill`), с регистрацией
      в сессии для самоочистки (NFR-T4);
    * **состав** — провайдер `acceptance.dataset_composition`: серверный состав (когда
      замечание P1 устранят), достоверный факт наполнения пультом, гипотеза по реестру
      файлов и честное «состав недоступен»; сверка серверного состава с фактом;
    * **наполнение** — `POST /api/datasets/fill/{id}` (202 без `task_id`): задача
      `dataset-fill` находится в `GET /api/tasks/` и наблюдается монитором до
      терминального статуса; состав фиксируется **до** вызова API;
    * **уборка** — удаление `__TEST__`-датасетов (и тех, что остались от прошлых сессий).

Экран не наполняет боевые датасеты: файлы из датасета удалить нельзя (API не
поддерживает), поэтому такое изменение было бы необратимым.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import streamlit as st

from acceptance.dataset_composition import (
    COMPOSITION_MODES,
    MODE_AUTO,
    SOURCE_LOCAL,
    SOURCE_SERVER,
    compare,
    composition_of,
    composition_rows,
    composition_store,
    read_composition,
    record_composition,
    record_fill,
    store_path,
)
from acceptance.endpoints import TEST_PREFIX
from acceptance.paths import ARTIFACT_DIR, ensure_dirs
from acceptance.records import build_records
from acceptance.session import (
    TestSession,
    add_artifact,
    add_test_entity,
    mark_test_entity_deleted,
    pending_test_entities,
)
from acceptance.tasks_monitor import TaskMonitor
from acceptance.ui import state
from acceptance.ui.common import api_notes
from acceptance.ui.common.flash import render_flash, set_flash
from client.errors import ApiError, ClientError
from client.schemas import DatasetResponse, DatasetType
from lib.polling import poll_until

#: Тип датасета по контракту (`DatasetType` — единственное значение).
DATASET_TYPE: DatasetType = "direct_fill"

#: Тип задачи наполнения датасета.
FILL_TASK_TYPE = "dataset-fill"

KEY_FILTER_NAME = "datasets_filter_name"
KEY_FILTER_DESCRIPTION = "datasets_filter_description"
KEY_FILTER_TYPE = "datasets_filter_type"
KEY_FILTER_DATE = "datasets_filter_date"
KEY_CARD_DATASET = "datasets_card_dataset"
KEY_CARD_MANUAL = "datasets_card_manual"
KEY_CARD_MODE = "datasets_card_mode"
KEY_NEW_NAME = "datasets_new_name"
KEY_NEW_DESCRIPTION = "datasets_new_description"
KEY_FILL_SOURCE = "datasets_fill_source"
KEY_FILL_DATASET = "datasets_fill_dataset"
KEY_FILL_PAIRS = "datasets_fill_pairs"
KEY_FILL_RESPONSIBLE = "datasets_fill_responsible"
KEY_FILL_CONFIRM = "datasets_fill_confirm"
KEY_FILL_WAIT = "datasets_fill_wait"
KEY_FILL_TIMEOUT = "datasets_fill_timeout"
KEY_CLEANUP_CONFIRM = "datasets_cleanup_confirm"
KEY_EXPORT = "datasets_export"

#: Подписи режимов чтения состава (карточка «Состав»).
MODE_LABELS: dict[str, str] = {
    MODE_AUTO: "авто (сервер → факт пульта → гипотеза)",
    "server": "только серверный состав",
    "local": "только локальный учёт пульта",
    "heuristic": "только предположение по реестру файлов",
}
MODE_OPTIONS: tuple[str, ...] = tuple(mode for mode in COMPOSITION_MODES if mode in MODE_LABELS)

#: Колонки выгрузки реестра датасетов (артефакт отчёта).
REGISTRY_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "description",
    "type",
    "creation_date",
    "composition_source",
    "composition_files",
    "note",
)


def render() -> None:
    """Экран «Датасеты»: реестр, создание, состав, наполнение и уборка (FR-4)."""
    render_flash()
    st.title("🗂️ Датасеты")
    st.caption(
        "Модуль «Datasets» (FR-4, UC-09…UC-14). Состав датасета сервер не отдаёт "
        "(замечание P1), поэтому пульт ведёт его учёт сам: факт наполнения, гипотеза по "
        "реестру файлов и сверка с серверным составом, когда тот появится."
    )

    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес испытуемого сервера не задан (TRAINING_SERVER_BASE_URL в .env).")
        return
    session = state.current_session()
    if session is None:
        st.warning("Сессия испытаний не выбрана: откройте экран «Сессия испытаний».")
        return

    datasets, error = state.load_datasets()
    if error:
        st.warning(f"Реестр датасетов недоступен: {error}")
    _render_kpi(session, datasets)

    tabs = st.tabs(("📋 Реестр", "🆕 Создание", "🔎 Состав", "⬆️ Наполнение", "🧹 Уборка"))
    with tabs[0]:
        _render_registry(session, datasets)
    with tabs[1]:
        _render_create(session)
    with tabs[2]:
        _render_composition(session, datasets)
    with tabs[3]:
        _render_fill(session, datasets)
    with tabs[4]:
        _render_cleanup(session, datasets)


def _render_kpi(session: TestSession, datasets: list[DatasetResponse]) -> None:
    """KPI: реестр, тестовые датасеты, учёт состава и задачи наполнения."""
    compositions = composition_store(session)
    test_datasets = [item for item in datasets if str(item.name).startswith(TEST_PREFIX)]
    tasks = [task for task in session.tasks if str(task.get("type")) == FILL_TASK_TYPE]
    columns = st.columns(4)
    columns[0].metric("Датасетов в реестре", len(datasets))
    columns[1].metric("Создано пультом (`__TEST__`)", len(test_datasets))
    columns[2].metric("Записей состава", len(compositions))
    columns[3].metric("Задач `dataset-fill`", len(tasks))
    if compositions:
        by_source = Counter(item.source_label for item in compositions)
        st.caption(
            "Источники состава: "
            + "; ".join(f"{label} — {count}" for label, count in sorted(by_source.items()))
        )


# ---------------------------------------------------------------------------
# Реестр датасетов
# ---------------------------------------------------------------------------
def _matches(
    item: DatasetResponse,
    *,
    name: str,
    description: str,
    dataset_type: str,
    day: date | None,
) -> bool:
    """Клиентский фильтр реестра (серверные фильтры проверяет `TC-DS-02`)."""
    if name and name.casefold() not in str(item.name).casefold():
        return False
    if description and description.casefold() not in str(item.description or "").casefold():
        return False
    if dataset_type and str(item.type) != dataset_type:
        return False
    if day is not None and (item.creation_date is None or item.creation_date.date() != day):
        return False
    return True


def _registry_rows(session: TestSession, datasets: list[DatasetResponse]) -> list[dict[str, Any]]:
    """Строки реестра датасетов с ярлыком состава из учёта пульта."""
    rows: list[dict[str, Any]] = []
    for item in datasets:
        composition = composition_of(session, item.id)
        rows.append(
            {
                "id": str(item.id),
                "name": item.name,
                "description": item.description or "",
                "type": str(item.type),
                "creation_date": (
                    item.creation_date.strftime("%d.%m.%Y %H:%M") if item.creation_date else "—"
                ),
                "composition_source": composition.source_label if composition else "не учтён",
                "composition_files": composition.files_count if composition else 0,
                "note": "тестовый (`__TEST__`)" if str(item.name).startswith(TEST_PREFIX) else "",
            }
        )
    return rows


def _csv_value(value: Any) -> str:
    """Экранирует значение для CSV-выгрузки (разделитель — запятая)."""
    text = str(value if value is not None else "")
    if any(char in text for char in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def _save_registry_artifact(session: TestSession, rows: list[dict[str, Any]]) -> Path:
    """Сохраняет перечень датасетов в артефакты отчёта (CSV) и регистрирует его."""
    ensure_dirs()
    moment = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(ARTIFACT_DIR) / f"datasets-registry-{moment}.csv"
    lines = [",".join(REGISTRY_COLUMNS)]
    for row in rows:
        lines.append(",".join(_csv_value(row.get(column, "")) for column in REGISTRY_COLUMNS))
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    add_artifact(
        session,
        kind="datasets_registry",
        path=path,
        note=f"перечень датасетов: {len(rows)} строк",
    )
    state.store_session(session)
    return path


def _render_registry(session: TestSession, datasets: list[DatasetResponse]) -> None:
    """Вкладка «Реестр»: фильтры, таблица и выгрузка перечня в артефакты."""
    st.subheader("Реестр датасетов")
    col_refresh, col_export = st.columns([1, 1])
    if col_refresh.button("⟳ Обновить реестр", key="datasets_refresh"):
        state.refresh_datasets()
        st.rerun()

    with st.expander("Фильтры (клиентские; серверные фильтры проверяет TC-DS-02)", expanded=False):
        col_name, col_description = st.columns(2)
        name = col_name.text_input("Наименование содержит", key=KEY_FILTER_NAME)
        description = col_description.text_input("Описание содержит", key=KEY_FILTER_DESCRIPTION)
        col_type, col_day = st.columns(2)
        dataset_type = col_type.selectbox("Тип датасета", ["", DATASET_TYPE], key=KEY_FILTER_TYPE)
        day = col_day.date_input("Дата создания", value=None, key=KEY_FILTER_DATE)

    filtered = [
        item
        for item in datasets
        if _matches(
            item,
            name=name.strip(),
            description=description.strip(),
            dataset_type=dataset_type,
            day=day,
        )
    ]
    rows = _registry_rows(session, filtered)
    if not rows:
        st.info("По заданным фильтрам датасетов нет.")
        if not datasets:
            st.caption("Реестр пуст: создайте датасет на вкладке «Создание».")
        return
    st.dataframe(rows, hide_index=True, use_container_width=True)
    if col_export.button("💾 Выгрузить перечень", key=KEY_EXPORT):
        path = _save_registry_artifact(session, rows)
        set_flash("success", f"Перечень датасетов выгружен: {path.name}")
        st.rerun()


# ---------------------------------------------------------------------------
# Создание `__TEST__`-датасета (UC-09)
# ---------------------------------------------------------------------------
def _datasets_api() -> Any:
    """Клиент датасетов активного стенда (экран уже проверил наличие `runtime`)."""
    runtime = state.get_runtime()
    if runtime is None:
        raise ClientError("адрес испытуемого сервера не задан")
    return runtime.apis.datasets


def _name_of(datasets: list[DatasetResponse], dataset_id: str) -> str:
    """Имя датасета по идентификатору (пустая строка, если он не в реестре)."""
    found = next((item for item in datasets if str(item.id) == str(dataset_id)), None)
    return str(found.name) if found is not None else ""


def _render_create(session: TestSession) -> None:
    """Вкладка «Создание»: только `__TEST__`-датасеты (NFR-T4)."""
    st.subheader("Создание датасета")
    st.caption(
        f"Пульт создаёт только тестовые датасеты: имя начинается с `{TEST_PREFIX}`, "
        "`type = direct_fill`. Созданное регистрируется в сессии и убирается на вкладке "
        "«Уборка»; боевые датасеты заводит заказчик."
    )
    name = st.text_input(
        f"Наименование (префикс `{TEST_PREFIX}` обязателен)",
        value=f"{TEST_PREFIX}dataset",
        key=KEY_NEW_NAME,
    )
    description = st.text_input(
        "Описание",
        value=f"создан пультом испытаний, сессия {session.session_id}",
        key=KEY_NEW_DESCRIPTION,
    )
    if st.button("Создать датасет", key="datasets_create", type="primary"):
        if not name.strip().startswith(TEST_PREFIX):
            set_flash("warning", f"Имя датасета должно начинаться с `{TEST_PREFIX}`.")
            st.rerun()
        try:
            created = _datasets_api().create_dataset(
                name=name.strip(),
                description=description.strip() or None,
                dataset_type=DATASET_TYPE,
            )
        except (ApiError, ClientError) as exc:
            set_flash("error", f"Датасет не создан: {exc}")
            st.rerun()
        add_test_entity(
            session,
            entity_id=created.id,
            entity_type="dataset",
            check_id="",
            note=f"имя {created.name} (экран «Датасеты»)",
        )
        session.add_history(
            "dataset_created",
            f"Датасет создан с экрана: {created.name}",
            dataset_id=str(created.id),
            source="экран «Датасеты»",
        )
        state.store_session(session)
        state.refresh_datasets()
        set_flash("success", f"Датасет создан: {created.name} ({created.id})")
        st.rerun()

    pending = [item for item in pending_test_entities(session) if item.get("type") == "dataset"]
    if pending:
        st.divider()
        st.markdown("**Создано пультом и ещё не удалено**")
        st.dataframe(
            [
                {
                    "id": item["id"],
                    "создан": item["at"],
                    "источник": item.get("check_id") or "экран «Датасеты»",
                    "примечание": item.get("note", ""),
                }
                for item in pending
            ],
            hide_index=True,
            use_container_width=True,
        )
        st.caption("Удаление — на вкладке «Уборка» (NFR-T4).")


# ---------------------------------------------------------------------------
# Состав датасета: провайдер, сверка и артефакты (P1)
# ---------------------------------------------------------------------------
def _save_composition_artifact(session: TestSession, composition: Any) -> Path:
    """Сохраняет состав датасета в артефакты отчёта (JSON) и регистрирует его."""
    ensure_dirs()
    moment = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(ARTIFACT_DIR) / f"dataset-composition-{moment}.json"
    payload = {"store": store_path().as_posix(), "composition": composition.to_dict()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    add_artifact(
        session,
        kind="dataset_composition",
        path=path,
        note=f"состав датасета {composition.name or composition.dataset_id}: {composition.source_label}",
    )
    state.store_session(session)
    return path


def _render_composition(session: TestSession, datasets: list[DatasetResponse]) -> None:
    """Вкладка «Состав»: провайдер состава, сверка с фактом и выгрузка."""
    st.subheader("Состав датасета")
    st.caption(
        "Серверного состава и агрегатов в контракте нет (замечание P1), поэтому источник "
        "данных всегда подписан: сервер (эталон), факт наполнения пультом, предположение по "
        "реестру файлов или «состав недоступен»."
    )
    options = {f"{item.name} · {item.type}": str(item.id) for item in datasets}
    chosen = st.selectbox(
        "Датасет из реестра",
        ["", *options],
        key=KEY_CARD_DATASET,
        format_func=lambda value: value or "— не выбран —",
    )
    manual = st.text_input(
        "…или идентификатор датасета (UUID)", key=KEY_CARD_MANUAL, placeholder="5f0a5b7e-…"
    )
    dataset_id = manual.strip() or options.get(str(chosen), "")
    mode = str(
        st.selectbox(
            "Режим чтения",
            list(MODE_OPTIONS),
            key=KEY_CARD_MODE,
            format_func=lambda value: MODE_LABELS.get(value, value),
        )
    )
    if not dataset_id:
        st.info("Выберите датасет из реестра или введите его идентификатор.")
        return

    runtime = state.get_runtime()
    files: list[Any] = []
    files_error = ""
    if runtime is not None:
        try:
            files = list(runtime.apis.files.list_files().files)
        except (ApiError, ClientError) as exc:
            files_error = str(exc)
    if files_error:
        st.warning(f"Реестр файлов недоступен: состав будет без имён и размеров ({files_error})")

    composition = read_composition(
        session,
        runtime.apis if runtime is not None else None,
        dataset_id,
        mode=mode,
        name=_name_of(datasets, dataset_id),
        files=files,
    )
    col_source, col_files, col_counts = st.columns(3)
    col_source.metric("Источник состава", composition.source_label)
    col_files.metric("Файлов в составе", composition.files_count)
    col_counts.metric("Агрегаты от сервера", composition.counts_label or "нет")
    st.caption(composition.note)
    if composition.is_hypothesis:
        st.warning(
            "Состав предполагается по реестру файлов: это гипотеза, её нужно подтвердить "
            "у заказчика — серверного состава в API нет (P1)."
        )
    if composition.files:
        st.dataframe(
            [
                {
                    "роль": file.role_label,
                    "файл": file.file_name or "—",
                    "размер": file.size_label,
                    "тип": file.file_type or "—",
                    "id": file.file_id,
                }
                for file in composition.files
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("Состав не получен: сервер его не сообщает, локальной записи нет.")
    st.dataframe(composition_rows([composition]), hide_index=True, use_container_width=True)

    if composition.source == SOURCE_SERVER:
        local = composition_of(session, dataset_id, source=SOURCE_LOCAL)
        if local is None:
            st.info("Локального факта наполнения нет: сверять серверный состав не с чем.")
        else:
            diff = compare(composition, local)
            message = (
                f"{diff['verdict']}: RAW +{diff['raw']['only_server']} / "
                f"-{diff['raw']['only_local']}, markup +{diff['markup']['only_server']} / "
                f"-{diff['markup']['only_local']}"
            )
            if diff["match"]:
                st.success(message)
            else:
                st.error(message)

    col_record, col_artifact, col_note = st.columns(3)
    if col_record.button("📌 Зафиксировать в учёте", key="datasets_record"):
        record_composition(session, composition)
        state.store_session(session)
        set_flash("success", f"Состав зафиксирован: {composition.source_label}")
        st.rerun()
    if col_artifact.button("💾 Состав в артефакты", key="datasets_composition_artifact"):
        path = _save_composition_artifact(session, composition)
        set_flash("success", f"Состав сохранён: {path.name}")
        st.rerun()
    if col_note.button("✍️ Замечание к API", key="datasets_composition_note"):
        api_notes.create_note_from_template(
            "Нет связи «датасет ↔ файлы» и агрегатов",
            session=session,
            check_id="TC-DS-03",
            module="datasets",
            evidence=composition.note,
        )


# ---------------------------------------------------------------------------
# Наполнение датасета (UC-14)
# ---------------------------------------------------------------------------
def _pair_options(files: list[Any]) -> dict[str, tuple[str, str]]:
    """Пары RAW+markup для наполнения: подпись → (`id` RAW, `id` markup)."""
    overview = build_records(files)
    options: dict[str, tuple[str, str]] = {}
    records = [
        record for record in overview.records if record.has_markup and record.markup is not None
    ]
    records.sort(key=lambda record: record.size_bytes)
    for record in records:
        markup = record.markup
        if markup is None:  # pragma: no cover - отфильтровано выше
            continue
        size = f"{record.size_bytes / 1024 / 1024:.2f} МБ"
        label = f"{record.raw.file_name} + {markup.file_name} · {size}"
        options[label] = (str(record.raw.id), str(markup.id))
    return options


def _newest_fill_task(tasks: list[Any], since: str) -> Any | None:
    """Новейшая задача `dataset-fill`, созданная после подачи запроса (допуск 60 с)."""
    try:
        moment = datetime.fromisoformat(since) - timedelta(seconds=60)
    except ValueError:
        moment = datetime.min
    candidates = [
        task
        for task in tasks
        if task.created_at is not None and task.created_at.replace(tzinfo=None) >= moment
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda task: task.created_at)
    return candidates[-1]


def _find_fill_task(monitor: TaskMonitor, since: str, wait: int) -> Any | None:
    """Ищет задачу наполнения в списке задач (`fill` не отдаёт `task_id`, P1)."""
    started = time.monotonic()
    while True:
        try:
            tasks = monitor.find_recent(task_type=FILL_TASK_TYPE)
        except (ApiError, ClientError):
            tasks = []
        found = _newest_fill_task(tasks, since)
        if found is not None or wait <= 0 or (time.monotonic() - started) >= wait:
            return found
        time.sleep(min(5.0, float(wait)))


def _wait_fill_task(monitor: TaskMonitor, session: TestSession, task_id: str, timeout: int) -> str:
    """Опрашивает задачу наполнения до терминального статуса (возвращает статус)."""
    poll = poll_until(
        lambda: monitor.poll_once(session, task_id, label="TC-DS-05"),
        lambda item: bool(item.terminal or item.unknown or item.error),
        interval=5.0,
        timeout=float(timeout),
    )
    return str(poll.status)


def _render_fill(session: TestSession, datasets: list[DatasetResponse]) -> None:
    """Вкладка «Наполнение»: `fill` → задача `dataset-fill` → терминальный статус."""
    st.subheader("Наполнение датасета")
    st.caption(
        "`POST /api/datasets/fill/{id}` отвечает 202 и **не возвращает `task_id`** (P1): пульт "
        "записывает состав наполнения до вызова API, находит задачу `dataset-fill` в "
        "`GET /api/tasks/` и наблюдает её до терминального статуса."
    )
    st.warning(
        "Наполнять можно только `__TEST__`-датасет: файлы из датасета удалить нельзя (API не "
        "поддерживает), поэтому наполнение боевого датасета было бы необратимым."
    )
    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес испытуемого сервера не задан.")
        return
    monitor = state.task_monitor()
    if monitor is None:
        st.error("Монитор задач недоступен: проверьте адрес стенда.")
        return

    test_datasets = [item for item in datasets if str(item.name).startswith(TEST_PREFIX)]
    source = str(
        st.radio(
            "Что наполнять",
            ("новый `__TEST__`-датасет", "существующий `__TEST__`-датасет"),
            key=KEY_FILL_SOURCE,
            horizontal=True,
        )
    )
    dataset_id = ""
    dataset_name = ""
    if source.startswith("существующий"):
        options = {f"{item.name} · {item.type}": str(item.id) for item in test_datasets}
        chosen = st.selectbox(
            "Тестовый датасет реестра",
            ["", *options],
            key=KEY_FILL_DATASET,
            format_func=lambda value: value or "— не выбран —",
        )
        dataset_id = options.get(str(chosen), "")
        dataset_name = _name_of(test_datasets, dataset_id)
        if not options:
            st.info(
                f"В реестре нет датасетов с префиксом `{TEST_PREFIX}`: создайте его на вкладке "
                "«Создание» или выберите наполнение нового."
            )

    try:
        files = list(runtime.apis.files.list_files().files)
    except (ApiError, ClientError) as exc:
        st.error(f"Реестр файлов недоступен: {exc}")
        return
    pairs = _pair_options(files)
    if not pairs:
        st.info("В реестре нет пар RAW+markup: наполнять нечем.")
        return

    chosen_pairs = st.multiselect(
        "Пары RAW+markup для наполнения",
        list(pairs),
        default=list(pairs)[:1],
        key=KEY_FILL_PAIRS,
    )
    col_wait, col_timeout, col_responsible = st.columns(3)
    wait = int(
        col_wait.number_input(
            "Поиск задачи `dataset-fill`, с",
            min_value=0,
            max_value=300,
            value=30,
            step=5,
            key=KEY_FILL_WAIT,
        )
    )
    timeout = int(
        col_timeout.number_input(
            "Ожидание терминального статуса, с",
            min_value=60,
            max_value=3600,
            value=600,
            step=60,
            key=KEY_FILL_TIMEOUT,
        )
    )
    responsible = col_responsible.text_input(
        "Ответственный", value=session.info.operator_fio, key=KEY_FILL_RESPONSIBLE
    )
    confirmed = st.checkbox(
        "Подтверждаю наполнение: выбранные файлы будут отправлены в датасет (NFR-T4)",
        key=KEY_FILL_CONFIRM,
    )
    if not st.button("⬆️ Наполнить датасет", key="datasets_fill", type="primary"):
        return
    if not chosen_pairs:
        set_flash("warning", "Выберите хотя бы одну пару RAW+markup.")
        st.rerun()
    if not confirmed:
        set_flash("warning", "Подтвердите наполнение (NFR-T4).")
        st.rerun()
    if source.startswith("существующий") and not dataset_id:
        set_flash("warning", "Выберите `__TEST__`-датасет для наполнения.")
        st.rerun()

    if not dataset_id:
        try:
            created = runtime.apis.datasets.create_dataset(
                name=f"{TEST_PREFIX}fill-{datetime.now():%Y%m%d-%H%M%S}",
                description=f"наполнение с экрана, сессия {session.session_id}",
                dataset_type=DATASET_TYPE,
            )
        except (ApiError, ClientError) as exc:
            set_flash("error", f"Датасет не создан: {exc}")
            st.rerun()
        add_test_entity(
            session,
            entity_id=created.id,
            entity_type="dataset",
            check_id="",
            note="создан для наполнения (экран «Датасеты»)",
        )
        dataset_id, dataset_name = str(created.id), str(created.name)
        state.refresh_datasets()

    raw_ids: list[UUID | str] = [pairs[label][0] for label in chosen_pairs]
    markup_ids: list[UUID | str] = [pairs[label][1] for label in chosen_pairs]
    composition = record_fill(
        session,
        dataset_id=dataset_id,
        raw_file_ids=raw_ids,
        markup_file_ids=markup_ids,
        name=dataset_name,
        dataset_type=DATASET_TYPE,
        operator=responsible,
        note="состав наполнения зафиксирован до вызова POST /api/datasets/fill/{dataset_id}",
        files=files,
    )
    state.store_session(session)
    try:
        runtime.apis.datasets.fill_dataset(
            dataset_id, raw_file_ids=raw_ids, markup_file_ids=markup_ids
        )
    except (ApiError, ClientError) as exc:
        set_flash("error", f"Наполнение не принято: {exc}")
        st.rerun()

    task = _find_fill_task(monitor, composition.captured_at, wait)
    if task is None:
        set_flash(
            "error",
            f"Наполнение принято, но задача `{FILL_TASK_TYPE}` не найдена за {wait} с "
            "(обходной путь P1 не работает).",
        )
        st.rerun()
    task_id = str(task.id)
    monitor.register(
        session,
        task_id=task_id,
        task_type=str(task.type),
        name=str(task.name),
        note="задача наполнения с экрана «Датасеты» (`fill` не отдаёт `task_id`, P1)",
    )
    state.store_session(session)

    status = _wait_fill_task(monitor, session, task_id, timeout)
    composition = replace(composition, task_id=task_id, task_status=status)
    record_composition(session, composition)
    state.store_session(session)
    if status == "completed":
        set_flash("success", f"Датасет {dataset_name} наполнен: задача {task_id} завершена.")
    else:
        set_flash(
            "warning",
            f"Задача {task_id} в статусе `{status}`: посмотрите наблюдение на экране «Задачи».",
        )
    st.rerun()


# ---------------------------------------------------------------------------
# Уборка `__TEST__`-датасетов (NFR-T4)
# ---------------------------------------------------------------------------
def _delete_dataset(session: TestSession, dataset_id: str) -> None:
    """Удаляет датасет и закрывает учёт в сессии (ошибка показывается оператору)."""
    try:
        _datasets_api().delete_dataset(dataset_id)
    except (ApiError, ClientError) as exc:
        set_flash("error", f"Датасет {dataset_id} не удалён: {exc}")
        return
    mark_test_entity_deleted(session, dataset_id, note="уборка с экрана «Датасеты»")
    session.add_history(
        "dataset_deleted", f"Датасет удалён с экрана: {dataset_id}", dataset_id=dataset_id
    )
    state.store_session(session)
    state.refresh_datasets()
    set_flash("success", f"Датасет {dataset_id} удалён.")


def _render_cleanup(session: TestSession, datasets: list[DatasetResponse]) -> None:
    """Вкладка «Уборка»: удаление `__TEST__`-датасетов реестра и учёта сессии."""
    st.subheader("Уборка тестовых датасетов")
    st.caption(
        f"Пульт создаёт только `{TEST_PREFIX}`-сущности и убирает их за собой: здесь видно, "
        "что осталось в реестре датасетов и в учёте сессии (NFR-T4, TC-CLEAN-01)."
    )
    test_datasets = [item for item in datasets if str(item.name).startswith(TEST_PREFIX)]
    pending = [item for item in pending_test_entities(session) if item.get("type") == "dataset"]
    if not test_datasets and not pending:
        st.success("Тестовых датасетов в реестре и в учёте сессии нет.")
        return

    if test_datasets:
        st.markdown(f"**`{TEST_PREFIX}`-датасеты в реестре ({len(test_datasets)})**")
        for item in test_datasets:
            columns = st.columns([4, 1])
            created = item.creation_date.strftime("%d.%m.%Y %H:%M") if item.creation_date else "—"
            columns[0].write(f"`{item.name}` · {item.type} · {item.id} · {created}")
            if columns[1].button("🗑 Удалить", key=f"datasets_delete_{item.id}"):
                _delete_dataset(session, str(item.id))
                st.rerun()
        st.divider()
        confirmed = st.checkbox(
            f"Подтверждаю удаление всех `{TEST_PREFIX}`-датасетов реестра ({len(test_datasets)})",
            key=KEY_CLEANUP_CONFIRM,
        )
        if st.button(
            "🗑 Удалить все тестовые датасеты",
            key="datasets_cleanup_all",
            disabled=not confirmed,
            type="primary",
        ):
            removed = 0
            for item in test_datasets:
                _delete_dataset(session, str(item.id))
                removed += 1
            state.store_session(session)
            set_flash("success", f"Удалено тестовых датасетов: {removed}.")
            st.rerun()

    if pending:
        st.markdown("**Учёт сессии: создано пультом и не удалено**")
        st.dataframe(
            [
                {
                    "id": item["id"],
                    "создан": item["at"],
                    "источник": item.get("check_id") or "экран «Датасеты»",
                    "примечание": item.get("note", ""),
                }
                for item in pending
            ],
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            "Если датасета уже нет в реестре, закройте учёт кнопкой ниже — иначе TC-CLEAN-01 "
            "увидит «хвост» самоочистки."
        )
        for record in pending:
            if st.button(
                f"✔️ Отметить удалённым {record['id']}", key=f"datasets_forget_{record['id']}"
            ):
                mark_test_entity_deleted(
                    session, record["id"], note="датасета нет в реестре (уборка с экрана)"
                )
                state.store_session(session)
                set_flash("success", f"Учёт закрыт: {record['id']}.")
                st.rerun()
