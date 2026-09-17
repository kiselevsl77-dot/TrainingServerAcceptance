"""Общий прогон сценариев проверок: контекст, реестр сценариев, обвязка движка (FR-T4).

Модуль выделен на этапе T3, когда сценариев стало много: группы `TC-SYS`, `TC-FILE`,
`TC-REC`, `TC-LOAD` описаны каждая в своём модуле (`system.py`, `files.py`,
`records.py`, `loads.py`), а `tasks.py` остаётся владельцем сценариев `TC-TASK`.

Здесь живёт то, что **не зависит** от модуля испытуемого API:

    * `CheckOutcome` — итог сценария (статус, вердикт, доказательства);
    * `PreconditionError` — проверку нельзя выполнить (нет `task_id`, нет данных);
      это «пропущена», а не «отказ» проверки;
    * `AutomationContext` — что нужно сценарию: сессия, описание проверки, API
      модулей (`apis`), монитор задач, журнал, параметры запуска и «сырой» клиент
      консоли для негативных проб (`RawProbe`);
    * `evaluate` / `automate` — прогон сценария напрямую или через движок
      (`acceptance.checks.engine`) с меткой `TC-…` и записью результата в сессию.

Реестр сценариев собирается из модулей-владельцев лениво (`automations()`), поэтому
циклических импортов нет: `runner` не импортирует домены на уровне модуля, а домены
импортируют `runner`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import ValidationError

from acceptance.checks.engine import record_result as save_result
from acceptance.checks.engine import start_check
from acceptance.checks.registry import CheckResult, CheckSpec, CheckStatus
from acceptance.config import DEFAULT_BODY_LIMIT
from acceptance.exchange import ExchangeResult, MultipartPayload, execute_request
from acceptance.http_log import Journal
from acceptance.session import TestSession
from acceptance.tasks_monitor import TaskMonitor
from client.errors import ApiError, ClientError
from client.tasks import TasksApi

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from acceptance.api import Apis
    from client.files import FilesApi
    from client.loads import LoadsApi
    from client.system import SystemApi

#: Модули-владельцы автоматических сценариев (ленивый импорт — см. `automations`).
AUTOMATION_DOMAINS: tuple[str, ...] = (
    "acceptance.checks.system",
    "acceptance.checks.files",
    "acceptance.checks.records",
    "acceptance.checks.loads",
    "acceptance.checks.tasks",
)


@dataclass
class CheckOutcome:
    """Итог автоматического сценария: статус, вердикт, доказательства."""

    status: CheckStatus
    verdict: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def is_passed(self) -> bool:
        """True, если проверка пройдена."""
        return self.status == CheckStatus.PASSED


class PreconditionError(Exception):
    """Проверку невозможно выполнить: не задано предусловие (нет задачи, нет данных).

    Отличается от «отказа» проверки: оператору нужно выбрать задачу или данные, а не
    разбираться с дефектом API. Такие проверки получают статус «пропущена».
    """


@dataclass
class RawProbe:
    """«Сырой» клиент консоли для негативных проб (статус, заголовки, тело ответа).

    Проверки `TC-SYS-03/05`, `TC-FILE-14`, `TC-LOAD-07` подтверждают поведение
    маршрутов, которых нет в реестре операций (`GET /api/__test_unknown__`,
    `DELETE /api/loads/{load_id}`) либо которые отвечают статусом ошибки. Обычный
    клиент API в таких случаях бросает исключение и теряет статус, поэтому пробы
    идут `httpx.Client` консоли и `execute_request` — обмен попадает в тот же журнал.
    """

    client: httpx.Client
    journal: Journal | None = None
    body_limit: int = DEFAULT_BODY_LIMIT
    timeout: float | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        label: str = "",
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        multipart: MultipartPayload | None = None,
        binary: bool = False,
        keep_content: bool = False,
    ) -> ExchangeResult:
        """Выполняет пробу и возвращает результат обмена (исключений не бросает)."""
        return execute_request(
            self.client,
            method=method,
            path=path,
            query=query,
            json_body=json_body,
            multipart=multipart,
            label=label,
            binary=binary,
            journal=self.journal,
            body_limit=self.body_limit,
            timeout=self.timeout,
            keep_content=keep_content,
        )


@dataclass
class AutomationContext:
    """Что нужно сценарию: сессия, описание проверки, API модулей, монитор, параметры."""

    session: TestSession
    spec: CheckSpec
    tasks: TasksApi | None = None
    journal: Journal | None = None
    monitor: TaskMonitor | None = None
    params: dict[str, Any] = field(default_factory=dict)
    #: Сервисы испытуемого API (этап T3: сценариям нужны файлы, нагрузки, система).
    apis: Apis | None = None
    #: Клиент негативных проб (`RawProbe`); None — пробы в сценарии недоступны.
    probe: RawProbe | None = None

    def __post_init__(self) -> None:
        """Подхватывает API задач из агрегата сервисов, если он передан не отдельно.

        Сценарии `TC-TASK` работают с атрибутом `tasks` (так же их собирают тесты),
        а новым сценариям (`TC-SYS`/`TC-FILE`/`TC-REC`/`TC-LOAD`) достаточно передать
        `apis` — контекст сам вытащит из него API задач.
        """
        if self.tasks is None and self.apis is not None:
            self.tasks = self.apis.tasks

    # -- параметры -----------------------------------------------------------
    @property
    def task_id(self) -> str:
        """Задача, с которой работает сценарий (`task_id` из параметров)."""
        return str(self.params.get("task_id") or "").strip()

    @property
    def action(self) -> str:
        """Команда FSM-1 для сценария идемпотентности (`pause` по умолчанию)."""
        return str(self.params.get("action") or "pause").strip().lower()

    def param(self, name: str, default: str = "") -> str:
        """Строковый параметр запуска (значение по умолчанию, если не задан)."""
        return str(self.params.get(name) or default).strip()

    def require_param(self, name: str, hint: str) -> str:
        """Параметр запуска, без которого проверка не выполняется.

        Raises:
            PreconditionError: параметр не задан — оператору нужно выбрать данные.
        """
        value = self.param(name)
        if not value:
            raise PreconditionError(f"не задан {name}: {hint}")
        return value

    def require_task(self) -> str:
        """`task_id` для сценария.

        Raises:
            PreconditionError: если задача не задана — оператор не выбрал её в карточке.
        """
        if not self.task_id:
            raise PreconditionError(
                "не выбран task_id: откройте карточку задачи на экране «Задачи»"
            )
        return self.task_id

    def require_monitor(self) -> TaskMonitor:
        """Монитор для сценария.

        Raises:
            PreconditionError: если монитор недоступен (стенд не настроен).
        """
        if self.monitor is None:
            raise PreconditionError("монитор задач недоступен: не задан адрес испытуемого сервера")
        return self.monitor

    # -- сервисы API ---------------------------------------------------------
    def tasks_api(self) -> TasksApi:
        """API задач (`client.tasks.TasksApi`).

        Raises:
            PreconditionError: адрес стенда не задан — сервис недоступен.
        """
        api = self.tasks or (self.apis.tasks if self.apis is not None else None)
        if api is None:
            raise PreconditionError("API задач недоступен: не задан адрес испытуемого сервера")
        return api

    def files_api(self) -> FilesApi:
        """API файлов (`client.files.FilesApi`); «пропущена», если стенд не настроен."""
        if self.apis is None:
            raise PreconditionError("API файлов недоступен: не задан адрес испытуемого сервера")
        return self.apis.files

    def loads_api(self) -> LoadsApi:
        """API нагрузок (`client.loads.LoadsApi`); «пропущена», если стенд не настроен."""
        if self.apis is None:
            raise PreconditionError("API нагрузок недоступен: не задан адрес испытуемого сервера")
        return self.apis.loads

    def system_api(self) -> SystemApi:
        """API модуля «Система» (`client.system.SystemApi`)."""
        if self.apis is None:
            raise PreconditionError("API системы недоступен: не задан адрес испытуемого сервера")
        return self.apis.system

    def probe_request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        multipart: MultipartPayload | None = None,
        binary: bool = False,
    ) -> ExchangeResult:
        """Негативная проба «сырым» клиентом консоли под меткой текущей проверки.

        Raises:
            PreconditionError: клиент проб недоступен (проверка запускается вне UI).
        """
        if self.probe is None:
            raise PreconditionError(
                "нет клиента консоли для негативных проб — проверку запускайте из пульта"
            )
        return self.probe.request(
            method,
            path,
            label=self.spec.check_id,
            query=query,
            json_body=json_body,
            multipart=multipart,
            binary=binary,
        )


# ---------------------------------------------------------------------------
# Реестр сценариев (собирается из модулей-владельцев)
# ---------------------------------------------------------------------------
def automations() -> dict[str, Callable[[AutomationContext], CheckOutcome]]:
    """Все автоматические сценарии пульта: ключ — `CheckSpec.automation`.

    Модули-владельцы (`AUTOMATION_DOMAINS`) импортируются лениво: так `runner` не
    зависит от них на уровне модуля, а сценарии могут пользоваться его типами.
    """
    from importlib import import_module

    combined: dict[str, Callable[[AutomationContext], CheckOutcome]] = {}
    for module_name in AUTOMATION_DOMAINS:
        module = import_module(module_name)
        domain = getattr(module, "AUTOMATIONS", None)
        if isinstance(domain, dict):
            combined.update(domain)
    return combined


def automation_names() -> tuple[str, ...]:
    """Ключи всех объявленных сценариев (для тестов и подсказок в интерфейсе)."""
    return tuple(automations())


def scenario(spec: CheckSpec) -> Callable[[AutomationContext], CheckOutcome] | None:
    """Сценарий проверки по её описанию (None — если сценарий не автоматизирован)."""
    return automations().get(str(spec.automation or ""))


def evaluate(context: AutomationContext, *, automation: str = "") -> CheckOutcome | None:
    """Выполняет сценарий проверки и возвращает её итог (None — если сценария нет).

    Args:
        context: сессия, описание проверки, API, монитор и параметры.
        automation: явный ключ сценария — для ручных проверок, где оператор
            подтверждает факт (`tasks.external_observation` для TC-TASK-08).

    Raises:
        Exception: ошибки программирования (опечатки в сценарии) не подавляются;
            отказы API, обрыв связи и отклонения схемы превращаются в вердикт
            «отказ» — проверка должна завершаться фактом, а не падением пульта.
    """
    function = automations().get(automation or str(context.spec.automation or ""))
    if function is None:
        return None
    try:
        return function(context)
    except PreconditionError as exc:
        return CheckOutcome(CheckStatus.SKIPPED, str(exc), {})
    except (ApiError, ClientError, httpx.HTTPError, ValidationError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"сценарий не выполнен ({type(exc).__name__}): {exc}",
            {"error": str(exc)},
        )


def automate(context: AutomationContext) -> CheckResult | None:
    """Прогоняет автоматический сценарий через движок и сохраняет результат в сессии.

    Returns:
        Результат проверки или None, если у проверки нет автоматического сценария
        (ручные проверки отмечает оператор через `engine.mark`).
    """
    if scenario(context.spec) is None:
        return None

    with start_check(context.spec, journal=context.journal, params=context.params) as run:
        outcome = evaluate(context) or CheckOutcome(
            CheckStatus.SKIPPED, "сценарий проверки не определён", {}
        )
        result = run.finish(
            outcome.status,
            verdict=outcome.verdict,
            operator_note=outcome.note,
            evidence=outcome.evidence,
        )
    save_result(context.session, result)
    return result
