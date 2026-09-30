"""Живой прогон сценариев T6–T9 (ML-модели, контракт обучения, инференс, уборка).

Запуск:

    $env:TRAINING_SERVER_BASE_URL = "https://energomera.ai-center.online"
    $env:PULT_LIVE_WRITE = "1"
    python -m pytest -m integration -k live_ml

Прогон идёт только под явным флагом: `TC-TR-01` отправляет на стенд POST-пробы
(тела заведомо неверные, `dataset_id` несуществующий) и читает данные стенда —
обученная модель и RAW-файл. Ресурсоёмкие проверки (`train`/`check`/`inference` по
датасету) в прогон **не** включаются: их запускает оператор отдельным решением.

Что проверяется:

    * каждая проверка списка даёт вердикт, а не падение и не «не выполнена»;
    * каталог архитектур живого стенда получен (это факт, не зависящий от данных);
    * после прогона в реестре нет `__TEST__`-файлов этой сессии (NFR-T4).
"""

from __future__ import annotations

import os

import pytest

from acceptance.api import Apis, build_client
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckStatus
from acceptance.endpoints import TEST_PREFIX
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal
from acceptance.session import new_session
from client.settings import load_settings

pytestmark = pytest.mark.integration

WRITE_ENV = "PULT_LIVE_WRITE"

#: Проверки T6–T9, которые можно прогонять на живом стенде автоматически.
#: `TC-MOD-03` создаёт модель при дефекте сервера, тяжёлые проверки расходуют ресурсы.
LIVE_CHECKS: tuple[str, ...] = (
    "TC-MOD-01",
    "TC-MOD-05",
    "TC-MOD-06",
    "TC-TR-01",
    "TC-INF-03",
    "TC-INF-04",
    "TC-CLEAN-02",
    "TC-CLEAN-03",
)


class LiveMlStand:
    """Подключение к живому стенду с общим журналом клиента API и проб."""

    def __init__(self, settings) -> None:
        self.journal = Journal(max_records=500)
        self.client = build_client(settings, self.journal)
        self.console = build_console_client(settings, self.journal)
        self.apis = Apis.build(self.client)
        self.session = new_session(base_url=settings.base_url)

    def context(self, spec) -> check_runner.AutomationContext:
        """Контекст сценария проверки для живого стенда."""
        return check_runner.AutomationContext(
            session=self.session,
            spec=spec,
            apis=self.apis,
            journal=self.journal,
            params={},
            probe=check_runner.RawProbe(client=self.console, journal=self.journal),
        )

    def test_file_names(self) -> list[str]:
        """Имена `__TEST__`-файлов сессии, оставшихся в реестре (должно быть пусто)."""
        marker = f"_{self.session.session_id}"
        return [
            str(file.file_name)
            for file in self.apis.files.list_files().files
            if str(file.file_name).startswith(TEST_PREFIX) and marker in str(file.file_name)
        ]

    def close(self) -> None:
        self.client.close()
        self.console.close()


def _live_or_skip():
    """Настройки стенда и явное разрешение на пробы с обращением к нему."""
    settings = load_settings()
    if not settings.is_configured:
        pytest.skip("TRAINING_SERVER_BASE_URL не задан")
    if os.environ.get(WRITE_ENV, "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip(f"{WRITE_ENV} не выставлен: живой прогон ML-проверок выключен")
    return settings


def test_live_ml_checklist_produces_verdicts():
    """Сценарии T6–T9 на живом стенде дают вердикты и не оставляют `__TEST__`-файлов."""
    settings = _live_or_skip()
    stand = LiveMlStand(settings)
    specs = [spec for spec in catalog.CHECKS if spec.check_id in LIVE_CHECKS]
    assert len(specs) == len(LIVE_CHECKS)
    try:
        results = checks_engine.run_checks(
            stand.session, specs, context_factory=lambda spec: stand.context(spec)
        )
        leftovers = stand.test_file_names()
    finally:
        stand.close()

    verdicts = {result.check_id: result for result in results}
    assert set(verdicts) == set(LIVE_CHECKS)
    for check_id, result in verdicts.items():
        assert result.status != str(CheckStatus.NOT_RUN), f"{check_id}: проверка не выполнена"
        assert result.verdict or result.evidence, f"{check_id}: нет вердикта и доказательств"
        # печатается для протокола живого прогона: `python -m pytest -m integration -s`
        print(f"{check_id}: {result.status} · {result.verdict}")
    assert leftovers == [], f"после прогона остались `__TEST__`-файлы: {leftovers}"

    # каталог архитектур у живого стенда заполнен — это не зависит от данных испытаний
    catalog_result = verdicts["TC-MOD-01"]
    assert catalog_result.status == str(CheckStatus.PASSED), catalog_result.verdict
    assert catalog_result.evidence["count"] >= 1
