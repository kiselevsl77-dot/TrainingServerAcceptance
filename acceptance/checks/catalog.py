"""Каталог проверок чек-листа: группы `TC-*` и описания проверок (FR-T4).

Каталог — программное представление программы испытаний (`docs/02. Чек-лист
испытаний.md`): каждая проверка описана `CheckSpec` (модель `registry.py`) с
трассировкой на требования постановки, классом, шагами, ожидаемым результатом и
эндпоинтами испытуемого API.

Этап T4 наполняет каталог **первой группой — `TC-TASK`** (8 проверок модуля
«Task service»): монитор задач — фундамент всех асинхронных проверок, поэтому
инфраструктура чек-листа появляется сразу с рабочим сценарием. Структура каталога
рассчитана на все 10 групп (69 проверок, `PLANNED_GROUPS`) — группы T3–T10
добавляются без переделки: достаточно дописать кортеж проверок и зарегистрировать
группу в `GROUPS`.

Сверка каталога с таблицей чек-листа (`docs/02`): id, название, группа и класс —
тест `tests/unit/test_checks_catalog.py` (по образцу `test_endpoints.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from acceptance.checks.registry import CheckClass, CheckSpec


@dataclass(frozen=True)
class GroupPlan:
    """План группы проверок: полный состав каталога (10 групп, 69 проверок)."""

    key: str
    title: str
    module: str
    stage: str
    checks_total: int


#: Полный состав программы испытаний (`docs/02` §14–§15). Этап указывает, на
#: каком этапе пульта группа наполняется проверками (T3 — системы/файлы/записи/
#: нагрузки, T4 — задачи, T5 — датасеты, …).
PLANNED_GROUPS: tuple[GroupPlan, ...] = (
    GroupPlan("TC-SYS", "TC-SYS — модуль «Система»", "System", "T3", 6),
    GroupPlan("TC-FILE", "TC-FILE — модуль «File Import»", "File Import", "T3", 14),
    GroupPlan("TC-REC", "TC-REC — записи (RAW + markup)", "Записи и разметка", "T3", 6),
    GroupPlan("TC-LOAD", "TC-LOAD — модуль «Loads»", "Loads", "T3", 7),
    GroupPlan("TC-TASK", "TC-TASK — модуль «Task service»", "Task service", "T4", 8),
    GroupPlan("TC-DS", "TC-DS — модуль «Datasets»", "Datasets", "T5", 7),
    GroupPlan("TC-MOD", "TC-MOD — модуль «ML models»", "ML models", "T6", 7),
    GroupPlan("TC-TR", "TC-TR — обучение, проверка, ONNX", "ML models", "T7", 6),
    GroupPlan("TC-INF", "TC-INF — инференс", "ML models", "T8", 4),
    GroupPlan("TC-CLEAN", "TC-CLEAN — уборка и учёт ресурсов", "Прочее", "T9", 4),
)

PLANNED_CHECKS_TOTAL = sum(group.checks_total for group in PLANNED_GROUPS)


@dataclass(frozen=True)
class CheckGroup:
    """Группа проверок одного модуля (наполняется по этапам)."""

    plan: GroupPlan
    checks: tuple[CheckSpec, ...] = ()

    @property
    def key(self) -> str:
        """Ключ группы (`TC-TASK`)."""
        return self.plan.key

    @property
    def title(self) -> str:
        """Заголовок группы для интерфейса."""
        return self.plan.title

    @property
    def module(self) -> str:
        """Модуль испытуемого API, к которому относится группа."""
        return self.plan.module

    @property
    def stage(self) -> str:
        """Этап пульта, на котором группа наполняется проверками."""
        return self.plan.stage

    @property
    def is_implemented(self) -> bool:
        """True, если проверки группы уже описаны в каталоге."""
        return bool(self.checks)

    @property
    def is_complete(self) -> bool:
        """True, если в группе описан полный состав проверок по программе."""
        return len(self.checks) == self.plan.checks_total


# ---------------------------------------------------------------------------
# TC-TASK — модуль «Task service» (FR-8, UC-26…UC-28, FSM-1) — этап T4
# ---------------------------------------------------------------------------
TASK_MODULE = "Task service"

TC_TASK_SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        check_id="TC-TASK-01",
        title="Запуск диагностической задачи",
        module=TASK_MODULE,
        requirement="UC-26, FR-8",
        check_class=CheckClass.LIVE,
        steps=(
            "Экран «Задачи» → «Диагностика»: указать длительность (2 с) и подтвердить запуск",
            "POST /api/tasks/test?duration=2",
            "Убедиться, что создана задача типа celery-test и она поставлена на наблюдение",
            "Проверить, что вызов и задача зафиксированы в журнале и в сессии",
        ),
        expected="задача типа celery-test создана; факт фиксируется в журнале и в сессии",
        endpoints=("post /api/tasks/test",),
        automation="tasks.run_test_task",
    ),
    CheckSpec(
        check_id="TC-TASK-02",
        title="Список задач, фильтры и пагинация",
        module=TASK_MODULE,
        requirement="UC-27, NFR-4",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/tasks/ без фильтров: получить `tasks` и `count`",
            "Проверить фильтры `task_type` и `status` (значения из перечислений спецификации)",
            "Проверить фильтры `start_date`/`end_date`",
            "Проверить пагинацию `limit` (по умолчанию 100)/`offset`",
        ),
        expected=(
            "`tasks` + `count`; работают фильтры `task_type`/`status`/`start_date`/`end_date`, "
            "`limit` (по умолчанию 100)/`offset`"
        ),
        endpoints=("get /api/tasks/",),
        automation="tasks.list_tasks",
    ),
    CheckSpec(
        check_id="TC-TASK-03",
        title="Карточка задачи с прогонами",
        module=TASK_MODULE,
        requirement="UC-27, BR-F7",
        check_class=CheckClass.TECH,
        steps=(
            "Выбрать задачу из списка и открыть карточку",
            "GET /api/tasks/{task_id}",
            "Сверить состав `runtimes[]` с ожидаемым перечнем полей",
        ),
        expected=(
            "приходит `TaskWithRuntimes` с `runtimes[]` (`status`, `start_time`, `end_time`, "
            "`result`, `intermediate_result`, `celery_task_id`)"
        ),
        endpoints=("get /api/tasks/{task_id}",),
        automation="tasks.task_card",
    ),
    CheckSpec(
        check_id="TC-TASK-04",
        title="Переходы FSM-1 без расхода ресурсов",
        module=TASK_MODULE,
        requirement="FSM-1, UC-28, BR-F7",
        check_class=CheckClass.LIVE,
        steps=(
            "Запустить задачу `celery-test` с длительностью, достаточной для двух команд",
            "POST /api/tasks/{task_id}/pause из `running` — дождаться `paused` поллингом",
            "POST /api/tasks/{task_id}/resume из `paused` — дождаться `running` поллингом",
            "Сверить историю переходов задачи с ожидаемой цепочкой",
        ),
        expected=(
            "`running → pausing → paused → running`; каждый переход виден в журнале поллинга "
            "и в истории задачи"
        ),
        endpoints=(
            "post /api/tasks/{task_id}/pause",
            "post /api/tasks/{task_id}/resume",
            "get /api/tasks/{task_id}",
        ),
        automation="tasks.fsm_transitions",
    ),
    CheckSpec(
        check_id="TC-TASK-05",
        title="Прерывание задачи",
        module=TASK_MODULE,
        requirement="FSM-1, UC-28",
        check_class=CheckClass.LIVE,
        steps=(
            "Выбрать выполняющуюся (`running`) задачу `celery-test` на экране «Задачи»",
            "POST /api/tasks/{task_id}/interrupt",
            "Поллингом убедиться, что задача перешла в `interrupted` и наблюдение остановлено",
        ),
        expected="задача переходит в `interrupted` из `running`/`pausing`/`paused`",
        endpoints=(
            "post /api/tasks/{task_id}/interrupt",
            "get /api/tasks/{task_id}",
        ),
        automation="tasks.interrupt",
    ),
    CheckSpec(
        check_id="TC-TASK-06",
        title="Идемпотентность команд и запрет из терминального состояния",
        module=TASK_MODULE,
        requirement="BR-R3",
        check_class=CheckClass.TECH,
        steps=(
            "Повторить `pause` на уже приостановленной задаче",
            "Повторить `resume` на выполняющейся задаче",
            "Повторить `interrupt` на прерванной задаче",
            "Зафиксировать вариант поведения: 409, сообщение API или no-op без побочных эффектов",
        ),
        expected=(
            "повтор не создаёт побочных эффектов; команда из терминального состояния → "
            "`409`/no-op — вариант фиксируется"
        ),
        endpoints=(
            "post /api/tasks/{task_id}/pause",
            "post /api/tasks/{task_id}/resume",
            "post /api/tasks/{task_id}/interrupt",
        ),
        automation="tasks.idempotency",
    ),
    CheckSpec(
        check_id="TC-TASK-07",
        title="Неизвестная задача",
        module=TASK_MODULE,
        requirement="BR-R7",
        check_class=CheckClass.TECH,
        steps=(
            "Открыть карточку по несуществующему `task_id` (валидный UUID)",
            "GET /api/tasks/{несуществующий}",
            "Убедиться, что UI различает «не найдена» и ошибку запроса",
        ),
        expected="`not_found` (или 404) — UI корректно очищает карточку",
        endpoints=("get /api/tasks/{task_id}",),
        automation="tasks.unknown_task",
    ),
    CheckSpec(
        check_id="TC-TASK-08",
        title="Наблюдение за внешней задачей",
        module=TASK_MODULE,
        requirement="BR-R5",
        check_class=CheckClass.MANUAL,
        steps=(
            "Запустить задачу вне пульта (например, из основного UI или `curl`)",
            "Ввести её `task_id` на экране «Задачи» → «Внешняя задача»",
            "Убедиться, что статусы и история переходов приходят, а задача помечена «внешняя»",
        ),
        expected=(
            "оператор вводит `task_id` задачи, запущенной вне пульта, и получает её статусы; "
            "задача помечена как «внешняя»"
        ),
        endpoints=("get /api/tasks/{task_id}",),
        automation=None,
    ),
)


#: Проверки по группам: ключ группы → кортеж описаний (заполняется по этапам).
CHECKS_BY_GROUP: dict[str, tuple[CheckSpec, ...]] = {
    "TC-TASK": TC_TASK_SPECS,
}

#: Группы каталога в порядке программы испытаний (`docs/02` §14).
GROUPS: tuple[CheckGroup, ...] = tuple(
    CheckGroup(plan=plan, checks=CHECKS_BY_GROUP.get(plan.key, ())) for plan in PLANNED_GROUPS
)

#: Все описанные проверки каталога (в порядке групп).
CHECKS: tuple[CheckSpec, ...] = tuple(check for group in GROUPS for check in group.checks)

#: Проверка по её идентификатору (`TC-TASK-01`).
CHECK_INDEX: dict[str, CheckSpec] = {check.check_id: check for check in CHECKS}

#: Группа каждой описанной проверки (`TC-TASK-01` → `TC-TASK`).
CHECK_GROUP_INDEX: dict[str, str] = {
    check.check_id: group.key for group in GROUPS for check in group.checks
}


def groups(*, implemented_only: bool = False) -> tuple[CheckGroup, ...]:
    """Группы каталога (при `implemented_only` — только наполненные проверками)."""
    if implemented_only:
        return tuple(group for group in GROUPS if group.is_implemented)
    return GROUPS


def group(key: str) -> CheckGroup | None:
    """Группа каталога по ключу (`TC-TASK`)."""
    return next((item for item in GROUPS if item.key == key), None)


def by_group(key: str) -> tuple[CheckSpec, ...]:
    """Проверки группы (`TC-TASK`)."""
    found = group(key)
    return found.checks if found is not None else ()


def find(check_id: str) -> CheckSpec | None:
    """Описание проверки по идентификатору (`TC-TASK-01`)."""
    return CHECK_INDEX.get(str(check_id).strip().upper())


def group_of(check_id: str) -> str:
    """Ключ группы проверки (пустая строка, если проверки нет в каталоге)."""
    return CHECK_GROUP_INDEX.get(str(check_id).strip().upper(), "")


def check_ids() -> tuple[str, ...]:
    """Идентификаторы всех описанных проверок."""
    return tuple(check.check_id for check in CHECKS)


def stage_of(check_id: str) -> str:
    """Этап пульта, на котором выполняется проверка (`T4` для `TC-TASK-*`)."""
    found = group(group_of(check_id))
    return found.stage if found is not None else ""


def catalog_summary() -> dict[str, Any]:
    """Сводка каталога: полный состав программы и степень наполнения по этапам."""
    by_stage: dict[str, int] = {}
    for group_plan in PLANNED_GROUPS:
        by_stage[group_plan.stage] = by_stage.get(group_plan.stage, 0) + group_plan.checks_total

    return {
        "groups_total": len(PLANNED_GROUPS),
        "groups_implemented": sum(1 for item in GROUPS if item.is_implemented),
        "checks_total": PLANNED_CHECKS_TOTAL,
        "checks_implemented": len(CHECKS),
        "by_stage": by_stage,
        "classes": _class_counts(),
    }


def _class_counts() -> dict[str, int]:
    """Число описанных проверок по классам (`tech`/`live`/`heavy`/`manual`)."""
    counts: dict[str, int] = {}
    for check in CHECKS:
        counts[str(check.check_class)] = counts.get(str(check.check_class), 0) + 1
    return counts
