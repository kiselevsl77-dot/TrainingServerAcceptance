"""Экран «Консоль запросов» (FR-T3, этап T2).

Произвольный запрос к испытуемому серверу с шаблонами по **всем** эндпоинтам
`SOM1.json` (реестр `acceptance/endpoints.py`): метод, путь, query, JSON-тело,
multipart-файл. Оператор видит статус, заголовки, тело, длительность и размер
ответа, а также номер записи журнала — по нему вызов воспроизводим.

Ключевые правила (NFR-T4, §9 ТЗ):

    * `read` — выполняется без подтверждения;
    * `write` — подтверждение оператора + префикс `__TEST__` у создаваемых имён;
    * `destructive` — двойное подтверждение (чекбокс, слово `DELETE` и полный `id`);
    * `heavy` — карточка запуска (`acceptance.ui.common.run_card`) с целью, данными,
      ответственным и подтверждением расхода ресурсов (FR-T10).

Каждый вызов регистрируется в сессии (`console_calls`) и в структурном журнале
с меткой проверки `TC-…`, поэтому в отчёте видно, что оператор делал вручную.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import streamlit as st

from acceptance import endpoints as ep
from acceptance import notes
from acceptance.endpoints import TEST_PREFIX, EndpointSpec
from acceptance.exchange import ExchangeResult, MultipartPayload, execute_request
from acceptance.logging_setup import log_event
from acceptance.paths import ARTIFACT_DIR
from acceptance.session import (
    TestSession,
    add_artifact,
    add_console_call,
    console_calls_summary,
    register_task_from_console,
)
from acceptance.ui import state
from acceptance.ui.common import api_notes
from acceptance.ui.common.flash import render_flash, set_flash
from acceptance.ui.common.run_card import RunCard, render_run_card
from lib.subdatasets import format_size

#: Ключи состояния экрана (результат вызова живёт до следующего запуска или очистки).
KEY_MODULE = "console_module"
KEY_LABEL = "console_label"
KEY_RESULT = "console_result"
KEY_RESULT_SPEC = "console_result_spec"
KEY_LAST_REQUEST = "console_last_request"

#: Метка по умолчанию: консоль — инструмент диагностики, а не проверка чек-листа.
DEFAULT_LABEL = "TC-CONSOLE"

#: Сколько последних обменов журнала показывать по текущей метке.
HISTORY_LIMIT = 50

#: Порог размера скачиваемого файла, выше которого требуется подтверждение оператора
#: (то же правило, что на экране «Записи», FR-T2).
BINARY_CONFIRM_LIMIT = 50 * 1024 * 1024

#: Сколько задач запрашивать для подстановки `task_id` (пикер path-параметра, этап T4).
TASK_PICKER_LIMIT = 50

#: Поля тела, которые считаются «именем создаваемой сущности» (контроль `__TEST__`).
NAME_KEYS = (
    "name",
    "title",
    "category",
    "model_name",
    "dataset_name",
    "trained_model_name",
)


def render() -> None:
    """Отрисовывает экран «Консоль запросов»."""
    st.title("Консоль запросов")
    st.caption(
        "Произвольные запросы к испытуемому серверу по всем операциям `SOM1.json`. "
        "Изменяющие и ресурсоёмкие запросы выполняются только с подтверждением (NFR-T4); "
        "каждый вызов попадает в журнал с меткой проверки `TC-…`."
    )
    render_flash()

    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес испытуемого сервера не задан (`TRAINING_SERVER_BASE_URL` в `.env`).")
        return

    session = state.current_session()
    _render_session_summary(session)

    tabs = st.tabs(["▶ Запрос", "🕘 История вызовов", "📚 Операции API"])
    with tabs[0]:
        _render_request(runtime=runtime, session=session)
    with tabs[1]:
        _render_history(session=session, runtime=runtime)
    with tabs[2]:
        _render_catalog()


def _render_session_summary(session: TestSession | None) -> None:
    """Сводка ручных вызовов консоли в текущей сессии испытаний."""
    if session is None:
        st.warning(
            "Сессия испытаний не выбрана: вызовы выполнятся и попадут в журнал, "
            "но в отчёт (раздел ручных вызовов) не войдут."
        )
        return

    summary = console_calls_summary(session)
    col_total, col_errors, col_labels = st.columns(3)
    col_total.metric("Вызовов консоли в сессии", summary["total"])
    col_errors.metric("С ошибками", summary["errors"])
    col_labels.metric("Меток проверок", summary["labels"])
    if summary["by_status_class"]:
        st.caption(
            "Статусы ответов: "
            + ", ".join(
                f"{key} — {value}" for key, value in sorted(summary["by_status_class"].items())
            )
        )


def _render_request(*, runtime: state.Runtime, session: TestSession | None) -> None:
    """Вкладка «Запрос»: выбор операции, параметры, подтверждения, результат."""
    module = st.selectbox("Модуль API", ep.module_names(), key=KEY_MODULE)
    options = {spec.menu_label: spec for spec in ep.by_module(module)}
    chosen = st.selectbox("Операция", list(options), key=f"console_op_{module}")
    spec = options[chosen]

    _render_operation_card(spec)

    path_values = _render_path_inputs(spec)
    resolved_path = ep.render_path(spec, path_values)
    st.caption(f"Итоговый путь: `{resolved_path}`")

    query = _render_query_inputs(spec)
    json_body, multipart = _render_body_inputs(spec)

    label = st.text_input(
        "Метка проверки (TC-…)",
        value=st.session_state.get(KEY_LABEL, DEFAULT_LABEL),
        key=KEY_LABEL,
        help="Метка попадает в журнал, сессию и отчёт: по ней видно, для какой проверки "
        "выполнялся запрос (например, TC-FILE-01).",
    )

    allowed, details, run_card = _safety_gate(
        spec,
        path_values=path_values,
        json_body=json_body,
        multipart=multipart,
        session=session,
    )

    missing = ep.missing_path_params(spec, path_values)
    ready = allowed and not missing
    if missing:
        st.caption("Заполните path-параметры: " + ", ".join(missing) + ".")
    if spec.body_kind == ep.BODY_JSON and json_body is None:
        ready = False
        st.caption("Тело запроса должно быть корректным JSON.")
    if spec.body_kind == ep.BODY_MULTIPART and multipart is None:
        ready = False
        st.caption(
            "Тело запроса заполнено не полностью: нужны файл и тип файла (`file_type`) — "
            "оба обязательны по контракту."
        )

    if st.button(
        "▶ Выполнить запрос",
        key=f"console_run_{spec.key}",
        type="primary",
        disabled=not ready,
        use_container_width=True,
    ):
        _run_and_store(
            spec,
            runtime=runtime,
            session=session,
            label=label,
            path=resolved_path,
            query=query,
            json_body=json_body,
            multipart=multipart,
            details=details,
            run_card=run_card,
        )

    result = st.session_state.get(KEY_RESULT)
    result_spec: EndpointSpec | None = st.session_state.get(KEY_RESULT_SPEC)
    if isinstance(result, ExchangeResult) and result_spec is not None:
        st.divider()
        _render_result(result, spec=result_spec, session=session, label=label)


def _render_operation_card(spec: EndpointSpec) -> None:
    """Карточка операции: метод/путь, назначение, класс безопасности, особенности."""
    st.markdown(f"### {spec.safety_icon} `{spec.title}`")
    st.caption(f"{spec.summary} · `{spec.operation_id}` · модуль «{spec.module}»")
    st.caption(
        f"класс безопасности: **{spec.safety_label}** · тело: `{spec.body_kind}` · "
        f"ответ: `{spec.response_kind}`"
    )
    if spec.body_fields:
        st.caption("Поля тела: " + spec.body_fields_label)
    if spec.note:
        st.info(f"Известная особенность операции (см. реестр замечаний): {spec.note}")

    requirement = {
        ep.Safety.READ: "Подтверждение не требуется.",
        ep.Safety.WRITE: (
            "Требуется подтверждение оператора и префикс `__TEST__` у создаваемых имён."
        ),
        ep.Safety.DESTRUCTIVE: (
            "Требуется двойное подтверждение: чекбокс, слово `DELETE` и полный `id`."
        ),
        ep.Safety.HEAVY: "Требуется карточка запуска и подтверждение расхода ресурсов (FR-T10).",
    }
    st.caption(requirement.get(spec.safety, ""))


def _render_path_inputs(spec: EndpointSpec) -> dict[str, str]:
    """Поля path-параметров операции.

    Для `task_id` дополнительно показывается **живой список задач** сервера: выбор
    подставляет идентификатор, а поле остаётся редактируемым (внешняя задача может
    в списке отсутствовать — TC-TASK-08).
    """
    values: dict[str, str] = {}
    for param in spec.path_params():
        if param.name == "task_id":
            _task_picker(spec)
        values[param.name] = st.text_input(
            param.label,
            key=f"console_pp_{spec.key}_{param.name}",
            help=param.description or None,
        )
    return values


def _task_picker(spec: EndpointSpec) -> None:
    """Живой список задач для подстановки `task_id` (снимает ручной копипаст, этап T4)."""
    tasks, total, error = state.load_tasks(limit=TASK_PICKER_LIMIT)
    if error:
        st.caption(f"Список задач недоступен ({error}): введите `task_id` вручную.")
        return
    options = {f"{task.id} · {task.type} · {task.name}": str(task.id) for task in tasks}
    if not options:
        st.caption("Задачи на сервере не найдены: введите `task_id` вручную.")
        return

    key = f"console_task_pick_{spec.key}"
    col_pick, col_use = st.columns([3, 1])
    chosen = col_pick.selectbox(
        "Живой список задач (подстановка `task_id`)",
        list(options),
        key=key,
        help=f"Всего задач у сервера: {total}. Кэш списка — 15 с.",
    )
    if col_use.button("⤵ Подставить", key=f"{key}_use", use_container_width=True):
        st.session_state[f"console_pp_{spec.key}_task_id"] = options[chosen]
        st.rerun()


def _param_help(param: ep.ParamSpec) -> str | None:
    """Подсказка поля параметра: описание из спецификации + требуемый формат.

    Формат важен там, где значение вводится руками: `start_date`/`end_date`
    в `GET /api/tasks/` принимаются только как дата-время (одна дата → 422).
    """
    parts = [text for text in (param.description, param.format_hint) if text]
    return " · ".join(parts) or None


def _render_query_inputs(spec: EndpointSpec) -> dict[str, Any]:
    """Поля query-параметров: по типу из спецификации + свободные параметры."""
    query: dict[str, Any] = {}
    params = spec.query_params()
    if params:
        st.markdown("**Query-параметры**")
        for param in params:
            key = f"console_q_{spec.key}_{param.name}"
            if param.is_boolean:
                if st.checkbox(param.label, key=key, help=_param_help(param)):
                    query[param.name] = "true"
            elif param.is_enum:
                chosen = st.selectbox(
                    param.label, ["", *param.enum], key=key, help=_param_help(param)
                )
                if chosen:
                    query[param.name] = chosen
            else:
                raw = st.text_input(
                    param.label,
                    value=param.default,
                    key=key,
                    help=_param_help(param),
                )
                if raw.strip():
                    query[param.name] = raw.strip()

    extra = st.text_area(
        "Дополнительные параметры (по одному `key=value` в строке)",
        key=f"console_q_extra_{spec.key}",
        height=68,
        placeholder="file_type=RAW",
    )
    query.update(_parse_extra_query(extra))
    return query


def _parse_extra_query(text: str) -> dict[str, str]:
    """Разбирает дополнительные query-параметры вида `key=value` (по строке на параметр)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        name, _, value = line.partition("=")
        name = name.strip()
        if name:
            result[name] = value.strip()
    return result


def _render_body_inputs(
    spec: EndpointSpec,
) -> tuple[dict[str, Any] | None, MultipartPayload | None]:
    """Поля тела запроса: редактор JSON или выбор файла multipart-формы."""
    if spec.body_kind == ep.BODY_JSON:
        st.markdown("**Тело запроса (JSON)**")
        if st.button(
            "Вставить заготовку из спецификации",
            key=f"console_sample_{spec.key}",
            help="Заготовка собрана генератором реестра из схемы тела запроса.",
        ):
            st.session_state[f"console_body_{spec.key}"] = spec.body_sample
            st.rerun()
        raw = st.text_area(
            "JSON-тело",
            value=spec.body_sample,
            key=f"console_body_{spec.key}",
            height=220,
            label_visibility="collapsed",
        )
        if not raw.strip():
            if spec.body_required:
                st.caption("Тело обязательно: заполните JSON.")
                return None, None
            return {}, None
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            st.error(f"Некорректный JSON: {exc}")
            return None, None
        if not isinstance(parsed, dict):
            st.error("Тело запроса должно быть JSON-объектом (словарём).")
            return None, None
        return parsed, None

    if spec.body_kind == ep.BODY_MULTIPART:
        st.markdown("**Тело запроса (multipart/form-data)**")
        uploaded = st.file_uploader("Файл для загрузки", key=f"console_file_{spec.key}")
        col_type, col_desc = st.columns(2)
        file_type = col_type.text_input(
            "Тип файла (file_type) *",
            key=f"console_ft_{spec.key}",
            placeholder="RAW",
            help="Обязательное поле тела: без него сервер отвечает 422 «file_type: Field "
            "required» (подтверждено прогоном 18.09.2026). Известные значения: RAW, LOADS, "
            "ONNX (типы H5/REPORT_ZIP вне перечисления спецификации — замечание P2).",
        )
        description = col_desc.text_input("Описание (description)", key=f"console_fd_{spec.key}")
        if uploaded is None:
            st.caption("Файл не выбран — multipart-запрос без файла не отправляется.")
            return None, None
        if not file_type.strip():
            st.caption(
                "Укажите тип файла: сервер требует `file_type` вместе с файлом "
                "(без него — 422 «Field required»)."
            )
            return None, None
        payload = MultipartPayload(
            file_name=uploaded.name,
            content=uploaded.getvalue(),
            file_type=file_type.strip(),
            description=description.strip(),
        )
        st.caption(
            f"Файл: {uploaded.name} · {format_size(len(payload.content))} · "
            f"MIME `{payload.content_type}`"
        )
        return None, payload

    return None, None


def _safety_gate(
    spec: EndpointSpec,
    *,
    path_values: dict[str, str],
    json_body: dict[str, Any] | None,
    multipart: MultipartPayload | None,
    session: TestSession | None,
) -> tuple[bool, dict[str, Any], RunCard | None]:
    """Подтверждения оператора перед запуском (NFR-T4) и данные для журнала/сессии."""
    details: dict[str, Any] = {"safety": str(spec.safety), "confirmations": [], "names": []}

    if spec.safety == ep.Safety.READ:
        if spec.is_binary:
            size, name = _file_from_registry(path_values)
            if size is None or size <= BINARY_CONFIRM_LIMIT:
                st.caption(
                    "Скачивание файла: содержимое не удерживается в памяти (фиксируются размер "
                    "и имя). "
                    + (f"Размер по реестру: {format_size(size)}." if size else "Размер неизвестен.")
                )
            else:
                st.warning(
                    f"Файл «{name}» — {format_size(size)}: скачивание крупных файлов "
                    f"(> {format_size(BINARY_CONFIRM_LIMIT)}) требует подтверждения оператора."
                )
                confirmed = st.checkbox(
                    "Подтверждаю скачивание крупного файла",
                    key=f"console_binary_confirm_{spec.key}",
                )
                if confirmed:
                    details["confirmations"].append("подтверждение скачивания крупного файла")
                return confirmed, details, None
        return True, details, None

    if spec.safety == ep.Safety.HEAVY:
        card = render_run_card(
            spec,
            key_prefix=f"console_heavy_{spec.key}",
            default_responsible=session.info.operator_fio if session else "",
            default_params=json.dumps(json_body, ensure_ascii=False) if json_body else "",
        )
        details["run_card"] = card.to_dict()
        if card.confirmed:
            details["confirmations"].append("подтверждение расхода ресурсов")
        return card.is_ready, details, card

    names = _created_names(json_body=json_body, multipart=multipart)
    problems = tuple(name for name in names if not name.startswith(TEST_PREFIX))
    details["names"] = sorted(names)

    if spec.safety == ep.Safety.DESTRUCTIVE:
        target_id = next(iter(path_values.values()), "").strip()
        _, target_name = _file_from_registry(path_values)
        needs_id = not target_name.startswith(TEST_PREFIX)

        confirmed = st.checkbox(
            "Подтверждаю удаление данных на общем стенде",
            key=f"console_confirm_{spec.key}",
        )
        word = st.text_input(
            "Введите слово DELETE (подтверждение удаления)",
            key=f"console_delete_word_{spec.key}",
        )
        typed_id = ""
        if needs_id:
            if target_name:
                st.caption(
                    f"Сущность «{target_name}» не помечена `__TEST__`: требуется повторный ввод id."
                )
            else:
                st.caption(
                    "Имя сущности по реестру не определено: требуется повторный ввод полного id."
                )
            typed_id = st.text_input(
                "Повторите полный идентификатор удаляемой сущности",
                key=f"console_delete_id_{spec.key}",
                placeholder=target_id or "id",
            )
        else:
            st.caption(
                f"Сущность «{target_name}» помечена `__TEST__` (создана для испытаний): "
                "достаточно слова `DELETE`."
            )
            details["names"] = [target_name]

        if confirmed:
            details["confirmations"].append("подтверждение удаления")
        if word.strip().upper() == "DELETE":
            details["confirmations"].append("слово DELETE")
        id_confirmed = True
        if needs_id:
            id_confirmed = bool(target_id) and typed_id.strip() == target_id
            if id_confirmed:
                details["confirmations"].append("id подтверждён")
        allowed = confirmed and word.strip().upper() == "DELETE" and id_confirmed
        if not target_id:
            st.error("Заполните путь: идентификатор удаляемой сущности не определён.")
        elif not allowed:
            st.caption(
                "Для удаления нужны: чекбокс, слово `DELETE`"
                + (" и точный идентификатор сущности." if needs_id else ".")
            )
        return allowed, details, None

    confirmed = st.checkbox(
        "Подтверждаю изменяющую операцию на общем стенде (созданное будет удалено после проверки)",
        key=f"console_confirm_{spec.key}",
    )
    if problems:
        st.error(
            "Имена создаваемых сущностей должны начинаться с `__TEST__` (§9 ТЗ): "
            + ", ".join(problems)
        )
    elif names:
        st.caption("Контроль префикса `__TEST__` пройден: " + ", ".join(sorted(names)))
    else:
        st.caption("Операция не создаёт новых сущностей (префикс `__TEST__` не требуется).")
    if confirmed:
        details["confirmations"].append("подтверждение изменяющей операции")
    return confirmed and not problems, details, None


def _created_names(
    *, json_body: dict[str, Any] | None, multipart: MultipartPayload | None
) -> tuple[str, ...]:
    """Имена создаваемых запросом сущностей (для контроля префикса `__TEST__`)."""
    names: list[str] = []
    if multipart is not None:
        names.append(multipart.file_name)
    for key, value in (json_body or {}).items():
        if key.lower() in NAME_KEYS and isinstance(value, str) and value.strip():
            names.append(value.strip())
    return tuple(names)


def _file_from_registry(path_values: dict[str, str]) -> tuple[int | None, str]:
    """Размер и имя файла по `file_id` из кэша реестра (пусто, если файл не найден).

    Нужно, чтобы решения оператора (подтверждение крупного скачивания, ввод `id`
    при удалении) опирались на фактические данные стенда, а не на догадки. Ошибка
    реестра никогда не мешает вызову: возвращается «размер неизвестен, имя пустое».
    """
    file_id = str(path_values.get("file_id", "")).strip()
    if not file_id:
        return None, ""
    try:
        files, error = state.load_files()
    except (OSError, RuntimeError, ValueError, KeyError):
        return None, ""
    if error:
        return None, ""
    for item in files:
        if str(getattr(item, "id", "")) == file_id:
            size = getattr(item, "size", None)
            name = str(getattr(item, "file_name", "") or "")
            return (int(size) if isinstance(size, int) else None), name
    return None, ""


def _response_task_id(result: ExchangeResult, spec: EndpointSpec) -> str:
    """Идентификатор задачи из ответа операции (для монитора задач, этап T4).

    Для чтения (`GET`) и для ресурсоёмких операций (`train`, `check`, `inference`,
    `fill`, диагностика) любой `task_id`/`task`/`id` в ответе — это задача. Для
    остальных изменяющих операций `id` означает **созданную сущность** (файл,
    датасет, модель), поэтому учитываются только явные ключи `task_id`/`task`.
    """
    if spec.method.upper() == "GET" or spec.is_heavy:
        return _task_id_from(result)
    return _task_id_from(result, explicit_only=True)


def _task_id_from(result: ExchangeResult, *, explicit_only: bool = False) -> str:
    """Идентификатор созданной задачи из ответа сервера (для монитора задач, T4).

    `explicit_only=True` — учитываются только явные ключи `task_id`/`task`: у операций,
    возвращающих `id` созданной сущности (файл, датасет, модель), он не является задачей.
    """
    payload = result.body_json
    if not isinstance(payload, dict):
        return ""
    keys = ("task_id", "task") if explicit_only else ("task_id", "task", "id")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _run_and_store(
    spec: EndpointSpec,
    *,
    runtime: state.Runtime,
    session: TestSession | None,
    label: str,
    path: str,
    query: dict[str, Any],
    json_body: dict[str, Any] | None,
    multipart: MultipartPayload | None,
    details: dict[str, Any],
    run_card: RunCard | None,
    keep_content: bool = False,
) -> None:
    """Выполняет запрос консоли, регистрирует его в журнале и сессии, кладёт результат."""
    client = state.console_client()
    if client is None:
        st.error("Клиент консоли недоступен: проверьте адрес испытуемого сервера в `.env`.")
        return None

    timeout = runtime.settings.download_timeout if (spec.is_binary or spec.is_heavy) else None
    with st.spinner(f"Выполняется {spec.title}…"):
        result = execute_request(
            client,
            method=spec.method,
            path=path,
            query=query,
            json_body=json_body,
            multipart=multipart,
            label=label,
            binary=spec.is_binary,
            timeout=timeout,
            body_limit=runtime.config.body_limit,
            journal=runtime.journal,
            keep_content=keep_content or not spec.is_binary,
        )

    task_id = _response_task_id(result, spec)

    log_event(
        "console_request",
        f"Консоль: {spec.title} → {result.status_label}",
        level="INFO" if result.ok else "WARNING",
        module="console",
        check_id=label,
        payload={
            **spec.to_dict(),
            **result.as_dict(),
            "task_id": task_id,
            "confirmations": details.get("confirmations", []),
            "names": details.get("names", []),
            "run_card": details.get("run_card", {}),
        },
    )

    if session is not None:
        add_console_call(
            session,
            label=label,
            method=spec.method,
            path=path,
            status=result.status,
            duration_ms=result.duration_ms,
            journal_seq=result.journal_seq,
            operation=spec.key,
            safety=str(spec.safety),
            request_body=result.request_body,
            error=result.error,
            note=_call_note(details, run_card),
            task_id=task_id,
        )
        if task_id:
            # задача, запущенная из консоли, сразу попадает в наблюдение монитора (FR-T3)
            register_task_from_console(
                session,
                task_id=task_id,
                label=label,
                operation=spec.key,
                journal_seq=result.journal_seq,
            )
        state.store_session(session)

    st.session_state[KEY_RESULT] = result
    st.session_state[KEY_RESULT_SPEC] = spec
    st.session_state[KEY_LAST_REQUEST] = {
        "spec_key": spec.key,
        "path": path,
        "query": dict(query),
        "json_body": json_body,
        "label": label,
        "multipart": multipart is not None,
        "keep_content": keep_content,
    }
    set_flash(
        "success" if result.ok else "warning",
        f"{spec.title}: {result.status_label} за {result.duration_ms:.0f} мс"
        + (f" · запись журнала #{result.journal_seq}" if result.journal_seq else "")
        + (f" · задача {task_id} (для монитора задач)" if task_id else ""),
    )
    st.rerun()


def _call_note(details: dict[str, Any], run_card: RunCard | None) -> str:
    """Краткое пояснение вызова для сессии и отчёта (подтверждения, карточка)."""
    confirmations = details.get("confirmations") or []
    parts: list[str] = []
    if run_card is not None:
        card = run_card.to_dict()
        parts.append(
            "карточка запуска: цель «{goal}», данные «{data}», ответственный {responsible}, "
            "артефакты: {artifacts}".format(
                goal=card["goal"],
                data=card["data"],
                responsible=card["responsible"] or "не указан",
                artifacts=card["artifacts"],
            )
        )
    if confirmations:
        parts.append("подтверждения: " + ", ".join(str(item) for item in confirmations))
    names = details.get("names") or []
    if names:
        parts.append("создаваемые сущности: " + ", ".join(str(name) for name in names))
    return "; ".join(parts)


def _render_result(
    result: ExchangeResult,
    *,
    spec: EndpointSpec,
    session: TestSession | None,
    label: str,
) -> None:
    """Показывает результат вызова и действия над ним."""
    st.markdown(f"#### {result.status_label} · `{result.method} {result.path}`")

    col_status, col_time, col_size, col_seq = st.columns(4)
    col_status.metric("Статус", result.status_label)
    col_time.metric("Длительность", f"{result.duration_ms:.0f} мс")
    col_size.metric("Размер ответа", format_size(result.size_bytes))
    col_seq.metric("Запись журнала", f"#{result.journal_seq}" if result.journal_seq else "—")

    if result.error:
        (st.error if result.is_error else st.warning)(f"{result.error_label}: {result.error}")
    if result.encoding and not result.binary:
        st.caption(f"Кодировка тела: `{result.encoding}`")

    with st.expander("Заголовки ответа", expanded=result.is_error):
        important = result.important_headers()
        if important:
            st.json(important)
        else:
            st.caption("Значимые заголовки не пришли.")
        if len(result.headers) > len(important):
            with st.expander("Все заголовки"):
                st.json(result.headers)

    if result.binary:
        st.info(
            f"Тело — файл: {result.file_name or 'имя не указано'} · "
            f"{format_size(result.size_bytes)} · тип `{result.content_type or 'не указан'}`"
        )
    elif result.body_json is not None:
        st.json(result.body_json)
    elif result.body_text:
        st.code(result.body_preview(), language="text")
    else:
        st.caption("Тело ответа пустое.")

    col_save, col_attach, col_repeat, col_clear = st.columns(4)
    if col_save.button(
        "💾 Ответ в артефакты",
        key=f"console_save_{spec.key}",
        use_container_width=True,
        help="Тело ответа сохраняется в acceptance_data/artifacts и регистрируется в сессии.",
    ):
        _save_result_artifact(result, spec=spec, session=session, label=label)
    if col_attach.button(
        "📌 Приложить к проверке",
        key=f"console_attach_{spec.key}",
        use_container_width=True,
        help="Фиксирует в истории сессии, что обмен относится к проверке с указанной меткой.",
    ):
        _attach_to_check(result, spec=spec, session=session, label=label)
    if col_repeat.button(
        "🔁 Повторить запрос",
        key=f"console_repeat_{spec.key}",
        use_container_width=True,
        help="Повтор разрешён только для читающих операций (изменяющие требуют "
        "повторного подтверждения на вкладке «Запрос»).",
    ):
        _repeat_last(runtime=state.get_runtime(), session=session, label=label)
    if col_clear.button(
        "🧹 Очистить результат", key=f"console_clear_{spec.key}", use_container_width=True
    ):
        _clear_result()

    st.caption(
        f"Воспроизведение запроса (для замечаний к API):\n\n```\n{result.as_curl(_base_url())}\n```"
    )
    _render_task_block(result, spec=spec, session=session, label=label)
    with st.expander("✍ Создать замечание к API из этого ответа"):
        _note_form(result, spec=spec, label=label, session=session)


def _render_task_block(
    result: ExchangeResult,
    *,
    spec: EndpointSpec,
    session: TestSession | None,
    label: str,
) -> None:
    """Действия по задаче из ответа: наблюдение и переход в монитор (этап T4)."""
    task_id = _response_task_id(result, spec)
    if not task_id:
        return

    st.info(f"Задача из ответа: `{task_id}` — доступна монитору задач («⏱️ Задачи»).")
    col_watch, col_monitor = st.columns(2)
    if col_watch.button(
        "🔗 Поставить на наблюдение",
        key=f"console_watch_{spec.key}",
        use_container_width=True,
        help="Монитор будет опрашивать задачу и вести историю переходов FSM-1.",
    ):
        if session is None:
            set_flash("warning", "Сессия не выбрана: наблюдение невозможно.")
        else:
            register_task_from_console(
                session,
                task_id=task_id,
                label=label,
                operation=spec.key,
                journal_seq=result.journal_seq,
            )
            state.store_session(session)
            set_flash("success", f"Задача {task_id} поставлена на наблюдение монитором.")
        st.rerun()
    if col_monitor.button(
        "⏱️ Открыть в мониторе",
        key=f"console_monitor_{spec.key}",
        use_container_width=True,
        help="Переход на экран «$Задачи»: карточка $задачи, история FSM-1, команды.",
    ):
        st.session_state["tasks_focus"] = task_id
        st.session_state["pult_screen"] = "tasks"
        st.rerun()


def _base_url() -> str:
    """Базовый URL испытуемого сервера (для `curl` в замечаниях)."""
    runtime = state.get_runtime()
    return runtime.settings.base_url if runtime is not None else ""


def _clear_result() -> None:
    """Убирает результат вызова с экрана (журнал и сессия не затрагиваются)."""
    st.session_state.pop(KEY_RESULT, None)
    st.session_state.pop(KEY_RESULT_SPEC, None)
    set_flash("info", "Результат убран с экрана (записи журнала и сессии сохранены).")
    st.rerun()


def _save_result_artifact(
    result: ExchangeResult,
    *,
    spec: EndpointSpec,
    session: TestSession | None,
    label: str,
    kind_prefix: str = "консоль",
) -> None:
    """Сохраняет тело ответа в `artifacts/` и регистрирует артефакт в сессии."""
    content = _artifact_content(result)
    if content is None:
        return

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = result.file_name or (
        f"{spec.method.lower()}_{spec.path.strip('/').replace('/', '_')}.txt"
    )
    path = ARTIFACT_DIR / f"console_{stamp}_{name}"
    path.write_bytes(content)

    log_event(
        "console_response_saved",
        f"Ответ консоли сохранён: {path.name}",
        module="console",
        check_id=label,
        payload={
            "endpoint": spec.key,
            "status": result.status,
            "journal_seq": result.journal_seq,
            "size_bytes": len(content),
            "path": path.as_posix(),
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
            kind=f"{kind_prefix}: {spec.title} ({result.status_label})",
            path=path,
            size_bytes=len(content),
            note=f"метка {label}; запись журнала #{result.journal_seq}",
        )
        state.store_session(session)
        set_flash("success", f"Артефакт сохранён и зарегистрирован в сессии: {path.name}")
    st.rerun()


def _artifact_content(result: ExchangeResult) -> bytes | None:
    """Содержимое ответа для артефакта; для файлов — повторное скачивание по требованию."""
    if result.content is not None:
        return result.content
    if result.binary:
        st.caption("Содержимое файла не удерживалось в памяти — выполняется повторное скачивание.")
        fresh = _repeat_last_request(keep_content=True)
        if fresh is None or fresh.content is None:
            set_flash(
                "warning",
                "Не удалось получить содержимое: выберите операцию заново на вкладке «Запрос».",
            )
            st.rerun()
            return None
        return fresh.content
    if result.body_text:
        return result.body_text.encode("utf-8")
    set_flash("warning", "Тело ответа пустое — сохранять нечего.")
    st.rerun()
    return None


def _repeat_last_request(*, keep_content: bool = False) -> ExchangeResult | None:
    """Повторяет последний запрос экрана (только читающие операции, без подтверждений)."""
    last = st.session_state.get(KEY_LAST_REQUEST)
    runtime = state.get_runtime()
    client = state.console_client()
    if not last or runtime is None or client is None or last.get("multipart"):
        return None
    spec = ep.find(str(last.get("spec_key") or ""))
    if spec is None or spec.requires_confirmation:
        return None
    timeout = runtime.settings.download_timeout if (spec.is_binary or spec.is_heavy) else None
    return execute_request(
        client,
        method=spec.method,
        path=str(last.get("path") or spec.path),
        query=dict(last.get("query") or {}),
        json_body=last.get("json_body"),
        label=str(last.get("label") or ""),
        binary=spec.is_binary,
        timeout=timeout,
        body_limit=runtime.config.body_limit,
        journal=runtime.journal,
        keep_content=keep_content or not spec.is_binary,
    )


def _repeat_last(*, runtime: state.Runtime | None, session: TestSession | None, label: str) -> None:
    """Обработчик кнопки «Повторить запрос» (только для читающих операций)."""
    last = st.session_state.get(KEY_LAST_REQUEST)
    if not last:
        set_flash("warning", "Нет запроса для повтора.")
        st.rerun()
    if last.get("multipart"):
        set_flash("warning", "Повтор multipart-запроса недоступен: выберите файл заново.")
        st.rerun()

    spec = ep.find(str(last.get("spec_key") or ""))
    if spec is None:
        set_flash("warning", "Операция не найдена в реестре эндпоинтов.")
        st.rerun()
    if spec.requires_confirmation or runtime is None:
        set_flash(
            "warning",
            "Повтор изменяющей или ресурсоёмкой операции выполняется через вкладку «Запрос» "
            "с повторным подтверждением.",
        )
        st.rerun()

    details: dict[str, Any] = {
        "safety": str(spec.safety),
        "confirmations": ["повтор читающего запроса"],
        "names": [],
    }
    _run_and_store(
        spec,
        runtime=runtime,
        session=session,
        label=str(last.get("label") or label),
        path=str(last.get("path") or spec.path),
        query=dict(last.get("query") or {}),
        json_body=last.get("json_body"),
        multipart=None,
        details=details,
        run_card=None,
    )


def _attach_to_check(
    result: ExchangeResult,
    *,
    spec: EndpointSpec,
    session: TestSession | None,
    label: str,
) -> None:
    """Фиксирует в истории сессии, что обмен относится к проверке с меткой `TC-…`."""
    if session is None:
        set_flash("warning", "Сессия не выбрана: привязка обмена к проверке невозможна.")
        st.rerun()

    session.add_history(
        "console_attachment",
        f"Обмен приложен к проверке {label or '(метка не задана)'}",
        label=label,
        endpoint=spec.key,
        method=result.method,
        path=result.path,
        status=result.status,
        duration_ms=round(result.duration_ms, 1),
        journal_seq=result.journal_seq,
    )
    log_event(
        "console_attachment",
        f"Обмен консоли приложен к проверке {label or '(без метки)'}",
        module="console",
        check_id=label,
        payload={
            "endpoint": spec.key,
            "status": result.status,
            "journal_seq": result.journal_seq,
        },
    )
    state.store_session(session)
    set_flash(
        "success",
        f"Обмен приложен к проверке {label or '(метка не задана)'}: запись журнала "
        f"#{result.journal_seq}. Тело ответа сохраняется кнопкой «💾 Ответ в артефакты».",
    )
    st.rerun()


def _default_fact(result: ExchangeResult) -> str:
    """Факт для замечания: ошибка или краткое описание ответа."""
    if result.error:
        return f"{result.error_label}: {result.error}"
    body = result.body_preview(400)
    return (
        f"Получен ответ {result.status_label} "
        f"({result.content_type or 'Content-Type не указан'}): {body}"
    ).strip()


def _note_form(
    result: ExchangeResult,
    *,
    spec: EndpointSpec,
    label: str,
    session: TestSession | None,
) -> None:
    """Форма создания замечания к API из результата вызова (FR-T7)."""
    st.caption(
        "Факт и воспроизведение предзаполнены из результата: дополните заголовок и ожидание."
    )
    module_options = list(notes.MODULES)
    default_module = spec.module if spec.module in module_options else "Прочее"
    col_priority, col_module = st.columns(2)
    priority = col_priority.selectbox(
        "Приоритет",
        list(notes.PRIORITIES),
        index=1,
        key=f"console_note_priority_{spec.key}",
        help=" · ".join(f"{item} — {notes.PRIORITY_HINTS[item]}" for item in notes.PRIORITIES),
    )
    module = col_module.selectbox(
        "Модуль",
        module_options,
        index=module_options.index(default_module),
        key=f"console_note_module_{spec.key}",
    )
    title = st.text_input(
        "Заголовок замечания",
        value=f"{spec.title}: ",
        key=f"console_note_title_{spec.key}",
    )
    fact = st.text_area(
        "Факт (что произошло)", value=_default_fact(result), key=f"console_note_fact_{spec.key}"
    )
    expected = st.text_area(
        "Ожидание (как должно быть по спецификации)",
        key=f"console_note_expected_{spec.key}",
    )
    reproduction = st.text_area(
        "Воспроизведение",
        value=result.as_curl(_base_url()),
        key=f"console_note_repro_{spec.key}",
    )
    evidence = st.text_input(
        "Доказательства",
        value=f"запись журнала #{result.journal_seq}; метка {label}",
        key=f"console_note_evidence_{spec.key}",
    )
    if st.button("Добавить замечание в сессию", key=f"console_note_add_{spec.key}", type="primary"):
        note = api_notes.manual_note(
            title.strip() or f"{spec.title}: замечание по результату вызова",
            module=module,
            endpoint=spec.title,
            priority=priority,
            fact=fact,
            expected=expected,
            reproduction=reproduction,
            check_id=label or None,
            evidence=evidence,
        )
        api_notes.create_note(note, session=session, module="console")


def _render_history(*, session: TestSession | None, runtime: state.Runtime) -> None:
    """Вкладка «История вызовов»: вызовы сессии и обмены журнала по текущей метке."""
    if session is None:
        st.info(
            "Сессия испытаний не выбрана: история вызовов ведётся только в журнале "
            "(экран «Журнал», фильтр по метке)."
        )
    elif not session.console_calls:
        st.info("В сессии пока нет ручных вызовов консоли — они появятся здесь автоматически.")
    else:
        summary = console_calls_summary(session)
        st.caption(
            "Всего вызовов: {total} · с ошибками: {errors} · меток: {labels}.".format(**summary)
        )
        st.dataframe(
            [
                {
                    "№": index,
                    "Время": call.get("at", ""),
                    "Метка": call.get("label", ""),
                    "Операция": call.get("operation", ""),
                    "Запрос": f"{call.get('method', '')} {call.get('path', '')}",
                    "Статус": call.get("status") if call.get("status") is not None else "—",
                    "мс": call.get("duration_ms") if call.get("duration_ms") is not None else "—",
                    "Журнал": f"#{call['journal_seq']}" if call.get("journal_seq") else "—",
                    "Ошибка": call.get("error", ""),
                }
                for index, call in enumerate(session.console_calls, start=1)
            ],
            hide_index=True,
            use_container_width=True,
        )
        labels = [
            f"{index}. {call.get('method', '')} {call.get('path', '')} · {call.get('at', '')}"
            for index, call in enumerate(session.console_calls, start=1)
        ]
        chosen = st.selectbox("Детали вызова", labels, key="console_history_detail")
        call = session.console_calls[labels.index(chosen)]
        st.json(
            {
                "метка": call.get("label", ""),
                "операция": call.get("operation", ""),
                "класс безопасности": call.get("safety", ""),
                "запрос": f"{call.get('method', '')} {call.get('path', '')}",
                "статус": call.get("status"),
                "длительность, мс": call.get("duration_ms"),
                "запись журнала": call.get("journal_seq"),
                "тело запроса": call.get("request_body", ""),
                "ошибка": call.get("error", ""),
                "примечание": call.get("note", ""),
            }
        )
        st.caption(
            "Полный HTTP-трейс — на экране «Журнал» по метке "
            f"`{call.get('label') or DEFAULT_LABEL}` и номеру записи#{call.get('journal_seq') or '—'}."
        )

    st.divider()
    st.markdown("**Обмены журнала по текущей метке**")
    current_label = str(st.session_state.get(KEY_LABEL) or DEFAULT_LABEL)
    label_records = runtime.journal.for_label(current_label)
    if not label_records:
        st.caption(f"В журнале нет записей с меткой `{current_label}`.")
        return
    st.dataframe(
        [
            {
                "№": record.seq,
                "Запрос": f"{record.method} {record.url}",
                "Статус": record.status if record.status is not None else "—",
                "мс": round(record.duration_ms, 1),
                "Байт": record.response_bytes,
                "Ошибка": record.error or "",
            }
            for record in label_records[-HISTORY_LIMIT:]
        ],
        hide_index=True,
        use_container_width=True,
    )


def _render_catalog() -> None:
    """Вкладка «Операции API»: реестр эндпоинтов, собранный из `SOM1.json`."""
    st.caption(
        "Реестр построен из `docs/SOM1.json` генератором `python -m tools.gen_endpoints`; "
        "расхождение со спецификацией проверяется тестом `tests/unit/test_endpoints.py`."
    )
    st.dataframe(
        [
            {
                "Модуль": spec.module,
                "Метод": spec.method,
                "Путь": spec.path,
                "Класс": spec.safety_icon + " " + spec.safety_label,
                "Тело": spec.body_kind,
                "Ответ": spec.response_kind,
                "Назначение": spec.summary,
                "Особенность (замечание)": spec.note,
            }
            for spec in ep.ENDPOINTS
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.markdown("**Классы безопасности и требования перед запуском**")
    st.table(
        [
            {
                "Класс": item["icon"] + " " + item["label"],
                "Что требуется": {
                    str(ep.Safety.READ): "ничего",
                    str(ep.Safety.WRITE): "подтверждение + префикс `__TEST__` у имён",
                    str(ep.Safety.DESTRUCTIVE): "чекбокс + слово `DELETE` + полный `id`",
                    str(ep.Safety.HEAVY): "карточка запуска + подтверждение ресурсов",
                }.get(item["safety"], ""),
            }
            for item in ep.safety_legend()
        ]
    )
