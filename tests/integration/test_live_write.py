"""Интеграционные проверки живого стенда с записью (этап T3, NFR-T4).

По умолчанию такие проверки не запускаются: они **создают** файл на испытуемом
сервере. Включение — явное, отдельным флагом:

    $env:TRAINING_SERVER_BASE_URL = "https://energomera.ai-center.online"
    $env:PULT_LIVE_WRITE = "1"
    python -m pytest -m integration -k live_write

Что проверяется:

    * `TC-FILE-11/12` (негативные загрузки) и `TC-FILE-13` (круговой рейс
      `upload → download → сверка sha256 → delete`) дают вердикт, а не падение;
    * самоочистка выполнена: после прогона в реестре нет файлов с именем
      `__TEST__…` этой сессии, а в `session.counters["test_entities"]` есть
      записи «создан» и «удалён» (вход для TC-CLEAN-01/02 в T9).

Читающие проверки (`TC-SYS`, `TC-FILE-01…10`, `TC-REC`, `TC-LOAD-01…06`)
проверяются отдельно, в `test_live_read.py`; деструктивная проба `TC-LOAD-07`
(`DELETE /api/loads/{load_id}`) в автоматический прогон не включается.
"""

from __future__ import annotations

import os

import pytest

from acceptance.api import Apis, build_client
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckResult, CheckStatus
from acceptance.endpoints import TEST_PREFIX
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal
from acceptance.session import new_session
from acceptance.session import test_entities as session_entities
from client.settings import load_settings

pytestmark = pytest.mark.integration

#: Проверки, которые пишут на стенд (класс `live`): прогон только под флагом.
WRITE_CHECKS = ("TC-FILE-11", "TC-FILE-12", "TC-FILE-13")

#: Проверки, выводимые из автоматического прогона (деструктивная проба удаления нагрузки).
EXCLUDED_FROM_BULK = ("TC-LOAD-07",)


def _live_write_or_skip():
    """Настройки стенда и явное разрешение на запись, иначе пропуск теста."""
    settings = load_settings()
    if not settings.is_configured:
        pytest.skip("TRAINING_SERVER_BASE_URL не задан")
    if os.environ.get("PULT_LIVE_WRITE", "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip("PULT_LIVE_WRITE не выставлен: проверки с записью на стенд выключены")
    return settings


class LiveStand:
    """Подключение к живому стенду с общим журналом для клиента API и проб."""

    def __init__(self, settings) -> None:
        self.journal = Journal(max_records=500)
        self.client = build_client(settings, self.journal)
        self.console = build_console_client(settings, self.journal)
        self.apis = Apis.build(self.client)
        self.session = new_session(base_url=settings.base_url)

    def context(self, spec, **params) -> check_runner.AutomationContext:
        """Контекст сценария для проверки каталога (сессия, API, журнал, пробы)."""
        return check_runner.AutomationContext(
            session=self.session,
            spec=spec,
            apis=self.apis,
            journal=self.journal,
            params=params,
            probe=check_runner.RawProbe(client=self.console, journal=self.journal),
        )

    def run(self, check_id: str) -> CheckResult:
        """Прогоняет автоматический сценарий проверки и возвращает её результат."""
        spec = catalog.find(check_id)
        assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
        result = check_runner.automate(self.context(spec))
        assert result is not None, f"у проверки {check_id} нет автоматического сценария"
        return result

    def test_file_names(self) -> list[str]:
        """Имена `__TEST__`-файлов этой сессии, оставшихся в реестре (должно быть пусто)."""
        marker = f"_{self.session.session_id}"
        return [
            str(file.file_name)
            for file in self.apis.files.list_files().files
            if str(file.file_name).startswith(TEST_PREFIX) and marker in str(file.file_name)
        ]

    def cleanup(self) -> list[str]:
        """Страховочная уборка `__TEST__`-файлов сессии (если сценарий не смог удалить)."""
        marker = f"_{self.session.session_id}"
        removed: list[str] = []
        for file in self.apis.files.list_files().files:
            name = str(file.file_name)
            if name.startswith(TEST_PREFIX) and marker in name:
                self.apis.files.delete(str(file.id))
                removed.append(name)
        return removed

    def close(self) -> None:
        """Закрывает оба клиента (после закрытия запросы невозможны)."""
        self.client.close()
        self.console.close()


def test_live_write_checks_self_cleanup():
    """`TC-FILE-11/12/13` на живом стенде: вердикт, самоочистка и след в учёте сессии."""
    settings = _live_write_or_skip()
    stand = LiveStand(settings)
    results: dict[str, CheckResult] = {}
    try:
        for check_id in WRITE_CHECKS:
            results[check_id] = stand.run(check_id)

        leftovers = stand.test_file_names()
        if leftovers:
            stand.cleanup()
        entities = session_entities(stand.session)
        session_id = stand.session.session_id
    finally:
        stand.close()

    assert leftovers == [], f"после прогона остались `__TEST__`-файлы: {leftovers}"
    for check_id, result in results.items():
        assert result.status in (
            CheckStatus.PASSED,
            CheckStatus.FAILED,
            CheckStatus.SKIPPED,
        ), f"{check_id}: неожиданный статус {result.status} — {result.verdict}"

    round_trip = results["TC-FILE-13"]
    assert round_trip.status == CheckStatus.PASSED, round_trip.verdict
    assert round_trip.evidence["sha256_uploaded"] == round_trip.evidence["sha256_downloaded"]
    assert round_trip.evidence["deleted"] is True
    assert round_trip.evidence["left_in_registry"] == 0

    actions = [(item["check_id"], item["action"]) for item in entities]
    created = [check_id for check_id, action in actions if action == "создан"]
    deleted = [check_id for check_id, action in actions if action == "удалён"]
    assert created, "ни одна тестовая сущность не зарегистрирована в сессии"
    assert set(created) <= set(deleted), f"созданы без удаления: {set(created) - set(deleted)}"
    assert any(session_id in str(item.get("note")) for item in entities)


def test_live_tech_checklist_produces_verdicts():
    """Технические проверки T3 на живом стенде дают вердикт (без записи на стенд)."""
    settings = _live_write_or_skip()
    stand = LiveStand(settings)
    specs = [
        spec
        for spec in catalog.CHECKS
        if spec.check_id not in EXCLUDED_FROM_BULK and spec.check_id not in WRITE_CHECKS
    ]
    try:
        results = checks_engine.run_checks(
            stand.session, specs, context_factory=lambda spec: stand.context(spec)
        )
    finally:
        stand.close()

    assert results, "ни один автоматический сценарий T3 не выполнен"
    statuses = {result.check_id: str(result.status) for result in results}
    assert all(status != str(CheckStatus.NOT_RUN) for status in statuses.values()), statuses
    for result in results:
        assert result.verdict or result.evidence, f"{result.check_id}: нет вердикта и доказательств"
    skipped = [key for key, status in statuses.items() if status == str(CheckStatus.SKIPPED)]
    assert len(skipped) <= 3, f"слишком много пропущенных проверок на живом стенде: {skipped}"
