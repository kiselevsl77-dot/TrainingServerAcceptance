"""View-модели пульта: подписи, статусы и готовые строки таблиц для интерфейса.

Главное правило новой версии (`llm_tasks/31.4` §3): **JS в браузере ничего не
считает**. Сервер отдаёт готовые к отрисовке структуры (подписи, цвета, маркеры,
диапазоны журнала, проценты), поэтому:

* вся логика остаётся в Python и проверяется unit-тестами (в отличие от JS);
* один и тот же расчёт используется API, протоколом, отчётом и выгрузками.

Модуль переносит проверенную логику прежнего интерфейса, потерявшую бы смысл при
переходе на тонкий API:

* `acceptance/ui/components/status.py` → подписи статусов, маркеров, классов и
  происхождения результата (`STATUS_ORDER`, `MARKER_*`, `class_label`, …);
* `acceptance/ui/components/layout.py` → мелкая типографика (`join_parts`, `plural`,
  `percent`, `short_time`, `cell`);
* `acceptance/ui/components/snapshots.py` → сравнение снимков «до/после»;
* `acceptance/ui/components/journal.py` → строки ленты обмена с **независимой**
  подробностью запроса и ответа (`FR-P-21`);
* `acceptance/ui/components/run_card.py` → поля карточки запуска (`FR-P-19`).

Модуль чистый: без интерфейса, без HTTP-сервера и без обращений к стенду.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from acceptance import dataset_composition as composition_api
from acceptance import glossary, run_state, test_payloads
from acceptance import programme as programme_api
from acceptance import queue as queue_api
from acceptance import results as results_api
from acceptance import run_card as run_card_api
from acceptance import runner as runner_api
from acceptance import session as session_api
from acceptance.checks import catalog
from acceptance.checks.registry import CLASS_LABELS, STATUS_ICONS, CheckClass, CheckStatus
from acceptance.endpoints import TEST_PREFIX
from acceptance.exchange import curl_command
from acceptance.queue import MARKER_LAST, MARKER_NEXT, MARKER_RUNNING, QUEUE_ICONS, QUEUE_STATES
from acceptance.session import TestSession
from acceptance.tasks_monitor import TaskMonitor, status_color, status_glyph, status_label
from pult import artifacts as artifacts_api
from pult import models as models_api
from pult import uploads as uploads_api

#: Разделитель подписей в «одной строке» (макет печатает ` · `).
SEPARATOR = " · "

#: Порог «длинного» значения параметра проверки: base64-сигналы в панель не печатаются.
LONG_VALUE_HINT = 64

#: Подписи источников статуса задачи (`acceptance.tasks_monitor`).
SOURCE_LABELS: dict[str, str] = {
    "карточка": "карточка",
    "снимок": "снимок",
    "опрос": "опрос",
    "нет данных": "нет данных",
}


def mask_params(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Маскирует длинные значения параметров проверки (base64-сигналы) для интерфейса.

    В `inference/single` уходит шесть Base64-сигналов по несколько килобайт: печатать
    их в панели «Задачи» и в предупреждениях нельзя — оператору нужен факт выбора,
    а не содержимое (`llm_tasks/52`, шаг 4).
    """
    masked: dict[str, Any] = {}
    for key, value in dict(values or {}).items():
        text = str(value if value is not None else "")
        if len(text) > LONG_VALUE_HINT:
            kind = "base64" if "signal" in str(key).lower() else "длинное значение"
            masked[str(key)] = f"<{kind}: {len(text)} симв.>"
        else:
            masked[str(key)] = value
    return masked


#: Маркеры очереди: как они называются оператору (макет `docs/16` §0).
MARKER_LABELS: dict[str, str] = {
    MARKER_RUNNING: "сейчас",
    MARKER_NEXT: "следующая",
    MARKER_LAST: "последняя",
}

#: Пиктограммы маркеров: «следующая» и «последняя» различаются глазом.
MARKER_ICONS: dict[str, str] = {
    MARKER_RUNNING: "▶",
    MARKER_NEXT: "▷",
    MARKER_LAST: "🕘",
}

#: Происхождение результата (`IR-P-7`: рядом со значением видно, кто его дал).
ORIGIN_LABELS: dict[str, str] = {
    results_api.ORIGIN_RUN: "пульт",
    results_api.ORIGIN_MANUAL: "оператор",
    results_api.ORIGIN_SUSPENDED: "оператор",
    results_api.ORIGIN_REQUEST: "запрос сессии",
}

#: Порядок статусов в фильтрах протокола: сначала «плохие».
STATUS_ORDER: tuple[str, ...] = (
    str(CheckStatus.FAILED),
    str(CheckStatus.BLOCKED),
    str(CheckStatus.INTERRUPTED),
    str(CheckStatus.SKIPPED),
    str(CheckStatus.NOT_RUN),
    str(CheckStatus.MANUAL_OK),
    str(CheckStatus.PASSED),
)

#: Фазы процесса (`docs/14` §3.2): подпись → что означает.
PHASE_LABELS: dict[str, str] = {
    "Ф0": "подготовка: программа, стенд, сессия, снимок «Начало»",
    "Ф1": "прогон программы по одной команде",
    "Ф2": "оценка и фиксация: вердикты, замечания, доказательства",
    "Ф3": "разбор и регрессия после исправлений",
    "Ф4": "финализация: уборка, снимок «Окончание», комплект отчёта",
}


# ---------------------------------------------------------------------------
# Типографика (перенос `ui/components/layout.py`)
# ---------------------------------------------------------------------------
def join_parts(*parts: Any) -> str:
    """Склеивает непустые части через ` · ` (пустые пропускаются)."""
    return SEPARATOR.join(str(part) for part in parts if str(part or "").strip())


def plural(number: int, forms: tuple[str, str, str]) -> str:
    """Русское склонение: `plural(3, ("пункт", "пункта", "пунктов"))` → «3 пункта»."""
    count = abs(int(number))
    if count % 10 == 1 and count % 100 != 11:
        form = forms[0]
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        form = forms[1]
    else:
        form = forms[2]
    return f"{count} {form}"


def percent(done: int, total: int) -> str:
    """Готовность в процентах («34 из 41 · 83 %»); без пунктов — «нет пунктов»."""
    if total <= 0:
        return "нет пунктов"
    share = round(int(done) * 100 / int(total))
    return f"{int(done)} из {int(total)}{SEPARATOR}{share} %"


def short_time(value: Any, format_text: str = "%d.%m %H:%M") -> str:
    """Короткое время для интерфейса («21.09 10:12»); нераспознанный текст — как есть."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    return f"{moment:{format_text}}"


def cell(value: Any) -> str:
    """Значение для таблицы: None → пустая строка, остальное — строкой."""
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# Подписи статусов, маркеров и классов (перенос `ui/components/status.py`)
# ---------------------------------------------------------------------------
def status_icon(status: str) -> str:
    """Пиктограмма статуса проверки («успех» → ✅)."""
    return STATUS_ICONS.get(str(status), "⚪")


def status_labels() -> tuple[str, ...]:
    """Все статусы проверки в порядке фильтров."""
    return STATUS_ORDER


def status_options() -> list[dict[str, str]]:
    """Варианты фильтра «результат»: значение — статус, подпись — статус с пиктограммой."""
    return [
        {"value": status, "label": f"{status_icon(status)} {status}"} for status in STATUS_ORDER
    ]


def option_rows(values: Iterable[Any]) -> list[dict[str, str]]:
    """Варианты фильтра-списка: значения без повторов («все» добавляет интерфейс)."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        item = str(value)
        if not item or item in seen:
            continue
        seen.add(item)
        rows.append({"value": item, "label": item})
    return rows


def queue_state_label(state: str) -> str:
    """Подпись состояния пункта очереди («ожидает», «выполняется», …)."""
    text = str(state)
    return text if text in QUEUE_STATES else str(QUEUE_STATES[0])


def queue_state_icon(state: str) -> str:
    """Пиктограмма состояния пункта очереди («ожидает» → ⚪)."""
    return QUEUE_ICONS.get(str(state), "⚪")


def marker_label(marker: str) -> str:
    """Подпись маркера очереди («следующая», «выполняется», «последняя»); пусто — нет."""
    return MARKER_LABELS.get(str(marker), "")


def marker_icon(marker: str) -> str:
    """Пиктограмма маркера очереди; пусто — нет маркера."""
    return MARKER_ICONS.get(str(marker), "")


def marker_text(marker: str) -> str:
    """«▶ сейчас», «▷ следующая», «🕘 последняя» — или пустая строка."""
    label = marker_label(marker)
    return f"{marker_icon(marker)} {label}".strip() if label else ""


def origin_label(origin: str, *, suspended: bool = False) -> str:
    """Кто дал значение: «пульт», «оператор», «запрос сессии»."""
    if suspended:
        return "оператор"
    return ORIGIN_LABELS.get(str(origin), "")


def class_label(check_class: str | CheckClass) -> str:
    """Человекочитаемый класс проверки (`live` → «боевая (реальные данные)»)."""
    key = str(check_class)
    return CLASS_LABELS.get(key, key)


def class_options() -> list[dict[str, str]]:
    """Классы проверок для фильтра каталога: ключ и подпись (`SCR-101`).

    Список отдаёт сервер, чтобы браузер не повторял справочник классов у себя
    (правило архитектуры: подписи живут в Python).
    """
    return [{"key": str(item), "label": class_label(item)} for item in CheckClass]


def is_confirmable(check_class: str | CheckClass) -> bool:
    """True, если проверке нужна карточка запуска (`live`/`heavy`, `FR-P-19`)."""
    return str(check_class) in (str(CheckClass.LIVE), str(CheckClass.HEAVY))


def is_manual(check_class: str | CheckClass) -> bool:
    """True, если проверка выполняется вручную (`FR-P-32`)."""
    return str(check_class) == str(CheckClass.MANUAL)


#: Способы выполнения проверки: сценарий, «по вызовам», ручная отметка.
SCENARIO_RUN = "сценарием"
SCENARIO_CALL = "по вызовам"
SCENARIO_OPERATOR = "вручную"


def scenario_label(check_id: str) -> str:
    """Чем выполняется проверка: сценарием, «по вызовам» или оператором.

    Группы T6–T9 описаны в каталоге раньше сценариев (этап M0), поэтому подпись
    честно показывает оператору способ выполнения: у такой проверки сценария нет,
    и закрыть её можно вызовами (режим «по вызовам») или ручной отметкой.
    """
    spec = catalog.find(check_id)
    if spec is None:
        return ""
    if is_manual(spec.check_class):
        return SCENARIO_OPERATOR
    return SCENARIO_RUN if spec.automation else SCENARIO_CALL


def scenario_note(check_id: str) -> str:
    """Предупреждение прогона для проверки без сценария (пусто, если сценарий есть)."""
    spec = catalog.find(check_id)
    if spec is None or spec.automation:
        return ""
    if is_manual(spec.check_class):
        return f"{check_id}: проверка ручная — закройте её ручной отметкой"
    return (
        f"{check_id}: сценарий проверки ещё не описан — выполните её «по вызовам» "
        "или отметьте вручную"
    )


def result_text(row: Mapping[str, Any]) -> str:
    """Строка результата для таблицы: «✅ успех · соответствует ожиданию»."""
    icon = str(row.get("icon") or status_icon(str(row.get("status") or "")))
    status = str(row.get("status") or "")
    verdict = str(row.get("verdict") or "")
    return f"{icon} {status}" + (f"{SEPARATOR}{verdict}" if verdict else "")


def build_warning(session: TestSession, current_build: str = "") -> str:
    """Предупреждение о расхождении сборок: испытываем не то, что записано в сессии.

    Молчать об этом нельзя (`AC-P-1`): прогон на другой сборке делает результаты
    неприменимыми к объекту испытаний, зафиксированному в реквизитах (`DR-P-1`).
    """
    stored = str(getattr(session, "server_build", "") or "").strip()
    actual = str(current_build or "").strip()
    if not stored or not actual or stored == actual:
        return ""
    return (
        f"Сборка стенда сейчас — {actual}, а в сессии зафиксирована {stored}: "
        "проверьте объект испытаний и снимите снимок «Начало»"
    )


# ---------------------------------------------------------------------------
# Шапка: «где мы и что дальше» (`SCR-201`)
# ---------------------------------------------------------------------------
def phase_of(
    session: TestSession | None,
    programme: programme_api.Programme,
    results: Mapping[str, Any] | None,
    queue: queue_api.Queue,
) -> str:
    """Фаза процесса Ф0–Ф4 по состоянию сессии и прогона (`docs/14` §3.2).

    Правила (проверяются unit-тестами):

    * нет сессии или программа не утверждена → Ф0 (подготовка);
    * в очереди есть незакрытые пункты → Ф1 (прогон);
    * прогон пройден, но есть отказ или блокировка → Ф3 (разбор);
    * прогон пройден, снимка «Окончание» нет → Ф2 (оценка и фиксация);
    * снимок «Окончание» снят или сессия завершена → Ф4 (финализация).
    """
    if session is None:
        return "Ф0"
    if session.status == session_api.STATUS_CLOSED:
        return "Ф4"
    if not programme.is_approved:
        return "Ф0"
    if queue.pending_ids:
        return "Ф1"
    failed, blocked = _fail_counts(session, results)
    if failed or blocked:
        return "Ф3"
    if session_api.SNAP_END not in session.snapshots:
        return "Ф2"
    return "Ф4"


def _fail_counts(session: TestSession, results: Mapping[str, Any] | None) -> tuple[int, int]:
    """Сколько проверок отказало и сколько блокировано.

    Считается по переданной карте состояния (`results_api.state`), а если её нет —
    по записям самой сессии: фаза не должна «улучшаться» из-за отсутствия аргумента.
    """
    if isinstance(results, Mapping):
        rows = [row for row in results.values() if isinstance(row, Mapping)]
        failed = sum(1 for row in rows if row.get("status") == str(CheckStatus.FAILED))
        blocked = sum(1 for row in rows if row.get("status") == str(CheckStatus.BLOCKED))
        return failed, blocked
    summary = results_api.summary(session)
    return int(summary["failed"]), int(summary["blocked"])


def header_view(
    *,
    stand_configured: bool,
    base_url: str,
    session: TestSession | None,
    programme: programme_api.Programme,
    queue: queue_api.Queue,
    results: Mapping[str, Any] | None = None,
    stand_status: Mapping[str, Any] | None = None,
    log_level: str = "",
) -> dict[str, Any]:
    """Шапка пульта: стенд, сессия, программа, фаза и «что дальше» одной строкой."""
    snapshot = dict(stand_status or {})
    build = build_label(snapshot.get("version")) if snapshot.get("version") else ""
    actual_build = build or (session.server_build if session is not None else "")
    phase = phase_of(session, programme, results, queue)
    next_item = queue.next_item()
    ready = readiness_view(session, programme, queue, stand_configured=stand_configured)
    next_view: dict[str, Any] = {
        "check_id": "" if next_item is None else next_item.check_id,
        "command": ""
        if next_item is None or not ready["runnable"]
        else f"▶ Следующая: {next_item.check_id}",
        "runnable": bool(ready["runnable"]),
        "reason": str(ready["reason"]),
        "blocked_by": str(ready["blocked_by"]),
        "fix": dict(ready["fix"]),
        "notice": str(ready["notice"]),
    }

    session_view: dict[str, Any] = {
        "id": "",
        "status": "",
        "title": "",
        "build": "",
        "operator": "",
    }
    if session is not None:
        session_view = {
            "id": session.session_id,
            "status": session.status,
            "title": session.info.title,
            "build": session.server_build,
            "operator": session.info.operator_fio,
        }
    warning = ""
    if session is not None and actual_build:
        warning = build_warning(session, actual_build)
    return {
        "stand": {
            "configured": stand_configured,
            "base_url": base_url,
            "build": actual_build,
            "snapshot_at": short_time(snapshot.get("at")),
            "warning": warning,
            "errors": dict(snapshot.get("errors") or {}),
        },
        "session": session_view,
        "programme": programme.summary(),
        "queue": run_state.progress(queue, results),
        "phase": {"code": phase, "label": PHASE_LABELS.get(phase, "")},
        "next": next_view,
        "log_level": log_level,
    }


# ---------------------------------------------------------------------------
# Прогон: готовность, карточка запуска, предпросмотр payload и отметка (`SCR-301`)
# ---------------------------------------------------------------------------
#: Кто устраняет препятствие и какой панелью: пульт показывает не только причину,
#: но и кнопку «что делать» (`IR-P-8` — экран не оставляет оператора без выхода).
READINESS_FIXES: dict[str, dict[str, str]] = {
    "no-session": {"panel": "sessions", "label": "Создать и начать сессию"},
    "no-stand": {"panel": "", "label": "Проверить адрес стенда в .env"},
    "no-programme": {"panel": "planning", "label": "Собрать программу"},
    "not-approved": {"panel": "planning", "label": "Утвердить программу"},
    "queue": {"panel": "planning", "label": "Пересобрать и утвердить программу"},
    "finished": {"panel": "protocol", "label": "Оформить протокол"},
}

#: Режимы прогона (ТЗ §2, решение 7): какие пункты берёт «▶ Следующая».
#: Значения и переключение живут в ядре (`acceptance.run_state`), подписи — здесь.
RUN_MODE_LABELS: dict[str, str] = {
    run_state.RUN_ONLY_REMAINING: "только невыполненное",
    run_state.RUN_ALL: "всё подряд (с повторами)",
}

RUN_MODE_HINTS: dict[str, str] = {
    run_state.RUN_ONLY_REMAINING: (
        "выполненные и снятые пункты пропускаются: повтор уже выполненного — "
        "отдельное действие (`FR-P-36`)"
    ),
    run_state.RUN_ALL: (
        "все пункты запускаются заново, включая выполненные; повтор пишет историю "
        "предыдущего результата (`FR-P-36`)"
    ),
}

#: Как показывать поле карточки запуска: вид элемента формы (`FR-P-19`).
CARD_FIELD_KINDS: dict[str, str] = {
    "goal": "text",
    "data": "text",
    "dataset_id": "select",
    "model_id": "select",
    "epochs": "number",
    "batch_size": "number",
    "learning_rate": "number",
    "threshold": "number",
    "sample_file_id": "text",
    "keep_artifact": "checkbox",
    "responsible": "text",
    "artifacts": "select",
    "confirm": "checkbox",
}

#: Что пульт пишет в «Данные, на которых выполняется», когда датасет ещё не выбран:
#: молчаливое «`__TEST__`» на ML-проверке было бы неправдой (`llm_tasks/49`, задача 1б).
DATASET_NOT_CHOSEN = "датасет не выбран: выберите его в поле «Датасет для проверки»"

#: Склонения для подписи числа файлов датасета.
FILES_FORMS: tuple[str, str, str] = ("файл", "файла", "файлов")

#: Статусы, которые оператор ставит вручную (`FR-P-32`): отметка, а не прогон.
MARK_STATUSES: tuple[str, ...] = (
    str(CheckStatus.MANUAL_OK),
    str(CheckStatus.SKIPPED),
    str(CheckStatus.BLOCKED),
    str(CheckStatus.INTERRUPTED),
    str(CheckStatus.FAILED),
)


def readiness_view(
    session: TestSession | None,
    programme: programme_api.Programme,
    queue: queue_api.Queue,
    *,
    stand_configured: bool = True,
) -> dict[str, Any]:
    """Можно ли нажимать «▶ Следующая» и что мешает — **единое** правило пульта.

    Ядро допускает прогон по черновику программы и помечает его (`queue.build_queue`),
    но пульт так не работает: испытания ведутся по **утверждённой** ревизии
    (`FR-P-13`, `IR-P-18`, решение заказчика 26.09.2026). Правило живёт здесь, а не в
    шапке и не в JS: шапка, экран «Прогон» и проверка действия запуска берут его из
    одного места, поэтому кнопка не может быть выключена «по одной причине», а API
    отказать «по другой».

    Returns:
        `runnable`, `reason`, `blocked_by` (код для тестов и подсказок), `fix`
        (панель и подпись кнопки «что делать»), `notice` (предупреждение прогона).
    """
    blocked = ""
    reason = ""
    if session is None:
        blocked, reason = "no-session", "Сессия испытаний не выбрана"
    elif not stand_configured:
        blocked, reason = "no-stand", "Адрес испытуемого сервера не задан (.env)"
    elif not programme.size:
        blocked, reason = "no-programme", "Программа сессии не собрана"
    elif not programme.is_approved:
        blocked = "not-approved"
        reason = (
            f"Программа сессии не утверждена (ревизия {programme.revision}, черновик): "
            "прогон идёт по утверждённой ревизии"
        )
    else:
        allowed, why = runner_api.can_run(queue, programme)
        if not allowed:
            blocked = "finished" if queue.next_item() is None else "queue"
            reason = why
    return {
        "runnable": not blocked,
        "reason": reason,
        "blocked_by": blocked,
        "fix": dict(READINESS_FIXES.get(blocked, {})),
        "notice": "" if blocked else runner_api.programme_notice(queue),
    }


def modes_view(session: TestSession) -> dict[str, Any]:
    """Режим прогона: «только невыполненное» (по умолчанию) или «всё подряд» (`UC-09`)."""
    current = run_state.run_mode_of(session)
    return {
        "current": current,
        "current_label": RUN_MODE_LABELS[current],
        "options": [
            {"value": value, "label": label, "hint": RUN_MODE_HINTS[value]}
            for value, label in RUN_MODE_LABELS.items()
        ],
    }


def run_card_view(
    item: programme_api.ProgrammeItem,
    *,
    values: Mapping[str, Any] | None = None,
    session_id: str = "",
    author: str = "",
    options: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
    extra_required: Sequence[str] = (),
    data_hint: str = "",
) -> dict[str, Any]:
    """Карточка запуска как форма: поля, заготовки и что ещё не заполнено (`FR-P-19`).

    Заготовки приходят из `acceptance.run_card.defaults`, правила обязательных полей —
    из `acceptance.run_card.validate`, поэтому форма интерфейса и проверка ядра не
    расходятся. `filled` честно False, пока оператор не подтвердил расход ресурсов.

    `options` — варианты выбора для полей-селектов (датасет и модель приходят из стенда),
    `extra_required` — поля, обязательные именно для этой проверки, `data_hint` — подпись
    данных («датасет «skfu-train» (104 файла)»): она уходит в доказательства результата
    (`llm_tasks/49`, задача 1б).
    """
    required, note = runner_api.needs_confirmation(item)
    saved = {key: value for key, value in dict(values or {}).items() if value not in ("", None)}
    merged: dict[str, Any] = dict(
        run_card_api.defaults(item, session_id=session_id, author=author, data_hint=data_hint)
    )
    merged.update(saved)
    known = {
        str(name): [dict(option) for option in list(field_options or [])]
        for name, field_options in dict(options or {}).items()
    }
    fields: list[dict[str, Any]] = []
    for name, label in run_card_api.FIELD_LABELS.items():
        value = merged.get(name, "")
        field: dict[str, Any] = {
            "name": name,
            "label": label,
            "kind": CARD_FIELD_KINDS.get(name, "text"),
            "value": "" if isinstance(value, bool) else str(value),
            "checked": bool(value) if name == "confirm" else False,
            "required": (
                name in run_card_api.REQUIRED or name == "confirm" or name in extra_required
            ),
        }
        if name in known:
            field["options"] = known[name]
        fields.append(field)
    missing = run_card_api.validate(merged, required_extra=extra_required)
    return {
        "required": bool(required),
        "note": str(note),
        "fields": fields,
        "artifacts": list(run_card_api.ARTIFACT_ACTIONS),
        "missing": missing,
        "filled": not missing,
        "saved": bool(saved),
        "author": str(saved.get("author") or ""),
        "at": short_time(saved.get("at")) if saved.get("at") else "",
    }


def dataset_options(
    datasets: Sequence[Any],
    compositions: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Варианты поля «Датасет для проверки» (`TC-TR-02`, `TC-INF-01`).

    Подпись — «имя · файлов N · RAW R / markup M · достоверность», когда состав известен
    (он читается на странице «Результаты» и для выбранного датасета), иначе «имя · тип»:
    читать составы всех датасетов на отрисовку экрана прогона нельзя — за каждым стоит
    обращение к `$Задачам` стенда (`llm_tasks/49`, задача 1б).
    """
    known = dict(compositions or {})
    rows: list[dict[str, str]] = []
    for item in datasets:
        dataset_id = str(_attr(item, "id") or "")
        name = str(_attr(item, "name") or dataset_id)
        composition = known.get(dataset_id)
        label = (
            composition_option_label(name, composition)
            if composition is not None
            else join_parts(name, str(_attr(item, "type") or ""))
        )
        rows.append({"value": dataset_id, "label": label})
    return rows


def model_options(models: Iterable[Any]) -> list[dict[str, str]]:
    """Варианты поля «Модель»: «имя · архитектура · версия» (`TC-TR-02`, `TC-INF-*`)."""
    rows: list[dict[str, str]] = []
    for item in models:
        payload = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        model_id = str(payload.get("id") or "")
        version = str(payload.get("version") or "")
        rows.append(
            {
                "value": model_id,
                "label": join_parts(
                    str(payload.get("name") or model_id),
                    str(payload.get("architecture") or ""),
                    f"v{version}" if version else "",
                ),
            }
        )
    return rows


def dataset_hint(label: str, composition: Any = None) -> str:
    """Подсказка для поля «Данные, на которых выполняется»: «датасет «X» (N файлов)».

    Числа берутся из состава, когда он прочитан; без состава пульт не выдумывает
    «файлов 0», а просто называет датасет (`llm_tasks/48` §1б, вопрос 5 = а).
    """
    name = str(label or "").strip()
    if not name:
        return DATASET_NOT_CHOSEN
    if composition is None:
        return f"датасет «{name}»"
    return f"датасет «{name}» ({plural(int(composition.files_count), FILES_FORMS)})"


def ml_view(
    item: programme_api.ProgrammeItem | None,
    *,
    datasets: Sequence[Any] = (),
    compositions: Mapping[str, Any] | None = None,
    models: Sequence[Any] = (),
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Выбор данных и модели для ML-проверки (`llm_tasks/49`, задача 1б).

    Всё решает сервер: нужны ли проверке датасет и модель (правило — из спецификации,
    `runner.ml_params_required`), какие варианты предложить, куда сохранить выбор
    (`run-card` для `live`/`heavy` и `ml-params` для проверок без карточки — решение
    заказчика 2 = а) и что делать, если моделей на стенде нет. Интерфейс печатает форму
    и не решает, обязателен ли датасет.

    Место сохранения выбирается тем же правилом, что и карточка запуска
    (`runner.needs_confirmation`), — иначе форма и запуск разошлись бы.
    """
    if item is None:
        return {}
    spec = catalog.find(item.check_id)
    if spec is None:
        return {}
    needs = runner_api.ml_params_required(spec)
    if not (needs["dataset"] or needs["model"]):
        return {"needs": needs, "panel": "", "fields": [], "save_as": ""}
    saved = {
        str(key): value for key, value in dict(values or {}).items() if str(value or "").strip()
    }
    known = dict(compositions or {})
    dataset_id = str(saved.get(runner_api.ML_DATASET_FIELD) or "")
    found = next((row for row in datasets if str(_attr(row, "id")) == dataset_id), None)
    dataset_name = str(_attr(found, "name")) if found is not None else ""
    dataset_composition = known.get(dataset_id)
    model_id = str(saved.get(runner_api.ML_MODEL_PARAM) or "")
    model_label = (
        next((row["label"] for row in model_options(models) if row["value"] == model_id), model_id)
        if model_id
        else ""
    )
    fields: list[dict[str, Any]] = []
    if needs["dataset"]:
        fields.append(
            {
                "name": runner_api.ML_DATASET_FIELD,
                "label": run_card_api.FIELD_LABELS[runner_api.ML_DATASET_FIELD],
                "kind": "select",
                "value": dataset_id,
                "options": dataset_options(datasets, known),
                "required": True,
            }
        )
    if needs["model"]:
        fields.append(
            {
                "name": runner_api.ML_MODEL_PARAM,
                "label": run_card_api.FIELD_LABELS[runner_api.ML_MODEL_PARAM],
                "kind": "select" if models else "text",
                "value": model_id,
                "options": model_options(models),
                "required": True,
            }
        )
    notes: list[str] = []
    if needs["model"] and not models:
        notes.append(
            "моделей на стенде нет: сначала создайте модель проверкой «TC-MOD-02», "
            "потом выберите её здесь (или впишите `model_id` вручную)"
        )
    if needs["dataset"] and not datasets:
        notes.append("датасетов на стенде нет: наполните датасет (`TC-DS-01`, `TC-DS-05`)")
    by_card = bool(runner_api.needs_confirmation(item)[0])
    return {
        "needs": needs,
        "required": True,
        "panel": "card" if by_card else "params",
        "save_as": "run-card" if by_card else "ml-params",
        "fields": fields,
        "note": join_parts(*notes),
        "hint": (
            "выбор — часть карточки запуска: он уходит в доказательства и протокол"
            if by_card
            else "выбор уходит в `params` запуска и в доказательства результата"
        ),
        "values": {
            runner_api.ML_DATASET_FIELD: dataset_id,
            "dataset_label": dataset_name,
            runner_api.ML_MODEL_PARAM: model_id,
            "model_label": model_label,
        },
        "data_hint": dataset_hint(dataset_name, dataset_composition) if needs["dataset"] else "",
    }


def payload_view(
    check_id: str, *, session: Any = None, params: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Предпросмотр «что будет отправлено»: `__TEST__`-имена и тела (`FR-P-19`).

    Строки формирует `acceptance.test_payloads` — тот же модуль, что и сценарии, поэтому
    предпросмотр не расходится с фактом. Проверки только для чтения payload не создают.
    """
    spec = catalog.find(check_id)
    if spec is None:
        return {"summary": "", "lines": [], "count": 0}
    previews = test_payloads.payload_previews(spec, session, params=dict(params or {}))
    return {
        "summary": test_payloads.summary(previews),
        "lines": test_payloads.preview_lines(previews),
        "count": len(previews),
    }


def mark_view(item: programme_api.ProgrammeItem) -> dict[str, Any]:
    """Форма ручной отметки: статусы и требование заключения (`FR-P-32`)."""
    required, note = runner_api.requires_operator(item)
    return {
        "required": bool(required),
        "check_id": item.check_id,
        "note": str(note) or "ручная проверка: отметьте результат с заключением",
        "statuses": [
            {"value": value, "label": f"{status_icon(value)} {value}"} for value in MARK_STATUSES
        ],
        "default_status": str(CheckStatus.MANUAL_OK),
        "note_label": "Заключение оператора",
        "note_hint": "без заключения отметка не принимается (`FR-P-32`)",
    }


def batch_view(queue: queue_api.Queue, programme: programme_api.Programme) -> dict[str, Any]:
    """«▶▶ Пачка tech (n)»: сколько пунктов заберёт пачка и до чьего препятствия (`A4`)."""
    ids = list(runner_api.batch_tech_ids(queue, programme))
    return {
        "ids": ids,
        "count": len(ids),
        "label": f"▶▶ Пачка tech ({len(ids)})" if ids else "▶▶ Пачка tech",
        "enabled": bool(ids),
    }


def step_rows(check_id: str) -> list[dict[str, Any]]:
    """Шаги проверки: что именно будет вызвано, по порядку (`FR-P-20`)."""
    spec = catalog.find(check_id)
    if spec is None:
        return []
    return [
        {
            "index": int(step.index),
            "label": f"{int(step.index)}. {str(step.method).upper()} {step.path}",
            "summary": str(step.summary),
            "safety": str(step.safety),
            "body": str(step.body_sample),
        }
        for step in runner_api.call_steps(spec)
    ]


def run_next_view(
    item: programme_api.ProgrammeItem | None,
    *,
    values: Mapping[str, Any] | None = None,
    session: Any = None,
    author: str = "",
    ml: Mapping[str, Any] | None = None,
    card_options: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
    extra_required: Sequence[str] = (),
    data_hint: str = "",
) -> dict[str, Any]:
    """Следующий пункт прогона: что запустится и что для этого нужно (`IR-P-20`).

    Всё, что интерфейсу нужно показать до нажатия «▶ Следующая» — название и класс
    пункта, требования (карточка запуска, ручная отметка), шаги, предпросмотр payload,
    форма отметки и выбор данных/модели (`ml`) — приходит готовым. Это позволяет
    оператору видеть последствия до вызова стенда (`FR-P-19`, `FR-P-20`) и не
    дублировать правила в JS.
    """
    if item is None:
        return {}
    session_id = str(getattr(session, "session_id", "") or "")
    confirm, confirm_note = runner_api.needs_confirmation(item)
    operator, operator_note = runner_api.requires_operator(item)
    return {
        "check_id": item.check_id,
        "title": item.title,
        "order": int(item.order),
        "check_class": str(item.check_class),
        "class_label": class_label(item.check_class),
        "section": item.group,
        "requirement": item.requirement,
        "expected": item.expected,
        "confirm_required": bool(confirm),
        "confirm_note": str(confirm_note),
        "operator_required": bool(operator),
        "operator_note": str(operator_note),
        "scenario": scenario_label(item.check_id),
        "scenario_note": scenario_note(item.check_id),
        "steps": step_rows(item.check_id),
        "card": run_card_view(
            item,
            values=values,
            session_id=session_id,
            author=author,
            options=card_options,
            extra_required=extra_required,
            data_hint=data_hint,
        ),
        "payload": payload_view(item.check_id, session=session, params=values),
        "mark": mark_view(item),
        "ml": dict(ml or {}),
    }


def build_label(version: Any) -> str:
    """Подпись сборки из ответа `GET /version` («dev@83319ae»)."""
    body = dict(version) if isinstance(version, Mapping) else {}
    revision = str(body.get("revision") or body.get("commit") or "")[:12]
    branch = str(body.get("branch") or "")
    if revision and branch:
        return f"{branch}@{revision}"
    return revision or branch


def outcome_rows(outcomes: Iterable[Any]) -> list[dict[str, Any]]:
    """Итоги прогона для интерфейса: проверка, статус, вердикт, журнал, останов (`FR-P-30`).

    Итог несёт то, что нужно оператору сразу: что запустилось, чем закончилось, в каких
    записях журнала это видно и почему прогон остановился (`StopReason` не теряется).
    """
    rows: list[dict[str, Any]] = []
    for outcome in outcomes:
        rows.append(
            {
                "check_id": str(getattr(outcome, "check_id", "") or ""),
                "ok": bool(getattr(outcome, "ok", False)),
                "icon": str(getattr(outcome, "icon", "") or ""),
                "state": str(getattr(outcome, "state", "") or ""),
                "status": str(getattr(outcome, "status", "") or ""),
                "verdict": str(getattr(outcome, "verdict", "") or ""),
                "detail": str(getattr(outcome, "detail", "") or ""),
                "journal": str(getattr(outcome, "journal_range", "") or ""),
                "stopped": bool(getattr(outcome, "stopped", False)),
                "stop_reason": str(getattr(outcome, "stop_reason", "") or ""),
                "duration_ms": round(float(getattr(outcome, "duration_ms", 0.0) or 0.0), 1),
                "steps": int(getattr(outcome, "steps", 0) or 0),
                "confirmation_required": bool(getattr(outcome, "confirmation_required", False)),
                "operator_required": bool(getattr(outcome, "operator_required", False)),
                "scenario_missing": bool(getattr(outcome, "scenario_missing", False)),
                "hint": _outcome_hint(outcome),
            }
        )
    return rows


def _outcome_hint(outcome: Any) -> str:
    """Что оператору делать по итогу шага (пусто — делать нечего).

    Интерфейс не выдумывает подсказки: «запрос на стенд не отправлялся, потому что нужна
    карточка запуска» — факт ядра (`RunOutcome.confirmation_required`), и он должен
    дойти до оператора тем же текстом, что и в протоколе.
    """
    if bool(getattr(outcome, "confirmation_required", False)):
        return (
            "запрос на стенд не отправлен: заполните карточку запуска и подтвердите "
            "расход ресурсов (`FR-P-19`)"
        )
    if bool(getattr(outcome, "operator_required", False)):
        return "ручная проверка: поставьте отметку с заключением (`FR-P-32`)"
    if bool(getattr(outcome, "scenario_missing", False)):
        return (
            "сценарий проверки ещё не описан: выполните её «по вызовам» "
            "или поставьте ручную отметку"
        )
    return ""


def sessions_view(
    sessions: Sequence[Mapping[str, Any]], current: str | None = None
) -> list[dict[str, Any]]:
    """Список сессий для выбора: подписи, прогресс и отметка текущей."""
    rows: list[dict[str, Any]] = []
    for item in sessions:
        session_id = str(item.get("session_id") or "")
        rows.append(
            {
                "id": session_id,
                "title": str(item.get("title") or "без наименования"),
                "status": str(item.get("status") or ""),
                "build": str(item.get("server_build") or ""),
                "checks": int(item.get("checks") or 0),
                "done": int(item.get("done") or 0),
                "created_at": short_time(item.get("created_at")),
                "current": bool(current and session_id == current),
            }
        )
    return rows


def run_rows(
    session: TestSession, programme: programme_api.Programme, queue: queue_api.Queue
) -> list[dict[str, Any]]:
    """Строки прогона: маркер, состояние, проверка, результат и способ получения.

    Способ получения (`origin`) и пометка «выполнено по факту запроса» приходят из
    `results.py`: подтверждение запросом не выдаётся за запуск пункта программы.
    """
    states = results_api.state(session)
    rows: list[dict[str, Any]] = []
    for row in queue.rows(states):
        check_id = str(row["check_id"])
        item = programme.find(check_id)
        state = states.get(check_id)
        state_row: Mapping[str, Any] = state if isinstance(state, Mapping) else {}
        evidence = state_row.get("evidence")
        note = str(dict(evidence).get("note") or "") if isinstance(evidence, Mapping) else ""
        origin = str(state_row.get("origin") or "")
        rows.append(
            {
                "order": int(row["order"]),
                "check_id": check_id,
                "title": item.title if item is not None else "",
                "section": item.group if item is not None else "",
                "check_class": class_label(item.check_class) if item is not None else "",
                "requirement": item.requirement if item is not None else "",
                "expected": item.expected if item is not None else "",
                "marker": marker_text(str(row["marker"])),
                "state": str(row["state"]),
                "queue_state": (
                    f"{queue_state_icon(str(row['state']))} {queue_state_label(str(row['state']))}"
                ),
                "result": result_text(state_row),
                "status": str(row["status"]),
                "verdict": str(row["verdict"]),
                "repeats": int(row["repeats"]),
                "journal": str(row["journal_range"]),
                "reason": str(row["reason"] or row["note"]),
                "origin": origin_label(origin, suspended=bool(state_row.get("suspended"))),
                "done_by": note if origin == results_api.ORIGIN_REQUEST else "",
                "confirm_required": bool(
                    item is not None and runner_api.needs_confirmation(item)[0]
                ),
                "operator_required": bool(
                    item is not None and runner_api.requires_operator(item)[0]
                ),
                "details": {
                    "steps": step_rows(check_id),
                    "payload": payload_view(check_id, session=session),
                    "author": str(state_row.get("author") or ""),
                    "at": short_time(state_row.get("at"), "%d.%m %H:%M:%S")
                    if state_row.get("at")
                    else "",
                    "revision": state_row.get("revision"),
                    "note": note,
                },
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Лента обмена (`FR-P-21`…`FR-P-23`)
# ---------------------------------------------------------------------------
def _size(label: str, value: Any) -> str:
    """Подпись размера тела: «запрос 1.2 КБ» (пусто, если сервер размера не дал)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number >= 1024 * 1024:
        return f"{label} {number / 1048576:.1f} МБ"
    if number >= 1024:
        return f"{label} {number / 1024:.1f} КБ"
    return f"{label} {number} Б"


def body_note(
    *,
    body: str,
    size: int | None,
    content_type: str,
    truncated: bool,
    method: str = "",
    bodies_logged: bool = True,
    limit: int = 0,
    kind: str = "запрос",
) -> str:
    """Что сказать оператору вместо пустого тела (`FR-P-21`).

    Пустое тело — не всегда потеря данных: у `GET`/`HEAD` тела нет вовсе, у ответа `204`
    его тоже нет, бинарные данные не сохраняются по замыслу, а при выключенном
    журналировании тел (`PULT_LOG_BODIES=false`) тело есть, но его не пишут. Интерфейс
    должен различать эти случаи, поэтому подпись считает сервер, а не пустая строка:
    иначе оператор читает «тело не сохранилось» там, где сохранять было нечего.
    """
    if body:
        if truncated and limit:
            return f"тело обрезано по лимиту {limit} символов"
        return ""
    if not size:
        if kind != "запрос":
            return "тела нет: ответ без тела (204 или пустой ответ)"
        return (
            "тела нет: запрос без тела (GET/HEAD)"
            if str(method).upper() in ("GET", "HEAD", "OPTIONS")
            else "тела нет: запрос отправлен без тела"
        )
    if "multipart/form-data" in str(content_type).lower():
        return f"тело не сохраняется: multipart/form-data, {size} байт (смотрите размер)"
    if not bodies_logged:
        return "тело не сохранялось: журналирование тел выключено (`PULT_LOG_BODIES`)"
    return f"тело не сохраняется: не текстовый формат, {size} байт (смотрите размер)"


def journal_row(
    record: Any,
    *,
    base_url: str = "",
    detail_request: bool = True,
    detail_response: bool = True,
    bodies_logged: bool = True,
    body_limit: int = 0,
) -> dict[str, Any]:
    """Запись ленты обмена: запрос и ответ с **независимой** подробностью (`FR-P-21`)."""
    body_request = str(record.request_body or "")
    body_response = str(record.response_body or "")
    content_type = str(record.content_type or "")
    return {
        "seq": int(record.seq),
        "at": short_time(record.started_at, "%H:%M:%S"),
        "method": str(record.method).upper(),
        "path": str(record.path),
        "query": str(record.query or ""),
        "url": str(record.url),
        "status": record.status,
        "ok": not bool(record.is_error),
        "error": str(record.error or ""),
        "duration_ms": round(float(record.duration_ms or 0.0), 1),
        "sizes": join_parts(
            _size("запрос", record.request_bytes), _size("ответ", record.response_bytes)
        ),
        "label": str(record.label or ""),
        "truncated": bool(record.body_truncated),
        "request": body_request if detail_request else "",
        "response": body_response if detail_response else "",
        "request_note": body_note(
            body=body_request,
            size=record.request_bytes,
            content_type=content_type,
            truncated=bool(record.body_truncated),
            method=str(record.method),
            bodies_logged=bodies_logged,
            limit=body_limit,
            kind="запрос",
        ),
        "response_note": body_note(
            body=body_response,
            size=record.response_bytes,
            content_type=content_type,
            truncated=bool(record.body_truncated),
            bodies_logged=bodies_logged,
            limit=body_limit,
            kind="ответ",
        ),
        "curl": curl_command(
            method=str(record.method),
            path=str(record.path),
            query=str(record.query or ""),
            body=body_request,
            base_url=base_url,
            content_type=content_type,
        ),
    }


def journal_rows(
    records: Iterable[Any],
    *,
    base_url: str = "",
    limit: int = 50,
    errors_only: bool = False,
    label: str = "",
    detail_request: bool = True,
    detail_response: bool = True,
    bodies_logged: bool = True,
    body_limit: int = 0,
) -> list[dict[str, Any]]:
    """Хвост ленты обмена: фильтры по ошибкам и метке пункта (`FR-P-28`)."""
    items = list(records)
    if errors_only:
        items = [record for record in items if bool(record.is_error)]
    if label:
        items = [record for record in items if str(record.label or "") == label]
    tail = items[-max(0, int(limit)) :] if limit else items
    return [
        journal_row(
            record,
            base_url=base_url,
            detail_request=detail_request,
            detail_response=detail_response,
            bodies_logged=bodies_logged,
            body_limit=body_limit,
        )
        for record in tail
    ]


# ---------------------------------------------------------------------------
# Протокол проверок (`SCR-401`)
# ---------------------------------------------------------------------------
def protocol_rows(session: TestSession, programme: programme_api.Programme) -> list[dict[str, Any]]:
    """Строки протокола: пункты программы плюс результаты «вне программы» (`FR-P-35`).

    Скрытых исключений нет: снятый, пропущенный и прерванный пункт видны с причиной,
    а проверка, выполненная вне программы, получает отметку `in_programme = False`,
    а не исчезает из таблицы.
    """
    states = results_api.state(session)
    rows: list[dict[str, Any]] = []
    for item in programme.items:
        state = states.get(item.check_id)
        state_row: Mapping[str, Any] = state if isinstance(state, Mapping) else {}
        rows.append(
            {
                "order": item.order,
                "check_id": item.check_id,
                "title": item.title,
                "section": item.group,
                "requirement": item.requirement,
                "check_class": class_label(item.check_class),
                "result": result_text(state_row),
                "status": str(state_row.get("status") or results_api.STATUS_NOT_RUN),
                "verdict": str(state_row.get("verdict") or ""),
                "origin": origin_label(
                    str(state_row.get("origin") or ""), suspended=bool(state_row.get("suspended"))
                ),
                "journal": _journal_range(state_row),
                "repeats": int(state_row.get("repeats") or 0),
                "reason": str(state_row.get("reason") or ""),
                "in_programme": True,
            }
        )
    known = {item.check_id for item in programme.items}
    for record in results_api.records_of(session):
        check_id = str(record.get("check_id") or "")
        if not check_id or check_id in known:
            continue
        state_row = results_api.row(session, check_id)
        spec = catalog.find(check_id)
        rows.append(
            {
                "order": 0,
                "check_id": check_id,
                "title": spec.title if spec is not None else "",
                "section": spec.module if spec is not None else "",
                "requirement": spec.requirement if spec is not None else "",
                "check_class": class_label(spec.check_class) if spec is not None else "",
                "result": result_text(state_row),
                "status": str(state_row.get("status") or ""),
                "verdict": str(state_row.get("verdict") or ""),
                "origin": origin_label(
                    str(state_row.get("origin") or ""), suspended=bool(state_row.get("suspended"))
                ),
                "journal": _journal_range(state_row),
                "repeats": int(state_row.get("repeats") or 0),
                "reason": str(state_row.get("reason") or ""),
                "in_programme": False,
            }
        )
    return rows


def _journal_range(state: Mapping[str, Any]) -> str:
    """Диапазон номеров журнала результата: «#214–#217» (пусто — обменов нет)."""
    start = state.get("journal_from")
    finish = state.get("journal_to")
    if start is None and finish is None:
        return ""
    left = "" if start is None else f"#{start}"
    right = "" if finish is None else f"#{finish}"
    return f"{left}–{right}".strip("–")


def protocol_summary(session: TestSession, programme: programme_api.Programme) -> dict[str, Any]:
    """KPI протокола: по программе и по фактическим результатам (`FR-P-34`)."""
    summary = results_api.summary(session)
    states = results_api.state(session)
    closed = 0
    for item in programme.items:
        status = str((states.get(item.check_id) or {}).get("status") or "")
        if status and status != results_api.STATUS_NOT_RUN:
            closed += 1
    return {
        "programme_size": programme.size,
        "closed": closed,
        "coverage": percent(closed, programme.size),
        "passed": summary["passed"],
        "failed": summary["failed"],
        "blocked": summary["blocked"],
        "skipped": summary["skipped"],
        "manual": summary["manual"],
        "not_run": summary["not_run"],
        "repeats": summary["repeats"],
        "suspended": summary["suspended"],
        "results_total": summary["total"],
    }


def filter_rows(
    rows: Sequence[Mapping[str, Any]], *, status: str = "", section: str = "", query: str = ""
) -> list[dict[str, Any]]:
    """Фильтры таблиц: статус, раздел и подстрока в идентификаторе или названии."""
    needle = str(query or "").strip().casefold()
    result: list[dict[str, Any]] = []
    for row in rows:
        if status and str(row.get("status") or "") != status:
            continue
        if section and str(row.get("section") or "") != section:
            continue
        if needle:
            haystack = f"{row.get('check_id') or ''} {row.get('title') or ''}".casefold()
            if needle not in haystack:
                continue
        result.append(dict(row))
    return result


def protocol_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """Протокол в CSV (разделитель «;» — выгрузки пульта для Excel, `FR-P-64`)."""
    columns = (
        ("order", "№"),
        ("check_id", "Проверка"),
        ("title", "Название"),
        ("section", "Раздел"),
        ("check_class", "Класс"),
        ("result", "Результат"),
        ("origin", "Способ"),
        ("journal", "Журнал"),
        ("repeats", "Повторов"),
        ("reason", "Причина снятия"),
        ("in_programme", "В программе"),
    )
    header = ";".join(title for _column, title in columns)
    body: list[str] = []
    for row in rows:
        cells: list[str] = []
        for column, _title in columns:
            value = row.get(column)
            if isinstance(value, bool):
                cells.append("да" if value else "нет")
            else:
                cells.append(str(value if value is not None else "").replace(";", ","))
        body.append(";".join(cells))
    return "\n".join((header, *body)) + "\n"


# ---------------------------------------------------------------------------
# Снимки стенда «до/после» (`DR-P-8`, перенос `ui/components/snapshots.py`)
# ---------------------------------------------------------------------------
COUNTER_LABELS: dict[str, str] = {
    "files": glossary.term("file"),
    "records": "Записи (RAW + markup)",
    "loads": glossary.term("load"),
    "datasets": glossary.term("dataset"),
    "models": glossary.term("model"),
    "tasks": glossary.term("task"),
}

#: Фаза снимка → подпись (макет: «Начало» / «Окончание»).
SNAPSHOT_LABELS: dict[str, str] = {
    session_api.SNAP_START: "Начало",
    session_api.SNAP_END: "Окончание",
}


def counters_rows(snapshot: Mapping[str, Any]) -> list[dict[str, str]]:
    """Счётчики реестров снимка: реестр → значение («нет данных» вместо нуля)."""
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


def delta_rows(start: Mapping[str, Any], end: Mapping[str, Any]) -> list[dict[str, str]]:
    """Разница снимков «до/после» по реестрам: сколько добавилось за сессию."""
    before = dict(start.get("counts") or {})
    after = dict(end.get("counts") or {})
    rows: list[dict[str, str]] = []
    for key, label in COUNTER_LABELS.items():
        left = before.get(key)
        right = after.get(key)
        difference = ""
        if isinstance(left, int) and isinstance(right, int):
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
    """CSV сравнения снимков для приложения к отчёту (`FR-P-47`)."""
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


def error_text(snapshot: Mapping[str, Any]) -> str:
    """Ошибки запросов внутри снимка одной строкой (пустая строка — ошибок нет)."""
    errors = dict(snapshot.get("errors") or {})
    if not errors:
        return ""
    return "Снимок частичный, ошибки запросов: " + "; ".join(
        f"{key}: {value}" for key, value in errors.items()
    )


def snapshot_view(session: TestSession) -> dict[str, Any]:
    """Снимки сессии: подписи «Начало/Окончание», счётчики и разница (`FR-P-6`, `FR-P-47`)."""
    start = dict(session.snapshots.get(session_api.SNAP_START) or {})
    end = dict(session.snapshots.get(session_api.SNAP_END) or {})
    labels = [
        {
            "phase": phase,
            "label": title,
            "at": short_time((session.snapshots.get(phase) or {}).get("at")),
            "taken": phase in session.snapshots,
        }
        for phase, title in SNAPSHOT_LABELS.items()
    ]
    return {
        "labels": labels,
        "counters": counters_rows(start or end),
        "delta": delta_rows(start, end),
        "errors": join_parts(error_text(start), error_text(end)),
        "taken": {"start": bool(start), "end": bool(end)},
    }


# ---------------------------------------------------------------------------
# Планирование: каталог, наборы, программа (`SCR-101`, `SCR-102`)
# ---------------------------------------------------------------------------
def _status_of(results: Mapping[str, Any] | None, check_id: str) -> str:
    """Статус проверки из карты состояния (`results.state`) или «не выполнена»."""
    row = (results or {}).get(check_id)
    if isinstance(row, Mapping):
        return str(row.get("status") or results_api.STATUS_NOT_RUN)
    return results_api.STATUS_NOT_RUN


def catalog_rows(
    *,
    results: Mapping[str, Any] | None = None,
    in_set: Iterable[str] = (),
    in_programme: Iterable[str] = (),
    only_class: str = "",
    only_section: str = "",
) -> list[dict[str, Any]]:
    """Строки каталога проверок: описание, класс, раздел и членство (`SCR-101`)."""
    member_set = {str(check_id) for check_id in in_set}
    member_programme = {str(check_id) for check_id in in_programme}
    rows: list[dict[str, Any]] = []
    for check_id in catalog.check_ids():
        spec = catalog.find(check_id)
        if spec is None:
            continue
        section = catalog.group_of(spec.check_id)
        if only_class and str(spec.check_class) != only_class:
            continue
        if only_section and section != only_section:
            continue
        rows.append(
            {
                "check_id": spec.check_id,
                "title": spec.title,
                "section": section,
                "module": spec.module,
                "check_class": class_label(spec.check_class),
                "class_key": str(spec.check_class),
                "requirement": spec.requirement,
                "expected": spec.expected,
                "automation": spec.automation or "",
                "scenario": scenario_label(spec.check_id),
                "blocked_by_api": spec.blocked_by_api or "",
                "confirm_required": is_confirmable(spec.check_class),
                "manual": is_manual(spec.check_class),
                "in_set": check_id in member_set,
                "in_programme": check_id in member_programme,
                "status": _status_of(results, check_id),
                "status_icon": status_icon(_status_of(results, check_id)),
            }
        )
    return rows


def sets_rows(
    library: Any,
    *,
    programme: programme_api.Programme | None = None,
    results: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Строки библиотеки наборов: состав, ревизия, статус и участие в программе."""
    selected = set(programme.selected_set_ids()) if programme is not None else set()
    in_programme = set(programme.check_ids) if programme is not None else set()
    rows: list[dict[str, Any]] = []
    for item in library.sets:
        check_ids = [str(entry.check_id) for entry in item.items]
        mandatory = sum(1 for entry in item.items if bool(entry.mandatory))
        rows.append(
            {
                "set_id": str(item.set_id),
                "title": str(item.title),
                "section": str(item.section),
                "scope": str(item.scope),
                "status": str(item.status),
                "approved": bool(item.is_approved),
                "mandatory": mandatory,
                "revision": int(item.revision or 1),
                "author": str(item.author or ""),
                "checks": len(check_ids),
                "in_programme": str(item.set_id) in selected,
                "overlap": sum(1 for check_id in check_ids if check_id in in_programme),
                "check_ids": check_ids,
                "items": _set_item_rows(item.items),
            }
        )
    return rows


def _set_item_rows(items: Iterable[Any]) -> list[dict[str, Any]]:
    """Состав набора для списка правки: порядок, идентификатор, название проверки, обязательность."""
    rows: list[dict[str, Any]] = []
    for entry in items:
        check_id = str(getattr(entry, "check_id", "") or "")
        spec = catalog.find(check_id)
        rows.append(
            {
                "order": int(getattr(entry, "order", 0) or 0),
                "check_id": check_id,
                "title": "" if spec is None else spec.title,
                "check_class": "" if spec is None else class_label(spec.check_class),
                "mandatory": bool(getattr(entry, "mandatory", False)),
                "in_catalog": spec is not None,
            }
        )
    return rows


def programme_view(
    programme: programme_api.Programme, results: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Программа сессии: состав, покрытие, предупреждения, ревизии и дельта (`SCR-102`)."""
    return {
        "revision": programme.revision,
        "approved": programme.is_approved,
        "status_label": programme.status_label,
        "size": programme.size,
        "mandatory": len(programme.mandatory_ids),
        "selected_sets": programme.selected_set_ids(),
        "coverage": programme.coverage(),
        "uncovered": programme.uncovered_sections(),
        "warnings": list(programme.warnings()),
        "rows": programme.rows(results),
        "revisions": programme.revision_rows(),
        "delta": programme.delta(),
    }


# ---------------------------------------------------------------------------
# Данные стенда и состав датасетов (`SCR-204`, UC-03/UC-04)
# ---------------------------------------------------------------------------
#: Режимы фильтра «Данных стенда» по датасету (`llm_tasks/49`, задача 1а).
FILTER_OFF = "off"
FILTER_DATASET = "dataset"
FILTER_OUTSIDE = "outside"
FILTER_MODES: tuple[str, str, str] = (FILTER_OFF, FILTER_DATASET, FILTER_OUTSIDE)

#: Значение селекта «файлы вне датасетов»: это не идентификатор датасета, а режим
#: (решение заказчика `llm_tasks/49`, вопрос 1 = б).
OUTSIDE_VALUE = "none"

#: Что пульт говорит, когда состав датасета не дал применить фильтр (замечание P1).
UNKNOWN_COMPOSITION_NOTE = "состав недоступен (P1): показаны все файлы реестра"

#: Подписи источников состава для селекта «источник» (`read_composition(mode=…)`).
SOURCE_MODE_LABELS: dict[str, str] = {
    composition_api.MODE_AUTO: "авто: сервер → задача наполнения → факт пульта → гипотеза",
    composition_api.MODE_SERVER: "только сервер (нужно устранение P1)",
    composition_api.MODE_SERVER_TASK: "только задача наполнения `dataset-fill`",
    composition_api.MODE_LOCAL: "только факт пульта (что сами наполняли)",
    composition_api.MODE_HEURISTIC: "гипотеза по реестру файлов",
}


@dataclass(frozen=True)
class RowSelection:
    """Отбор строк «Данных стенда» по составу датасета (`llm_tasks/49`, задача 1а).

    `keys` — файлы выбранного датасета (в режиме «вне датасетов» — объединение всех
    датасетов), `outside=True` инвертирует правило: остаются строки, которых в составе
    нет. `applied=False` означает «фильтр не применён» (состав недоступен или пуст):
    показать пустой список вместо реестра было бы обманом, поэтому строки остаются,
    а причина называется в блоке `filter`.
    """

    keys: composition_api.FileKeys = field(default_factory=composition_api.FileKeys)
    outside: bool = False
    applied: bool = False

    def matches_file(self, file_id: Any = "", file_name: Any = "") -> bool:
        """Показывать ли строку файла: ключ выбирает `FileKeys.matches_row`.

        `applied=False` (состав недоступен) или пустые ключи означают «фильтр не
        применён» — строку показываем; при `outside` условие переворачивается
        («файлы вне датасетов»).
        """
        found = self.keys.matches_row(file_id, file_name)
        if not self.applied or self.keys.is_empty:
            return True
        return not found if self.outside else found

    def matches_record(self, *pairs: tuple[Any, Any]) -> bool:
        """Показывать ли запись RAW + markup: достаточно совпадения raw **или** markup.

        Пары — это «идентификатор, имя» для raw и markup: правило то же, что у файлов
        (`id` решает, когда он известен и есть в составе), поэтому тёзка-файл не
        притаскивает в датасет чужую запись.
        """
        found = any(self.keys.matches_row(file_id, name) for file_id, name in pairs)
        if not self.applied or self.keys.is_empty:
            return True
        return not found if self.outside else found


#: Отбор по умолчанию: фильтр не применяется, показывается весь реестр.
NO_SELECTION = RowSelection()


def file_rows(
    files: Iterable[Any], *, selection: RowSelection | None = None
) -> list[dict[str, Any]]:
    """Строки реестра `$Файлов`: имя, тип, размер, путь и дата импорта.

    `selection` — отбор по составу датасета: строки остаются те же, меняется их состав
    (ключ выбирает `dataset_composition.FileKeys.matches_row`: `id` — когда он известен,
    имя файла — когда идентификатора у строки или у состава нет).
    """
    chosen = selection or NO_SELECTION
    rows: list[dict[str, Any]] = []
    for item in files:
        if not chosen.matches_file(getattr(item, "id", ""), getattr(item, "file_name", "")):
            continue
        size = int(getattr(item, "size", 0) or 0)
        rows.append(
            {
                "id": str(getattr(item, "id", "") or ""),
                "name": str(getattr(item, "file_name", "") or ""),
                "type": str(getattr(item, "file_type", "") or ""),
                "size": size,
                "size_text": _size("", size).strip(),
                "path": str(getattr(item, "s3_path", "") or ""),
                "imported_at": short_time(getattr(item, "import_date", None)),
            }
        )
    return rows


def load_rows(loads: Iterable[Any]) -> list[dict[str, Any]]:
    """Строки реестра `$Нагрузок` (поля берутся из ответа: схема может меняться)."""
    rows: list[dict[str, Any]] = []
    for item in loads:
        payload = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        rows.append(
            {
                "id": str(payload.get("load_id") or payload.get("id") or ""),
                "name": str(payload.get("name") or payload.get("title") or ""),
                "description": str(payload.get("description") or ""),
                "created_at": short_time(payload.get("creation_date") or payload.get("created_at")),
            }
        )
    return rows


def record_rows(overview: Any, *, selection: RowSelection | None = None) -> list[dict[str, Any]]:
    """Строки записей (RAW + markup) из объединённого обзора (`records.build_records`).

    `selection` — отбор по составу датасета: строка остаётся, если её raw или markup
    входит в состав (ключ разбирает `RowSelection.matches_record` — `id` решает, когда
    он известен). Обрезку и счётчик «показано N из M» делает `page_rows`: раньше здесь
    стоял лимит 300 **без счётчика**, и оператор считал неполный список полным.
    """
    chosen = selection or NO_SELECTION
    rows = [dict(row) for row in overview.manifest_rows()]
    if not chosen.applied:
        return rows
    return [
        row
        for row in rows
        if chosen.matches_record(
            (row.get("raw_id"), row.get("raw_name")),
            (row.get("markup_id"), row.get("markup_name")),
        )
    ]


def model_rows(models: Iterable[Any]) -> list[dict[str, Any]]:
    """Строки реестра `$Моделей`: имя, архитектура, версия, фаза, выход и обновление.

    Поля берутся из ответа (`ModelMetadata`), а не из выдуманной схемы: список нужен
    и на «Результатах» (`UC-18`), и как варианты выбора модели для проверок T6–T9.
    Дополнено признаками «Реальной тренировки» (`llm_tasks/59`): обучена ли модель,
    сколько у неё групп сигналов, последняя точность и владелец (`__TEST__`/боевая) —
    без этого «выбрать конкретную модель» было нечем.
    """
    rows: list[dict[str, Any]] = []
    for item in models:
        data = models_api.row(item)
        rows.append(
            {
                **data,
                "created_at": short_time(data.get("created_at")),
                "updated_at": short_time(data.get("updated_at")),
            }
        )
    return rows


def model_card_view(
    model: Any, *, file: Any = None, session: Any = None, files: Iterable[Any] | None = None
) -> dict[str, Any]:
    """Карточка модели для интерфейса: свойства, сигналы, классы, слои и метрики.

    `files` — реестр файлов стенда: по нему находится размер `.h5` (модель хранит только
    `file_id`). Если файл не найден, карточка честно пишет «нет в реестре файлов», а не
    показывает нули.
    """
    card = models_api.card(model, file=file, session=session)
    rows = list(files or [])
    file_row = None
    if card["file"]["id"]:
        file_row = next(
            (row for row in rows if str(getattr(row, "id", "") or "") == card["file"]["id"]),
            None,
        )
    if file_row is not None:
        card["file"]["name"] = card["file"]["name"] or str(getattr(file_row, "file_name", "") or "")
        card["file"]["size"] = card["file"]["size"] or int(getattr(file_row, "size", 0) or 0)
        card["file"]["known"] = True
    card["created_at_text"] = short_time(card["created_at"])
    card["updated_at_text"] = short_time(card["updated_at"])
    card["file"]["size_text"] = (
        f"{card['file']['size'] / 1024:.0f} КБ" if card["file"]["size"] else ""
    )
    card["signals_meta"] = join_parts(
        plural(len(card["signals"]), ("группа сигналов", "группы сигналов", "групп сигналов")),
        plural(len(card["classes"]), ("класс", "класса", "классов")) if card["classes"] else "",
        plural(len(card["layers"]), ("слой", "слоя", "слоёв")) if card["layers"] else "",
    )
    card["metrics_meta"] = join_parts(
        card["trained_label"],
        f"точность {card['metrics']['accuracy_label']}"
        if card["metrics"]["accuracy_label"]
        else "",
        card["metrics"]["origin_label"],
    )
    return card


#: Шаги «Реальной тренировки» (`llm_tasks/59`): что уже работает и что запланировано.
#: Порядок шагов — порядок работы оператора: профиль → модель → данные → обучение → ONNX.
TRAIN_STEPS: tuple[tuple[str, str, str], ...] = (
    ("profile", "Профиль прогона", "Э1"),
    ("model", "Модель", "Э1"),
    ("dataset", "Данные (датасет)", "Э2"),
    ("training", "Обучение", "Э3"),
    ("onnx", "ONNX и комплект", "Э4"),
)


def train_view(
    *,
    profile: Mapping[str, Any] | None = None,
    models: Iterable[Any] = (),
    files: Iterable[Any] = (),
    datasets: Iterable[Any] = (),
    filters: Mapping[str, Any] | None = None,
    options: Mapping[str, Any] | None = None,
    card: Mapping[str, Any] | None = None,
    session_id: str = "",
    configured: bool = True,
    note: str = "",
    uploads: Iterable[Any] = (),
    manifest: Mapping[str, Any] | None = None,
    composition: Any = None,
    steps: Iterable[Any] = (),
) -> dict[str, Any]:
    """Экран «Реальная тренировка»: профиль, отбор моделей, карточка и состояние шагов.

    Э2 (данные) отдаёт блок `data`: загруженные файлы с паспортами (`size`, `sha256`),
    датасеты стенда, состав выбранного датасета, паспорт сборки и статусы шагов. Что
    ещё не реализовано (обучение, ONNX), показано как запланированное — интерфейс не
    выглядит сломанным и не обещает того, чего нет. Строки, подписи и статусы считает
    сервер — JS только рисует (правило `llm_tasks/31.4` §3).
    """
    values = dict(profile or {})
    rows = model_rows(models)
    chosen_id = str(values.get("model_id") or "")
    dataset_id = str(values.get("dataset_id") or "")
    selected = next((row for row in rows if row["id"] == chosen_id), None)
    chosen_label = str(values.get("model_label") or (selected["name"] if selected else ""))
    upload_rows = models_api.upload_rows(uploads)
    dataset_rows = models_api.dataset_rows(datasets)
    step_states = models_api.latest_steps(steps)
    composition_row = (
        composition_api.composition_rows([composition])[0] if composition is not None else {}
    )
    # Статусы шагов данных: сначала факт из журнала шагов, иначе — вывод по тому, что
    # уже видно (загруженные файлы, выбранный датасет, состав, отметка валидации).
    dataset_status = str(values.get("dataset_label") or dataset_id)
    filled = str(composition_row.get("files") or "")
    default_data_status = {
        "files": (
            f"{len(upload_rows)} файл(ов) загружено" if upload_rows else "файлы не загружены"
        ),
        "dataset": dataset_status or "датасет не создан",
        "fill": (f"файлов в составе: {filled}" if filled else "наполнение не выполнено"),
        "validated": str(values.get("dataset_validated_at") or "") or "нет отметки оператора",
    }
    states = {
        "profile": ("готово" if values.get("object_of_test") else "заполнить"),
        "model": ("выбрана" if chosen_id else "не выбрана"),
        "dataset": (
            "готово"
            if dataset_id and values.get("dataset_validated")
            else ("выбран" if dataset_id else "не создан")
        ),
        "training": "этап Э3",
        "onnx": "этап Э4",
    }
    notes = {
        "profile": "объект, цель, ответственный и режим нагрузки",
        "model": chosen_label or "выберите модель по свойствам",
        "dataset": dataset_status or "загрузите файлы и наполните датасет",
        "training": "параметры обучения, задача FSM-1 и метрики «до/после» — этап Э3",
        "onnx": "экспорт ONNX, скачивание и комплект доказательств — этап Э4",
    }
    ready = {
        "profile": bool(values.get("object_of_test")),
        "model": bool(chosen_id),
        "dataset": bool(dataset_id and values.get("dataset_validated")),
        "training": False,
        "onnx": False,
    }
    return {
        "configured": bool(configured),
        "session_id": str(session_id or ""),
        "profile": values,
        "profile_rows": [
            {"label": "Объект испытаний", "value": str(values.get("object_of_test") or "")},
            {"label": "Цель прогона", "value": str(values.get("goal") or "")},
            {"label": "Ответственный", "value": str(values.get("responsible") or "")},
            {
                "label": "Режим полной нагрузки",
                "value": "да" if values.get("full_load") else "",
            },
            {"label": "Датасет", "value": str(values.get("dataset_label") or dataset_id)},
            {
                "label": "Датасет валидирован вне пульта",
                "value": str(values.get("dataset_validated_at") or ""),
            },
            {"label": "Модель", "value": str(values.get("model_label") or chosen_id)},
        ],
        "steps": [
            {
                "key": key,
                "title": title,
                "stage": stage,
                "status": states[key],
                "ready": ready[key],
                "note": notes[key],
            }
            for key, title, stage in TRAIN_STEPS
        ],
        "filters": dict(filters or {}),
        "options": {
            "architectures": list((options or {}).get("architectures") or []),
            "phases": list((options or {}).get("phases") or []),
            "output_sizes": list((options or {}).get("output_sizes") or []),
        },
        "flags": [{"key": key, "label": label} for key, label in models_api.FILTER_FLAGS],
        "rows": rows,
        "total": len(rows),
        "card": dict(card or {}),
        "datasets": [
            {
                "value": str(getattr(item, "id", "") or ""),
                "label": str(getattr(item, "name", "") or ""),
            }
            for item in datasets
        ],
        "file_count": len(list(files)),
        "note": str(note or ""),
        "data": {
            "uploads": upload_rows,
            "upload_count": len(upload_rows),
            "raw_count": sum(1 for item in upload_rows if item["role"].upper() == "RAW"),
            "markup_count": sum(1 for item in upload_rows if item["role"].upper() == "LOADS"),
            "size_total_text": models_api.human_size(
                sum(int(item["size"] or 0) for item in upload_rows)
            ),
            "datasets": dataset_rows,
            "dataset_id": dataset_id,
            "manifest": dict(manifest or {}),
            "manifest_rows": models_api.manifest_summary(manifest),
            "manifest_files": models_api.upload_rows(dict(manifest or {}).get("files") or []),
            "composition": composition_row,
            "roles": [{"value": key, "label": label} for key, label in models_api.FILE_ROLES],
            "steps": [
                {
                    "key": key,
                    "title": title,
                    "status": str((step_states.get(key) or {}).get("status") or "")
                    or default_data_status.get(key, ""),
                    "at": str((step_states.get(key) or {}).get("at") or ""),
                    "note": str((step_states.get(key) or {}).get("note") or ""),
                    "author": str((step_states.get(key) or {}).get("author") or ""),
                }
                for key, title in models_api.FLOW_STEPS
            ],
            "max_upload": uploads_api.MAX_UPLOAD,
            "max_upload_text": models_api.human_size(uploads_api.MAX_UPLOAD),
            "test_prefix": TEST_PREFIX,
        },
    }


#: Лимит строк в подразделах «Данных стенда»: реестр файлов бывает в сотни строк,
#: а страница «Результаты» нужна для обзора. Оператор снимает лимит кнопкой
#: «показать все» (`llm_tasks/49`, вопрос 6 = а).
DEFAULT_ROW_LIMIT = 200

#: Подразделы зоны «Данные стенда» (`SCR-401`): ключ, заголовок, ключ строк в ответе API.
DATA_SECTIONS: tuple[tuple[str, str], ...] = (
    ("files", "$Файлы"),
    ("records", "Записи RAW + markup"),
    ("loads", "$Нагрузки"),
    ("datasets", "$Датасеты и состав"),
    ("models", "$Модели"),
)


def page_rows(
    rows: Sequence[Any], *, limit: int = DEFAULT_ROW_LIMIT
) -> tuple[list[Any], dict[str, Any]]:
    """Отдаёт (строки для показа, страница со счётчиком).

    Правило честности: обрезка списка **не выглядит** как «стенд отдал 200 файлов» —
    рядом с таблицей стоит «показано 200 из 388». `limit=0` — показать всё: так
    работает кнопка «показать все», и решает это сервер, а не браузер.
    """
    all_rows = list(rows)
    total = len(all_rows)
    shown = all_rows if not limit or total <= int(limit) else all_rows[: int(limit)]
    return shown, {
        "shown": len(shown),
        "total": total,
        "limited": len(shown) < total,
        "label": f"показано {len(shown)} из {total}",
    }


def data_sections(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Подразделы «Данных стенда»: подпись со счётчиком реестра и состояние списка.

    Подписи считает Python: в свёрнутом `<details>` оператор видит число сущностей
    (`$Файлы · 388`), а не только название; при включённом фильтре по датасету подпись
    становится `$Файлы · 104 из 388` — видно и выборку, и весь реестр. `page_label`
    говорит, что список обрезан лимитом и это можно снять (`llm_tasks/49`).
    """
    counts = dict(data.get("counts") or {})
    pages = dict(data.get("pages") or {})
    sections: list[dict[str, Any]] = []
    for key, title in DATA_SECTIONS:
        page = dict(pages.get(key) or {})
        page_total = page.get("total")
        registry = counts.get(key)
        rows_shown = len(data.get(key) or [])
        if page_total is not None:
            shown_total = int(page_total)
        else:
            shown_total = int(registry) if registry is not None else rows_shown
        total = int(registry) if registry is not None else shown_total
        sections.append(
            {
                "key": key,
                "table": f"results-{key}",
                "title": title,
                "count": shown_total,
                "registry": total,
                "shown": int(page.get("shown") or rows_shown),
                "limited": bool(page.get("limited")),
                "can_show_all": bool(page.get("limited")),
                "label": (
                    f"{title} · {shown_total} из {total}"
                    if page_total is not None and total != shown_total
                    else f"{title} · {total}"
                ),
                "page_label": str(page.get("label") or ""),
            }
        )
    return sections


def composition_view(composition: Any) -> dict[str, Any]:
    """Состав датасета: источник факта, файлы, подпись достоверности и заметка."""
    if composition is None:
        return {
            "source": "",
            "source_label": "состав недоступен",
            "files": 0,
            "raw": 0,
            "markup": 0,
            "trusted": False,
            "confidence": "",
            "note": "",
            "task_id": "",
            "task_status": "",
            "counts": {},
            "rows": [],
        }
    return {
        "source": str(composition.source),
        "source_label": str(composition.source_label),
        "files": int(composition.files_count),
        "raw": len(composition.raw_ids),
        "markup": len(composition.markup_ids),
        "trusted": bool(composition.is_trusted),
        "confidence": str(composition.confidence or ""),
        "note": str(composition.note or ""),
        "task_id": str(composition.task_id or ""),
        "task_status": str(composition.task_status or ""),
        "counts": dict(composition.counts),
        "rows": composition_api.composition_rows([composition]),
    }


def dataset_rows(
    datasets: Iterable[Any], *, compositions: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Строки реестра `$Датасетов` с составом: источник факта виден рядом с числом файлов."""
    known = dict(compositions or {})
    rows: list[dict[str, Any]] = []
    for item in datasets:
        dataset_id = str(getattr(item, "id", "") or "")
        composition = known.get(dataset_id)
        rows.append(
            {
                "id": dataset_id,
                "name": str(getattr(item, "name", "") or ""),
                "type": str(getattr(item, "type", "") or ""),
                "description": str(getattr(item, "description", "") or ""),
                "created_at": short_time(getattr(item, "creation_date", None)),
                "composition": composition_view(composition) if composition is not None else None,
            }
        )
    return rows


def _attr(item: Any, name: str, default: Any = "") -> Any:
    """Поле сущности реестра: работает и с pydantic-моделью, и со словарём.

    Сервер получает модели (`DatasetResponse`), а тесты и выгрузки передают словари:
    подпись варианта не должна зависеть от того, чем представлена строка реестра.
    """
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def dataset_filter_options(
    datasets: Sequence[Any],
    compositions: Mapping[str, Any] | None = None,
    *,
    registry_files: int = 0,
) -> list[dict[str, str]]:
    """Варианты селекта «датасет» для фильтра «Данных стенда» (`llm_tasks/49`, задача 1а).

    Первый вариант — весь реестр, второй — «файлы вне датасетов» (решение заказчика,
    вопрос 1 = б), дальше — датасеты с подписью «имя · файлов N · RAW R / markup M».
    Состав в подписи появляется только у прочитанных датасетов: читать все составы
    на каждое открытие страницы нельзя (за каждым — обращение к `$Задачам` стенда).
    """
    known = dict(compositions or {})
    options = [
        {"value": "", "label": f"весь реестр ({int(registry_files)} файлов)"},
        {"value": OUTSIDE_VALUE, "label": "файлы вне датасетов"},
    ]
    for item in datasets:
        dataset_id = str(_attr(item, "id") or "")
        name = str(_attr(item, "name") or dataset_id)
        options.append(
            {"value": dataset_id, "label": composition_option_label(name, known.get(dataset_id))}
        )
    return options


def composition_option_label(name: str, composition: Any) -> str:
    """Подпись датасета для селекта: «имя · файлов N · RAW R / markup M» (`UC-19`)."""
    if composition is None:
        return join_parts(name, "состав не читался")
    return join_parts(
        name,
        f"файлов {int(composition.files_count)}",
        f"RAW {len(composition.raw_ids)} / markup {len(composition.markup_ids)}",
        "достоверно" if composition.is_trusted else "требует проверки",
    )


def dataset_filter_view(
    *,
    options: Sequence[Mapping[str, str]] = (),
    mode: str = FILTER_OFF,
    dataset_id: str = "",
    name: str = "",
    composition: Any = None,
    applied: bool = False,
    unavailable: Sequence[str] = (),
    note: str = "",
    counted: int = 0,
    registry_files: int = 0,
    shown_files: int = 0,
    shown_records: int = 0,
) -> dict[str, Any]:
    """Блок `filter` страницы «Результаты»: что отобрано, чем подтверждено, почему нет.

    Пульт обязан сказать не только «показано 104», но и **откуда** известен состав и что
    фильтр не применился: при недоступном составе (замечание P1) в интерфейсе прямо
    написано «состав недоступен (P1): показаны все файлы реестра» (`FR-P-25`).
    """
    files_count = 0 if composition is None else int(composition.files_count)
    raw = 0 if composition is None else len(composition.raw_ids)
    markup = 0 if composition is None else len(composition.markup_ids)
    resolved = str(mode) if str(mode) in FILTER_MODES else FILTER_OFF
    label = _filter_label(
        mode=resolved,
        dataset_id=dataset_id,
        name=name,
        composition=composition,
        applied=applied,
        note=note,
        registry_files=registry_files,
        unavailable=unavailable,
        counted=counted,
    )
    return {
        "mode": resolved,
        "dataset_id": str(dataset_id),
        "name": str(name),
        "source": "" if composition is None else str(composition.source),
        "source_label": "" if composition is None else str(composition.source_label),
        "trusted": bool(composition is not None and composition.is_trusted),
        "files": files_count,
        "raw": raw,
        "markup": markup,
        "registry_files": int(registry_files),
        "shown_files": int(shown_files),
        "shown_records": int(shown_records),
        "applied": bool(applied),
        "note": str(note),
        "unavailable": [str(item) for item in unavailable],
        "label": label,
        "options": [dict(item) for item in options],
        "sources": [
            {"value": value, "label": SOURCE_MODE_LABELS.get(value, value)}
            for value in composition_api.COMPOSITION_MODES
        ],
    }


def _filter_label(
    *,
    mode: str,
    dataset_id: str,
    name: str,
    composition: Any,
    applied: bool,
    note: str,
    registry_files: int,
    unavailable: Sequence[str],
    counted: int = 0,
) -> str:
    """Подпись фильтра для зоны «Данные стенда»: что показано и на каком основании."""
    if mode == FILTER_OFF:
        return f"весь реестр: файлов {int(registry_files)} (фильтр по датасету не выбран)"
    if not applied:
        default_note = (
            "составы датасетов недоступны (P1): показаны все файлы реестра"
            if mode == FILTER_OUTSIDE
            else UNKNOWN_COMPOSITION_NOTE
        )
        return join_parts("фильтр не применён", note or default_note)
    if mode == FILTER_OUTSIDE:
        return join_parts(
            "файлы вне датасетов",
            f"учтено датасетов: {int(counted)}",
            f"состав не прочитан у {len(list(unavailable))}" if unavailable else "",
            note,
        )
    trusted = bool(composition is not None and composition.is_trusted)
    return join_parts(
        f"датасет «{name or dataset_id}»",
        "" if composition is None else str(composition.source_label),
        f"файлов {int(composition.files_count)}"
        f" (RAW {len(composition.raw_ids)} / markup {len(composition.markup_ids)})",
        "достоверно" if trusted else "требует проверки",
    )


# ---------------------------------------------------------------------------
# Отчёт и реквизиты сессии
# ---------------------------------------------------------------------------
def report_points(
    stats: Mapping[str, Any], *, snapshots: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Пункты полноты отчёта (`FR-P-48`): значение, подпись и «в порядке ли пункт».

    Список собирает сервер, браузер печатает готовые строки и не решает, что считать
    готовностью (архитектура B). Пункты совпадают с проверками `report.readiness`:
    реквизиты, снимки, объём прогона, отказы, замечания, `$Задачи`, артефакты и уборка
    `__TEST__`-сущностей.
    """
    checks = dict(stats.get("checks") or {})
    notes = dict(stats.get("notes") or {})
    tasks = dict(stats.get("tasks") or {})
    artifacts = dict(stats.get("artifacts") or {})
    entities = dict(stats.get("test_entities") or {})
    session = dict(stats.get("session") or {})
    shots = dict(snapshots or {})
    taken = dict(shots.get("taken") or {})
    start = bool(taken.get("start"))
    end = bool(taken.get("end"))
    implemented = int(checks.get("implemented") or 0)
    done = int(checks.get("done") or 0)
    failed = int(checks.get("failed") or 0)
    blocked = int(checks.get("blocked") or 0)
    not_run = int(checks.get("not_run") or 0)
    notes_total = int(notes.get("total") or 0)
    p0 = int(notes.get("p0") or 0)
    p1 = int(notes.get("p1") or 0)
    incomplete = int(notes.get("incomplete") or 0)
    kinds = ", ".join(
        f"{name} {count}" for name, count in sorted((artifacts.get("kinds") or {}).items())
    )
    return [
        {
            "label": "Реквизиты сессии",
            "value": "заполнены" if session.get("filled") else "не заполнены",
            "ok": bool(session.get("filled")),
        },
        {
            "label": "Снимки стенда «Начало» / «Окончание»",
            "value": f"{'есть' if start else 'нет'} / {'есть' if end else 'нет'}",
            "ok": bool(start and end),
        },
        {
            "label": "Проверок выполнено",
            "value": f"{done} из {implemented}",
            "ok": implemented > 0 and not_run == 0,
        },
        {
            "label": "Проверок с отказом",
            "value": str(failed),
            "ok": failed == 0,
        },
        {
            "label": "Блокировано API",
            "value": str(blocked),
            "ok": True,
        },
        {
            "label": "Замечания к API",
            "value": join_parts(
                f"всего {notes_total}",
                f"P0 {p0}",
                f"P1 {p1}",
                f"без воспроизведения {incomplete}",
            ),
            "ok": p0 == 0 and incomplete == 0,
        },
        {
            "label": "Задач `$Задачи` наблюдено",
            "value": str(int(tasks.get("observed") or 0)),
            "ok": True,
        },
        {
            "label": "Артефактов в сессии",
            "value": join_parts(str(int(artifacts.get("total") or 0)), kinds),
            "ok": True,
        },
        {
            "label": "`__TEST__`-сущности",
            "value": f"создано {int(entities.get('total') or 0)} · не удалено {int(entities.get('pending') or 0)}",
            "ok": not int(entities.get("pending") or 0),
        },
    ]


def data_summary(data: Mapping[str, Any]) -> list[dict[str, str]]:
    """Сводка данных стенда: сколько чего прочитано и что не прочиталось (`UC-03`).

    Числа берутся из `counts` (сколько сущностей в реестре стенда), а не из длины
    показанного списка: списки обрезаются счётчиком «показано N из M», и KPI не должен
    выдавать 200 файлов за весь реестр.
    """
    errors = dict(data.get("errors") or {})
    counts = dict(data.get("counts") or {})

    def total(key: str) -> str:
        """Число сущностей реестра: из `counts`, иначе — длина показанного списка."""
        if key in counts:
            return str(int(counts[key] or 0))
        return str(len(data.get(key) or []))

    return [
        {"label": "файлов", "value": total("files")},
        {"label": "записей RAW+markup", "value": total("records")},
        {"label": "требуют внимания", "value": str(int(data.get("records_attention") or 0))},
        {"label": "нагрузок", "value": total("loads")},
        {"label": "датасетов", "value": total("datasets")},
        {"label": "моделей", "value": total("models")},
        {
            "label": "разделов не прочитано",
            "value": str(sum(1 for value in errors.values() if value)),
        },
    ]


def report_view(
    readiness: Mapping[str, Any], *, snapshots: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Готовность отчёта: «чего не хватает» и KPI (`FR-P-48`, `FR-P-49`)."""
    warnings = [str(item) for item in (readiness.get("warnings") or [])]
    payload = {key: value for key, value in readiness.items() if key != "warnings"}
    return {
        "ready": not warnings,
        "warnings": warnings,
        "stats": payload,
        "points": report_points(payload, snapshots=snapshots),
    }


#: Поля реквизитов сессии (`SessionInfo`): имя → подпись в форме.
SESSION_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "Наименование"),
    ("object_of_test", "Объект испытаний"),
    ("program_doc", "Программа-методика"),
    ("customer", "Заказчик"),
    ("lab", "Организация"),
    ("operator_fio", "Оператор (ФИО)"),
    ("operator_position", "Оператор (должность)"),
    ("commission", "Комиссия"),
    ("goal", "Цель"),
    ("scope", "Объём"),
    ("conditions", "Условия"),
    ("limitations", "Ограничения"),
    ("criteria", "Критерии годности"),
    ("conclusion", "Заключение"),
    ("notes", "Примечания"),
    ("signatures", "Подписи"),
)

#: Обязательные реквизиты сессии (`FR-P-3`).
SESSION_REQUIRED: tuple[str, ...] = ("title", "object_of_test")


def session_form_view(session: TestSession) -> dict[str, Any]:
    """Реквизиты сессии для формы: значения, обязательные поля и доступные действия."""
    info = session.info
    return {
        "id": session.session_id,
        "status": session.status,
        "build": session.server_build,
        "fields": [
            {
                "name": name,
                "label": label,
                "value": str(getattr(info, name, "") or ""),
                "required": name in SESSION_REQUIRED,
            }
            for name, label in SESSION_FIELDS
        ],
        "filled": bool(info.is_filled),
        "can_start": session.status == session_api.STATUS_DRAFT,
        "can_finish": session.is_open,
        "duration_seconds": session.duration_seconds,
        "snapshots": snapshot_view(session),
        "history": [
            {
                "at": short_time(entry.get("at")),
                "event": str(entry.get("event") or ""),
                "message": str(entry.get("message") or ""),
            }
            for entry in list(session.history)[-50:]
        ],
    }


# ---------------------------------------------------------------------------
# Панель «Задачи»: наблюдение за задачами стенда и фоновые прогоны (FR-T11)
# ---------------------------------------------------------------------------
def task_rows(monitor: TaskMonitor, session: TestSession) -> list[dict[str, Any]]:
    """Строки таблицы наблюдения: задача, статус, поллинг, команды FSM-1 (`FR-T11`).

    Статус берётся из записи сессии (последний опрос монитора): список задач сервера
    статуса не отдаёт, поэтому наблюдение — единственный источник факта.
    """
    rows: list[dict[str, Any]] = []
    for item in monitor.observed(session):
        rows.append(
            {
                "task_id": item.task_id,
                "short_id": item.task_id[:8],
                "task_type": item.task_type,
                "name": item.name,
                "origin": item.origin,
                "check_id": item.check_id,
                "status": item.status,
                "status_label": item.status_label,
                "icon": item.status_icon,
                "glyph": status_glyph(item.status),
                "color": status_color(item.status),
                "terminal": item.is_terminal,
                "actions": [str(name) for name in item.actions],
                "interval": item.interval,
                "active": item.active,
                "polls": item.polls,
                "last_poll": str(item.last_poll or ""),
                "first_seen": item.first_seen,
                "last_seen": item.last_seen,
                "journal_from": item.journal_from,
                "journal_seq": item.journal_seq,
                "note": item.note,
                "errors": list(item.errors),
            }
        )
    return rows


def task_list_rows(
    monitor: TaskMonitor, session: TestSession | None, tasks: Iterable[Any]
) -> list[dict[str, Any]]:
    """Строки списка задач стенда: статус — из наблюдения, иначе «нет данных».

    `GET /api/tasks/` **не отдаёт статус** (подтверждённый дефект API), поэтому
    колонка статуса честно показывает источник: «опрос» — задача наблюдается и
    опрошена, «нет данных» — статус неизвестен (`TC-TASK-02`).
    """
    index = monitor.observation_index(session)
    rows: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "")
        observation = index.get(task_id)
        status = "" if observation is None else observation.status
        rows.append(
            {
                "task_id": task_id,
                "short_id": task_id[:8],
                "task_type": str(getattr(task, "type", "") or ""),
                "name": str(getattr(task, "name", "") or ""),
                "description": str(getattr(task, "description", "") or ""),
                "created_at": str(getattr(task, "created_at", "") or ""),
                "status": status,
                "status_label": status_label(status) if status else "нет данных",
                "glyph": status_glyph(status) if status else "?",
                "source": "опрос" if status else "нет данных",
                "observed": observation is not None,
                "check_id": "" if observation is None else observation.check_id,
                "actions": []
                if observation is None
                else [str(name) for name in observation.actions],
            }
        )
    return rows


def job_rows(jobs: Iterable[Any]) -> list[dict[str, Any]]:
    """Строки панели фоновых прогонов: подпись, статус, длительность, вердикт.

    Принимает словари `pult.jobs.Job.as_dict()` или сами задания.
    """
    rows: list[dict[str, Any]] = []
    for job in jobs:
        data = job.as_dict() if hasattr(job, "as_dict") else dict(job)
        rows.append(
            {
                "job_id": str(data.get("job_id") or ""),
                "check_id": str(data.get("check_id") or ""),
                "action": str(data.get("action") or ""),
                "label": str(data.get("label") or ""),
                "status": str(data.get("status") or ""),
                "icon": str(data.get("icon") or ""),
                "active": bool(data.get("active")),
                "created_at": str(data.get("created_at") or ""),
                "duration": str(data.get("duration") or ""),
                "duration_ms": data.get("duration_ms"),
                "verdict": str(data.get("verdict") or ""),
                "error": str(data.get("error") or ""),
                "rows": list(data.get("rows") or []),
                "journal_from": data.get("journal_from"),
                "journal_to": data.get("journal_to"),
                "params": dict(data.get("params") or {}),
            }
        )
    return rows


def jobs_view(jobs: Iterable[Any], *, active: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Панель фоновых прогонов: активное задание, история и подсказка оператору."""
    rows = job_rows(jobs)
    active_row = next((row for row in rows if row["active"]), None)
    if active is not None and active_row is None:
        active_row = job_rows([active])[0] if active else None
    note = ""
    if active_row is not None:
        note = (
            f"Прогон идёт: {active_row['label'] or active_row['check_id']} "
            f"({active_row['status']}). Экран обновляется сам."
        )
    elif rows:
        note = "Активного прогона нет: последнее задание завершено."
    else:
        note = "Фоновых прогонов ещё не было."
    return {"rows": rows, "active": active_row, "note": note}


def _size_text(value: Any) -> str:
    """Размер объекта: байты с разрядами или «—», если размер неизвестен."""
    if value is None:
        return "—"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,}".replace(",", " ") + " Б"


def artifact_rows(
    rows: Iterable[Mapping[str, Any]], records: Iterable[Any]
) -> list[dict[str, Any]]:
    """Строки панели «Артефакты»: объект стенда, решение по нему и что можно сделать.

    Кандидатов собирает `pult.artifacts.candidate_rows` (данные реестров стенда),
    решения — записи `session.artifacts`. Строка показывает и то, и другое: оператор
    видит, что уже решено, а что ещё ждёт решения (`FR-T10`, `TC-CLEAN-04`).

    Оба действия остаются доступными и после решения: передумать можно (модель,
    оставленную артефактом, разрешено убрать позже), а отчёт покажет последнее
    решение — тем и закрывается `TC-CLEAN-04`.
    """
    index = artifacts_api.decision_index(records)
    display: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row.get("kind") or "")
        entity_id = str(row.get("entity_id") or "")
        decision = index.get(entity_id) or {}
        action = str(decision.get("action") or "")
        word = artifacts_api.decision_word(action)
        display.append(
            {
                "kind": kind,
                "kind_label": artifacts_api.KIND_LABELS.get(kind, kind),
                "entity_id": entity_id,
                "short_id": entity_id[:8],
                "name": str(row.get("name") or ""),
                "detail": str(row.get("detail") or ""),
                "version": str(row.get("version") or ""),
                "size": _size_text(row.get("size_bytes")),
                "ours": bool(row.get("ours")),
                "owner": "наша сессия" if row.get("ours") else "прошлый прогон",
                "created_at": str(row.get("created_at") or ""),
                "decision": word,
                "decision_label": action or "решение не принято",
                "decided": bool(action),
                "decided_at": str(decision.get("at") or ""),
                "check_id": str(decision.get("check_id") or ""),
                "note": str(decision.get("note") or ""),
                "actions": [
                    {"action": name, "label": label}
                    for name, label in artifacts_api.DECISION_LABELS.items()
                ],
            }
        )
    return display


def artifacts_view(rows: Iterable[Mapping[str, Any]], records: Iterable[Any]) -> dict[str, Any]:
    """Панель «Артефакты ресурсоёмких проверок»: строки, итог и подсказка оператору."""
    listed = [dict(row) for row in rows]
    index = artifacts_api.decision_index(records)
    return {
        "rows": artifact_rows(listed, records),
        "summary": artifacts_api.summary(listed, index),
        "note": artifacts_api.conclusion(listed, index),
    }
