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
from acceptance.exchange import curl_command
from acceptance.queue import MARKER_LAST, MARKER_NEXT, MARKER_RUNNING, QUEUE_ICONS, QUEUE_STATES
from acceptance.session import TestSession

#: Разделитель подписей в «одной строке» (макет печатает ` · `).
SEPARATOR = " · "

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
    "responsible": "text",
    "artifacts": "select",
    "confirm": "checkbox",
}

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
) -> dict[str, Any]:
    """Карточка запуска как форма: поля, заготовки и что ещё не заполнено (`FR-P-19`).

    Заготовки приходят из `acceptance.run_card.defaults`, правила обязательных полей —
    из `acceptance.run_card.validate`, поэтому форма интерфейса и проверка ядра не
    расходятся. `filled` честно False, пока оператор не подтвердил расход ресурсов.
    """
    required, note = runner_api.needs_confirmation(item)
    saved = {key: value for key, value in dict(values or {}).items() if value not in ("", None)}
    merged: dict[str, Any] = dict(run_card_api.defaults(item, session_id=session_id, author=author))
    merged.update(saved)
    fields: list[dict[str, Any]] = []
    for name, label in run_card_api.FIELD_LABELS.items():
        value = merged.get(name, "")
        fields.append(
            {
                "name": name,
                "label": label,
                "kind": CARD_FIELD_KINDS.get(name, "text"),
                "value": "" if isinstance(value, bool) else str(value),
                "checked": bool(value) if name == "confirm" else False,
                "required": name in run_card_api.REQUIRED or name == "confirm",
            }
        )
    missing = run_card_api.validate(merged)
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
) -> dict[str, Any]:
    """Следующий пункт прогона: что запустится и что для этого нужно (`IR-P-20`).

    Всё, что интерфейсу нужно показать до нажатия «▶ Следующая» — название и класс
    пункта, требования (карточка запуска, ручная отметка), шаги, предпросмотр payload
    и форма отметки. Это позволяет оператору видеть последствия до вызова стенда
    (`FR-P-19`, `FR-P-20`) и не дублировать правила в JS.
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
        "steps": step_rows(item.check_id),
        "card": run_card_view(item, values=values, session_id=session_id, author=author),
        "payload": payload_view(item.check_id, session=session),
        "mark": mark_view(item),
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


def journal_row(
    record: Any,
    *,
    base_url: str = "",
    detail_request: bool = True,
    detail_response: bool = True,
) -> dict[str, Any]:
    """Запись ленты обмена: запрос и ответ с **независимой** подробностью (`FR-P-21`)."""
    body_request = str(record.request_body or "")
    body_response = str(record.response_body or "")
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
        "curl": curl_command(
            method=str(record.method),
            path=str(record.path),
            query=str(record.query or ""),
            body=body_request,
            base_url=base_url,
            content_type=str(record.content_type or ""),
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
    header = ";".join(title for _field, title in columns)
    body: list[str] = []
    for row in rows:
        cells: list[str] = []
        for field, _title in columns:
            value = row.get(field)
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
def file_rows(files: Iterable[Any]) -> list[dict[str, Any]]:
    """Строки реестра `$Файлов`: имя, тип, размер, путь и дата импорта."""
    rows: list[dict[str, Any]] = []
    for item in files:
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


def record_rows(overview: Any, *, limit: int = 300) -> list[dict[str, Any]]:
    """Строки записей (RAW + markup) из объединённого обзора (`records.build_records`)."""
    rows = [dict(row) for row in overview.manifest_rows()]
    return rows[: max(0, int(limit))] if limit else rows


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


# ---------------------------------------------------------------------------
# Отчёт и реквизиты сессии
# ---------------------------------------------------------------------------
def report_view(readiness: Mapping[str, Any]) -> dict[str, Any]:
    """Готовность отчёта: «чего не хватает» и KPI (`FR-P-48`, `FR-P-49`)."""
    warnings = [str(item) for item in (readiness.get("warnings") or [])]
    payload = {key: value for key, value in readiness.items() if key != "warnings"}
    return {"ready": not warnings, "warnings": warnings, "stats": payload}


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
