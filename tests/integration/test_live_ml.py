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

import json
import os
import urllib.request
from typing import Any

import pytest

from acceptance import session as session_api
from acceptance.api import Apis, build_client
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckStatus
from acceptance.endpoints import TEST_PREFIX
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal
from acceptance.session import new_session
from acceptance.tasks_monitor import TaskMonitor
from client.errors import ClientError
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

#: Проверки T6–T9, которые создают свои `__TEST__`-сущности и убирают их за собой.
WRITE_CHECKS: tuple[str, ...] = (
    "TC-MOD-02",
    "TC-MOD-04",
    "TC-MOD-07",
    "TC-TR-06",
    "TC-CLEAN-01",
)


class LiveMlStand:
    """Подключение к живому стенду с общим журналом клиента API и проб."""

    def __init__(self, settings) -> None:
        self.journal = Journal(max_records=500)
        self.client = build_client(settings, self.journal)
        self.console = build_console_client(settings, self.journal)
        self.apis = Apis.build(self.client)
        self.session = new_session(base_url=settings.base_url)

    def monitor(self) -> TaskMonitor:
        """Монитор задач: асинхронным сценариям нужен, иначе вести задачу нечем."""
        return TaskMonitor(self.apis.tasks, journal=self.journal, interval=0.0)

    def context(
        self,
        spec,
        *,
        params: dict[str, Any] | None = None,
        session: Any = None,
    ) -> check_runner.AutomationContext:
        """Контекст сценария проверки для живого стенда.

        `session` передаётся для второго шага цепочки (`TC-TR-04` после `TC-TR-02`):
        результат обучения ищется по записи сессии, поэтому шаги должны смотреть в
        одну и ту же сессию.
        """
        return check_runner.AutomationContext(
            session=session or self.session,
            spec=spec,
            apis=self.apis,
            journal=self.journal,
            monitor=self.monitor(),
            params=dict(params or {}),
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

    def test_entity_names(self) -> dict[str, list[str]]:
        """`__TEST__`-имена, оставшиеся в реестрах моделей и датасетов (должно быть пусто)."""
        return {
            "models": [
                str(row.name)
                for row in self.apis.models.list_models()
                if str(row.name).startswith(TEST_PREFIX)
            ],
            "datasets": [
                str(row.name)
                for row in self.apis.datasets.list_datasets().datasets
                if str(row.name).startswith(TEST_PREFIX)
            ],
        }

    def cleanup(self) -> dict[str, list[str]]:
        """Страховочная уборка `__TEST__`-сущностей сессии (если сценарий не смог)."""
        marker = f"_{self.session.session_id}"
        removed: dict[str, list[str]] = {"files": [], "models": [], "datasets": []}
        for file in self.apis.files.list_files().files:
            name = str(file.file_name)
            if name.startswith(TEST_PREFIX) and marker in name:
                self.apis.files.delete(str(file.id))
                removed["files"].append(name)
        for model in self.apis.models.list_models():
            name = str(model.name)
            if name.startswith(TEST_PREFIX) and marker in name:
                self.apis.models.delete_model(str(model.id))
                removed["models"].append(name)
        for dataset in self.apis.datasets.list_datasets().datasets:
            name = str(dataset.name)
            if name.startswith(TEST_PREFIX) and marker in name:
                self.apis.datasets.delete_dataset(str(dataset.id))
                removed["datasets"].append(name)
        return removed

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


def test_live_ml_write_checks_self_cleanup():
    """Создающие ML-проверки: вердикты, самоочистка и учёт `__TEST__`-сущностей (NFR-T4).

    Проверки создают модель (POST), загружают `.h5` источника, удаляют модель и
    убирают `__TEST__`-сущности сессии. После прогона на стенде не должно остаться
    ни одной сущности испытаний: если что-то осталось, тест чистит за собой и падает.
    """
    settings = _live_or_skip()
    stand = LiveMlStand(settings)
    specs = [spec for spec in catalog.CHECKS if spec.check_id in WRITE_CHECKS]
    assert len(specs) == len(WRITE_CHECKS)
    try:
        results = checks_engine.run_checks(
            stand.session,
            specs,
            context_factory=lambda spec: stand.context(spec),
            # создающие проверки класса `live` запускаются явно (флаг PULT_LIVE_WRITE)
            only_tech=False,
        )
        entities = session_api.test_entities(stand.session)
        pending = session_api.pending_test_entities(stand.session)
        leftovers = stand.test_entity_names()
        files = stand.test_file_names()
        if pending or leftovers["models"] or leftovers["datasets"] or files:
            stand.cleanup()
    finally:
        stand.close()

    verdicts = {result.check_id: result for result in results}
    assert set(verdicts) == set(WRITE_CHECKS)
    for check_id, result in verdicts.items():
        print(f"{check_id}: {result.status} · {result.verdict}")
        assert result.status != str(CheckStatus.NOT_RUN), f"{check_id}: проверка не выполнена"
        assert result.verdict or result.evidence, f"{check_id}: нет вердикта и доказательств"
    assert not leftovers["models"], f"на стенде остались `__TEST__`-модели: {leftovers['models']}"
    assert not leftovers["datasets"], f"остались `__TEST__`-датасеты: {leftovers['datasets']}"
    assert files == [], f"остались `__TEST__`-файлы этой сессии: {files}"
    assert pending == [], f"в учёте сессии остались незакрытые сущности: {pending}"

    # созданные сущности зарегистрированы и закрыты (учёт для TC-CLEAN-01/02)
    actions = [str(item.get("action")) for item in entities]
    assert "создан" in actions, "ни одна тестовая сущность не зарегистрирована"
    assert "удалён" in actions, "ни одна тестовая сущность не отмечена удалённой"


# ---------------------------------------------------------------------------
# Тяжёлая приёмка: реальное обучение и решение по артефакту через пульт
# ---------------------------------------------------------------------------
#: Флаг тяжёлого прогона: обучение занимает стенд минутами, поэтому включается отдельно
#: от `PULT_LIVE_WRITE` — «разрешил изменяющие проверки» ещё не значит «разрешил обучение».
HEAVY_ENV = "PULT_LIVE_HEAVY"

#: Адрес живого пульта для проверки решения по артефакту (`POST /api/artifacts`).
PULT_URL_ENV = "PULT_URL"
DEFAULT_PULT_URL = "http://127.0.0.1:8502"

#: Датасет приёмки реального обучения (решение заказчика, `llm_tasks/52`).
TRAIN_DATASET = "skfu-train"

#: Параметры контрактного обучения: эпох мало — проверяется конвейер, а не качество.
TRAIN_EPOCHS = 3
TRAIN_BATCH = 32

#: Сколько ждать исполнения задачи обучения в приёмке, секунды.
#:
#: Живой опыт 01.10.2026: стенд принимает `POST /train` (200, задача типа `training`
#: создаётся), но не исполняет её — runtime остаётся в статусе `new` (наблюдали 9 минут).
#: Поэтому приёмка ждёт ограниченно и не считает это отказом: проверка `TC-TR-02` про
#: незавершённую задачу пишет примечание, а не отказ (решение `llm_tasks/52`).
TRAINING_WAIT = 300


def _heavy_or_skip():
    """Стенд настроен, изменяющие проверки разрешены **и** разрешён тяжёлый прогон."""
    settings = _live_or_skip()
    if os.environ.get(HEAVY_ENV, "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip(f"{HEAVY_ENV} не выставлен: реальное обучение расходует ресурсы стенда")
    return settings


def _dataset_by_name(stand: LiveMlStand, name: str):
    """Датасет стенда по имени (None — не найден: приёмку запускать не на чем)."""
    for dataset in stand.apis.datasets.list_datasets().datasets:
        if name.strip().lower() in str(dataset.name).strip().lower():
            return dataset
    return None


def _dataset_for(stand: LiveMlStand, template: Any):
    """Датасет для обучения: тот же, на котором обучали образец, иначе — `skfu-train`.

    Датасет из метрик образца — самый надёжный выбор: он заведомо совместим с его
    архитектурой и признаками (на нём образец и обучался).
    """
    datasets = list(stand.apis.datasets.list_datasets().datasets)
    wanted = str((template.metrics or {}).get("dataset_id") or "")
    for dataset in datasets:
        if wanted and str(dataset.id) == wanted:
            return dataset
    chosen = _dataset_by_name(stand, TRAIN_DATASET)
    if chosen is None:
        stand.close()
        pytest.skip(f"на стенде нет датасета «{TRAIN_DATASET}»: обучать не на чем")
    return chosen


def _pult_json(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Запрос к живому пульту (`urllib`, без зависимостей): решение по артефакту."""
    base = os.environ.get(PULT_URL_ENV, DEFAULT_PULT_URL).rstrip("/")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 — адрес задаёт оператор (локальный пульт)
        f"{base}{path}", data=data, method=method
    )
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=120) as answer:  # noqa: S310
        body = json.loads(answer.read().decode("utf-8"))
    assert body.get("ok") is True, body.get("error")
    return dict(body.get("data") or {})


def test_live_ml_real_training_and_artifact_decision():
    """Приёмка тяжёлой цепочки: обучение → результат → решение по артефакту (`FR-T10`).

    Прогоняется по решению заказчика (`llm_tasks/52`): одно контрактное обучение
    (`epochs=3`, `batch_size=32`, датасет `skfu-train`) на **своей** `__TEST__`-модели,
    затем `TC-TR-04` с решением «оставить артефактом», затем решение оператора в
    живом пульте (`GET/POST /api/artifacts`) — модель уходит со стенда, решение
    переписано. Стенд остаётся чистым: в конце проверяется отсутствие следов сессии.
    """
    settings = _heavy_or_skip()
    stand = LiveMlStand(settings)
    template = next((row for row in stand.apis.models.list_models() if row.metrics), None)
    if template is None:
        stand.close()
        pytest.skip("на стенде нет обученной модели-образца: неизвестны `signals` для обучения")
    card = stand.apis.models.get_model(str(template.id))
    # Свою модель для приёмки собираем по образцу боевой: стенд требует, чтобы число
    # групп сигналов совпадало с `output_size` («Number of signal groups (0) does not
    # match output_size (2)»), а `device_id` должны существовать на стенде. В карточке
    # сигналы — плоский список (`signal_num`, `device_id`), а на создание модели
    # передаётся словарь «группа → устройства» (`client.models.create_model`).
    groups: dict[str, list[str]] = {}
    for item in card.signals or []:
        groups.setdefault(str(item.get("signal_num", 0)), []).append(
            str(item.get("device_id") or "")
        )
    model = stand.apis.models.create_model(
        architecture=str(card.architecture),
        name=f"{TEST_PREFIX}train_base_{stand.session.session_id}",
        output_size=int(card.output_size or 2),
        phase_type=str(card.phase_type or "single"),
        signals=groups or None,
        signal_aliases=dict(card.signal_aliases or {}) or None,
        description=f"база для приёмки реального обучения (образец: {template.name})",
    )
    print(
        f"образец для обучения: {template.name} · {card.architecture} · "
        f"{card.output_size} групп ({', '.join(sorted(groups))})"
    )
    dataset = _dataset_for(stand, template)
    print(f"датасет обучения: {dataset.name} ({dataset.id})")

    trained_name = f"{TEST_PREFIX}TC_TR_02_{stand.session.session_id}_trained"
    params = {
        "model_id": str(model.id),
        "dataset_id": str(dataset.id),
        "epochs": str(TRAIN_EPOCHS),
        "batch_size": str(TRAIN_BATCH),
        "trained_model_name": trained_name,
        "timeout": str(TRAINING_WAIT),
    }
    specs = {spec.check_id: spec for spec in catalog.CHECKS}
    start_result = None
    result_result = None
    try:
        start_result = check_runner.automate(stand.context(specs["TC-TR-02"], params=params))
        assert start_result is not None
        print(f"TC-TR-02: {start_result.status} · {start_result.verdict}")
        print("FSM-1:", json.dumps(start_result.evidence.get("watch"), ensure_ascii=False))

        trained = [row for row in stand.apis.models.list_models() if str(row.name) == trained_name]
        if trained:
            result_result = check_runner.automate(
                stand.context(
                    specs["TC-TR-04"],
                    params={**params, "keep_artifact": "да"},
                    session=start_result.session,
                )
            )
            assert result_result is not None
            print(f"TC-TR-04: {result_result.status} · {result_result.verdict}")
            print(
                "решение сценария:",
                json.dumps(result_result.evidence.get("artifact"), ensure_ascii=False),
            )
        trained_id = str(trained[0].id) if trained else ""
    finally:
        # базовая модель — тоже наша `__TEST__`-сущность: без уборки она осталась бы
        # на общем стенде (NFR-T4); модель-результат намеренно оставлена артефактом —
        # её убирает решение оператора в пульте (следующий тест)
        try:
            stand.apis.models.delete_model(str(model.id))
            print(f"уборка: базовая модель {model.name} удалена")
        except ClientError as exc:  # стенд недоступен — об этом честно сообщаем
            print(f"уборка не выполнена: {exc}")
        stand.close()

    assert start_result is not None, "сценарий TC-TR-02 не выполнен"
    response = dict(start_result.evidence.get("response") or {})
    print("ответ стенда на обучение:", json.dumps(response, ensure_ascii=False))
    assert response.get("task_id"), "стенд не вернул `task_id`: обучение не запущено"

    watch = dict(start_result.evidence.get("watch") or {})
    print("FSM-1:", json.dumps(watch, ensure_ascii=False))
    if not watch.get("terminal"):
        pytest.skip(
            "обучение принято стендом, но задача не исполняется (статус "
            f"«{watch.get('final_label') or watch.get('status') or 'new'}»): "
            "результата обучения нет, приёмка закрывается примечанием TC-TR-02"
        )

    assert start_result.evidence["trained_model"]["name"] == trained_name
    print(
        "время обучения:",
        watch.get("duration") or "стенд не отдал длительность",
    )
    assert result_result is not None and trained_id, "модель-результат найдена и проверена TC-TR-04"
    assert result_result.evidence["artifact"]["action"] == "оставлена артефактом"


def test_live_pult_artifact_decision_removes_model():
    """Решение оператора в пульте: «💾 в артефакты», затем «🗑 удалить» (`FR-T10`).

    Живой срез поверх пульта: пульт заводит свой `__TEST__`-объект (модель без `signals`
    — как в `TC-MOD-02`), панель артефактов показывает его как кандидата без решения,
    оператор сохраняет его артефактом, затем передумывает и удаляет. Решения пишутся в
    **отдельную** сессию приёмки — сессия оператора в пульте не трогается (возвращается
    выбранной в конце), а стенд остаётся чистым.
    """
    settings = _live_or_skip()
    if not _pult_available():
        pytest.skip(f"пульт не отвечает по {DEFAULT_PULT_URL}: живой срез невозможен")

    stand = LiveMlStand(settings)
    name = f"{TEST_PREFIX}artifact_demo_{stand.session.session_id}"
    model = stand.apis.models.create_model(
        architecture="cnn_1d",
        name=name,
        output_size=2,
        phase_type="single",
        description="живая приёмка решения по артефакту",
    )
    print(f"объект приёмки: {name} ({model.id})")
    previous = str(_pult_json("GET", "/api/sessions").get("current") or "")
    acceptance = _pult_json(
        "POST",
        "/api/sessions",
        {
            "info": {
                "title": "Приёмка артефактов (TC-CLEAN-04)",
                "object_of_test": "Стенд ML",
                "operator_fio": "автотест",
            }
        },
    )
    try:
        candidates = [
            row
            for row in _pult_json("GET", "/api/artifacts")["view"]["rows"]
            if str(row["entity_id"]) == str(model.id)
        ]
        print("кандидат в панели:", json.dumps(candidates, ensure_ascii=False))
        assert candidates, "панель артефактов не видит созданный объект"
        assert candidates[0]["decision"] == "", "решение ещё не принято"
        payload = {"kind": "model", "id": str(model.id), "check_id": "TC-TR-04"}

        kept = _pult_json("POST", "/api/artifacts", {"action": "keep", **payload})
        print("решение «в артефакты»:", json.dumps(kept["artifact"], ensure_ascii=False))
        assert kept["artifact"]["action"] == session_api.ARTIFACT_KEPT
        assert "остаётся на стенде" in kept["artifact"]["note"]
        assert kept["summary"]["kept"] == 1, "панель учла сохранённый артефакт"

        deleted = _pult_json("POST", "/api/artifacts", {"action": "delete", **payload})
        print("решение «удалить»:", json.dumps(deleted["artifact"], ensure_ascii=False))
        print("итог панели:", deleted["conclusion"])
        assert deleted["artifact"]["action"] == session_api.ARTIFACT_DELETED
        assert deleted["artifact"]["name"] == name
        assert "решения зафиксированы" in deleted["conclusion"], deleted["conclusion"]
        assert deleted["summary"]["deleted"] == 1
    finally:
        if previous:
            _pult_json("POST", "/api/sessions", {"action": "select", "session_id": previous})
        left = [str(row.name) for row in stand.apis.models.list_models() if str(row.name) == name]
        if left:  # страховка: решение не прошло — убираем за собой вручную
            stand.apis.models.delete_model(str(model.id))
        stand.close()
    assert acceptance["id"], "сессия приёмки создана"


def _pult_available() -> bool:
    """Отвечает ли живой пульт: без него срез «панель артефактов» не проверить."""
    base = os.environ.get(PULT_URL_ENV, DEFAULT_PULT_URL).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/state", timeout=10) as answer:  # noqa: S310
            return answer.status == 200
    except Exception:  # noqa: BLE001 — недоступный пульт это «пропустить», а не «упасть»
        return False
