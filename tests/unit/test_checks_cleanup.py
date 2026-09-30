"""Тесты сценариев группы `TC-CLEAN` (этап T9): перечень `__TEST__` и снимки стенда.

Проверки ничего не создают: источник факта — сессия испытаний (`session.counters`,
`session.snapshots`). Тесты собирают сессию вручную и проверяют, что сценарий честно
отличает «всё объяснено» от «есть расхождения», а расхождения идут **примечанием**
(замечание отчёта), как требует чек-лист `FR-T1`/`BR-R6`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from acceptance import session as session_api
from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import cleanup as check_cleanup
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckSpec, CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
Handler = Callable[[httpx.Request], httpx.Response]

FILE_ID = "11111111-1111-4111-8111-111111111111"
DATASET_ID = "22222222-2222-4222-8222-222222222222"
MODEL_ID = "3d2d1139-0000-4000-8000-000000000001"


def spec_of(check_id: str) -> CheckSpec:
    """Описание проверки из каталога (в тестах оно всегда существует)."""
    spec = catalog.find(check_id)
    assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
    return spec


class FakeRegistry:
    """Подменённый стенд: реестры файлов, датасетов и моделей с удалением (TC-CLEAN-01)."""

    def __init__(
        self,
        *,
        files: list[dict[str, Any]] | None = None,
        datasets: list[dict[str, Any]] | None = None,
        models: list[dict[str, Any]] | None = None,
        delete_file_status: int = 200,
        delete_dataset_status: int = 200,
        delete_model_status: int = 200,
    ) -> None:
        self.files = list(files or [])
        self.datasets = list(datasets or [])
        self.models = list(models or [])
        self.delete_file_status = delete_file_status
        self.delete_dataset_status = delete_dataset_status
        self.delete_model_status = delete_model_status
        self.deleted: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого стенда: реестры и удаление сущностей."""
        path = request.url.path
        if path == "/api/data/files":
            return httpx.Response(200, json={"files": self.files, "count": len(self.files)})
        if path == "/api/datasets/":
            return httpx.Response(
                200, json={"datasets": self.datasets, "count": len(self.datasets)}
            )
        if path == "/api/ml_models/models":
            return httpx.Response(200, json={"models": self.models, "count": len(self.models)})
        if request.method != "DELETE":
            return httpx.Response(404, json={"detail": "Not Found"})
        if path.startswith("/api/ml_models/models/"):
            return self._remove(
                self.models, path.rsplit("/", 1)[-1], "модель", self.delete_model_status
            )
        if path.startswith("/api/datasets/"):
            return self._remove(
                self.datasets, path.rsplit("/", 1)[-1], "датасет", self.delete_dataset_status
            )
        return self._remove(self.files, path.rsplit("/", 1)[-1], "файл", self.delete_file_status)

    def _remove(
        self, rows: list[dict[str, Any]], entity_id: str, kind: str, status: int
    ) -> httpx.Response:
        """Удаляет запись реестра либо отвечает ошибкой (негативный вариант)."""
        if status != 200:
            return httpx.Response(status, json={"detail": "Не удалось удалить"})
        rows[:] = [row for row in rows if str(row["id"]) != entity_id]
        self.deleted.append((kind, entity_id))
        return httpx.Response(200, json={"message": f"{kind} удалён"})


def file_row(file_id: str, name: str, *, size: int = 1024) -> dict[str, Any]:
    """Запись реестра файлов (`FileMetadataResponse`)."""
    return {
        "id": file_id,
        "file_name": name,
        "size": size,
        "s3_path": f"s3://bucket/{name}",
        "import_date": "2026-09-30T12:00:00",
        "file_type": "RAW",
    }


def dataset_row(dataset_id: str, name: str) -> dict[str, Any]:
    """Запись реестра датасетов (`DatasetResponse`)."""
    return {
        "id": dataset_id,
        "creation_date": "2026-09-30T12:00:00",
        "name": name,
        "description": None,
        "type": "direct_fill",
    }


def model_row(model_id: str, name: str) -> dict[str, Any]:
    """Запись реестра моделей (`ModelMetadata`)."""
    return {
        "id": model_id,
        "file_id": FILE_ID,
        "architecture": "stft_mlp",
        "version": "stft_mlp.9bbb964f",
        "name": name,
        "description": None,
        "phase_type": "single",
        "output_size": 2,
        "config": {"architecture": "stft_mlp"},
        "metrics": None,
        "pruning_mask": None,
        "created_at": "2026-09-30T12:00:00",
        "updated_at": "2026-09-30T12:00:00",
        "signals": [],
        "signal_aliases": {},
    }


def _not_found(request: httpx.Request) -> httpx.Response:  # pragma: no cover - не вызывается
    """Проверки, у которых источник факта — сессия: на стенд они не ходят."""
    return httpx.Response(500, json={"detail": "сценарий не должен обращаться к стенду"})


def stand(
    check_id: str,
    session: Any = None,
    *,
    handler: Handler | None = None,
    **params: Any,
) -> Any:
    """Контекст сценария с заранее собранной сессией (без реальной сети)."""
    journal = Journal(max_records=200)
    transport = httpx.MockTransport(handler or _not_found)
    client = ApiHttpClient(
        base_url=BASE_URL, timeout=5.0, transport=LoggingTransport(journal, inner=transport)
    )
    settings = TrainingServerSettings(base_url=BASE_URL, timeout=5.0)
    console = build_console_client(settings, journal, inner=transport)
    return check_runner.AutomationContext(
        session=session or new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=Apis.build(client),
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
    )


def entity(session: Any, *, entity_id: str, entity_type: str, action: str, check_id: str) -> None:
    """Регистрирует `__TEST__`-сущность сессии тем же способом, что и проверки."""
    session_api.add_test_entity(
        session,
        entity_id=entity_id,
        entity_type=entity_type,
        action=action,
        check_id=check_id,
        note=f"создан проверкой {check_id}",
    )


def snapshot(
    *, files: int, models: int, datasets: int, tasks: int, version: str = "dev@abc"
) -> dict:
    """Снимок стенда фазы: счётчики реестров и версия сервера."""
    return {
        "at": "2026-09-30T12:00:00",
        "version": {"branch": version.split("@")[0], "revision": version.split("@")[-1]},
        "counts": {"files": files, "models": models, "datasets": datasets, "tasks": tasks},
        "files": {},
        "records": {},
        "errors": {},
    }


# ---------------------------------------------------------------------------
# TC-CLEAN-02 — перечень созданного и удалённого
# ---------------------------------------------------------------------------
def test_entities_pass_with_created_and_deleted():
    """Перечень полон: записи с id/типом/действием/временем, не удалось — примечание."""
    session = new_session(base_url=BASE_URL)
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="создан", check_id="TC-FILE-13"
    )
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="удалён", check_id="TC-FILE-13"
    )
    entity(session, entity_id=MODEL_ID, entity_type="модель", action="создан", check_id="TC-MOD-02")

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["total"] == 3
    assert outcome.evidence["created"] == 2
    assert outcome.evidence["deleted"] == 1
    assert outcome.evidence["pending"] == 1
    assert outcome.evidence["pending_ids"] == [MODEL_ID]
    assert outcome.evidence["by_type"] == {"файл RAW": 2, "модель": 1}
    assert outcome.evidence["checks"] == ["TC-FILE-13", "TC-MOD-02"]
    assert "не удалено 1" in outcome.note


def test_entities_skipped_without_entities():
    """Сессия без `__TEST__`-сущностей — «пропущена»: учитывать нечего."""
    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02"))

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет `__TEST__`-сущностей" in outcome.verdict


def test_entities_fail_on_incomplete_record():
    """Запись без времени — отказ: в отчёте должно быть видно, когда создано."""
    session = new_session(base_url=BASE_URL)
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="создан", check_id="TC-FILE-13"
    )
    session.counters["test_entities"][0].pop("at")

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02", session))

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "неполные записи" in outcome.verdict


def test_entities_fail_on_unknown_action():
    """Действие вне «создан/удалён» — отказ: перечень перестаёт быть однозначным."""
    session = new_session(base_url=BASE_URL)
    session.counters["test_entities"] = [
        {
            "id": FILE_ID,
            "type": "файл RAW",
            "action": "создано",
            "at": "2026-09-30T12:00:00",
            "check_id": "TC-FILE-13",
            "note": "",
        }
    ]

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02", session))

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "неизвестные действия" in outcome.verdict


def test_entities_note_for_unknown_type():
    """Тип без соответствия счётчику снимка — примечание, а не отказ."""
    session = new_session(base_url=BASE_URL)
    entity(session, entity_id=FILE_ID, entity_type="запись", action="создан", check_id="TC-REC-01")

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-02", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "типы без соответствия" in outcome.note


# ---------------------------------------------------------------------------
# TC-CLEAN-03 — сверка снимков стенда
# ---------------------------------------------------------------------------
def test_snapshots_explain_all_deltas():
    """Дельта каждого счётчика объясняется сущностями и задачами испытаний (FR-T1)."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=11, models=2, datasets=7, tasks=6)
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="создан", check_id="TC-FILE-13"
    )
    entity(session, entity_id=MODEL_ID, entity_type="модель", action="создан", check_id="TC-MOD-02")

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    rows = {row["counter"]: row for row in outcome.evidence["delta"]}
    assert rows["files"]["delta"] == 1 and rows["files"]["explained"] is True
    assert rows["models"]["delta"] == 1 and rows["models"]["expected"] == 1
    assert rows["datasets"]["delta"] == 0
    assert rows["tasks"]["delta"] == 1 and "задачи испытаний" in rows["tasks"]["reason"]
    assert outcome.evidence["unexplained"] == []
    assert outcome.note == ""
    assert "не объяснено 0" in outcome.verdict


def test_snapshots_note_unexplained_delta():
    """Рост реестра без сущностей в сессии — примечание (расхождение в отчёт)."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=12, models=1, datasets=7, tasks=5)

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["unexplained"] == ["files: +2 (ожидалось +0)"]
    assert "расхождения для отчёта" in outcome.note
    assert "files" in outcome.note


def test_snapshots_note_negative_delta():
    """Уменьшение счётчика — необъяснённое расхождение (удалено больше, чем учтено)."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=8, models=1, datasets=7, tasks=5)

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    rows = {row["counter"]: row for row in outcome.evidence["delta"]}
    assert "уменьшился" in rows["files"]["reason"]
    assert outcome.evidence["unexplained"] == ["files: -2 (ожидалось +0)"]


def test_snapshots_explains_returned_entities():
    """Создано и удалено по одной сущности — счётчик вернулся к прежнему значению."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="создан", check_id="TC-FILE-13"
    )
    entity(
        session, entity_id=FILE_ID, entity_type="файл RAW", action="удалён", check_id="TC-FILE-13"
    )

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["unexplained"] == []
    assert outcome.evidence["entities"]["created"] == {"files": 1}
    assert outcome.evidence["entities"]["deleted"] == {"files": 1}


def test_snapshots_skipped_without_end_snapshot():
    """Снимок «Окончание» не снят — «пропущена» с подсказкой оператору."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "Окончание" in outcome.verdict


def test_snapshots_skipped_without_start_snapshot():
    """Снимок «Начало» не снят — «пропущена»."""
    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03"))

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "Начало" in outcome.verdict


def test_snapshots_note_for_snapshot_errors_and_version_change():
    """Ошибки чтения реестров и смена сборки — примечания отчёта."""
    session = new_session(base_url=BASE_URL)
    start = snapshot(files=10, models=1, datasets=7, tasks=5, version="dev@abc")
    end = snapshot(files=10, models=1, datasets=7, tasks=5, version="dev@def")
    end["errors"] = {"loads": "реестр нагрузок не получен"}
    session.snapshots["start"] = start
    session.snapshots["end"] = end

    outcome = check_cleanup.evaluate(stand("TC-CLEAN-03", session))

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "ошибки чтения реестров" in outcome.note
    assert "сборка сервера" in outcome.note


# ---------------------------------------------------------------------------
# TC-CLEAN-01 — самоочистка `__TEST__`-сущностей
# ---------------------------------------------------------------------------
def _pending_session(*entities: tuple[str, str, str]) -> Any:
    """Сессия с созданными (не удалёнными) `__TEST__`-сущностями."""
    session = new_session(base_url=BASE_URL)
    for entity_id, entity_type, check_id in entities:
        entity(
            session,
            entity_id=entity_id,
            entity_type=entity_type,
            action=session_api.ENTITY_CREATED,
            check_id=check_id,
        )
    return session


def test_self_cleaning_removes_pending_entities():
    """Файл, датасет и модель удаляются по типам, следов в реестрах не остаётся."""
    session = _pending_session(
        (FILE_ID, "файл RAW", "TC-FILE-13"),
        (DATASET_ID, "dataset", "TC-DS-01"),
        (MODEL_ID, "модель", "TC-MOD-02"),
    )
    fake = FakeRegistry(
        files=[file_row(FILE_ID, "__TEST__TC_FILE_13_session.csv")],
        datasets=[dataset_row(DATASET_ID, "__TEST__TC_DS_01_session")],
        models=[model_row(MODEL_ID, "__TEST__TC_MOD_02_session")],
    )
    context = stand("TC-CLEAN-01", session, handler=fake.handler)

    outcome = check_cleanup.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["pending_before"] == 3
    assert [row["result"] for row in outcome.evidence["removed"]] == ["удалено"] * 3
    assert outcome.evidence["leftovers"] == []
    assert session_api.pending_test_entities(session) == []
    assert {kind for kind, _ in fake.deleted} == {"файл", "датасет", "модель"}
    assert fake.files == [] and fake.datasets == [] and fake.models == []


def test_self_cleaning_fails_when_delete_is_rejected():
    """Сервер не удаляет сущность — отказ: `__TEST__`-след остался в сессии."""
    session = _pending_session((FILE_ID, "файл RAW", "TC-FILE-13"))
    fake = FakeRegistry(
        files=[file_row(FILE_ID, "__TEST__TC_FILE_13_session.csv")], delete_file_status=500
    )
    context = stand("TC-CLEAN-01", session, handler=fake.handler)

    outcome = check_cleanup.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не удалено" in outcome.verdict


def test_self_cleaning_detects_session_leftovers():
    """Удаление «прошло», но имя этой сессии осталось в реестре — отказ."""
    session = _pending_session((FILE_ID, "файл RAW", "TC-FILE-13"))
    marker = f"__TEST__TC_FILE_13_{session.session_id}.csv"
    fake = FakeRegistry(files=[file_row(FILE_ID, marker)])
    # файл удаляется из реестра, но сценарий видит второй след с тем же маркером
    fake.files.append(file_row("22222222-2222-4222-8222-222222222222", marker))
    context = stand("TC-CLEAN-01", session, handler=fake.handler)

    outcome = check_cleanup.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "остались `__TEST__`-сущности этой сессии" in outcome.verdict


def test_self_cleaning_notes_foreign_leftovers():
    """`__TEST__`-имена других прогонов — примечание, а не отказ."""
    session = new_session(base_url=BASE_URL)
    fake = FakeRegistry(files=[file_row(FILE_ID, "__TEST__TC_FILE_13_2026-01-01-old.csv")])
    context = stand("TC-CLEAN-01", session, handler=fake.handler)

    outcome = check_cleanup.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "других прогонов" in outcome.note
    assert outcome.evidence["pending_before"] == 0
    assert "подтверждена" in outcome.verdict


def test_self_cleaning_fails_on_unsupported_type():
    """Тип сущности, который уборка не знает, — отказ с подсказкой оператору."""
    session = _pending_session((FILE_ID, "запись", "TC-REC-01"))
    context = stand("TC-CLEAN-01", session, handler=FakeRegistry().handler)

    outcome = check_cleanup.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "уборкой не поддержан" in outcome.verdict


def test_self_cleaning_notes_snapshot_delta():
    """Изменение счётчиков между снимками — примечание со ссылкой на TC-CLEAN-03."""
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = snapshot(files=10, models=1, datasets=7, tasks=5)
    session.snapshots["end"] = snapshot(files=11, models=1, datasets=7, tasks=6)
    context = stand("TC-CLEAN-01", session, handler=FakeRegistry().handler)

    outcome = check_cleanup.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert {"counter": "files", "delta": 1} in outcome.evidence["snapshot_delta"]
    assert "TC-CLEAN-03" in outcome.note
