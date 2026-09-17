"""Тесты сценариев проверок записей `TC-REC-01…06` (этап T3).

Записи API не отдаёт: пульт объединяет плоский реестр файлов сам
(`acceptance.records`). Поэтому стенд для тестов — это реестр с типовыми случаями:

    * пара `RAW + markup` одной базы (сопоставление по времени импорта);
    * база имени с двумя версиями RAW (дубли, замечание №1);
    * markup без RAW и RAW без разметки (несопоставленные);
    * «прочие» файлы (`H5`, `REPORT_ZIP`), которые в записи не попадают.

Сценарии проверяют вердикты и полноту манифеста; `TC-REC-05` дополнительно кладёт
клиентскую оценку записей/чанков в сессию (`lib.markup_stats`), а `TC-REC-03`
остаётся ручной проверкой.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest

from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import records as check_records
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.overrides import Overrides
from acceptance.session import new_session
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"

#: markup-файл с двумя записями и двумя чанками (формат живого сервиса).
MARKUP_BODY = (
    b"markupId;chunkID;LoadID;Ph_n;Category\n"
    b"1;chunk-1;load-1;Ph_A;Antminer\n"
    b"2;chunk-2;load-1;Ph_A;Antminer\n"
    b"3;chunk-2;load-2;Ph_B;Antminer\n"
)

RAW_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
RAW_A2 = "aaaaaaaa-2222-4222-8222-aaaaaaaaaaaa"
UNPAIRED_RAW = "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb"
MARKUP_A = "cccccccc-1111-4111-8111-cccccccccccc"
MARKUP_FREE = "cccccccc-3333-4333-8333-cccccccccccc"
MARKUP_NON_ASCII = "cccccccc-4444-4444-8444-cccccccccccc"
OTHER_H5 = "dddddddd-1111-4111-8111-dddddddddddd"
OTHER_ZIP = "dddddddd-2222-4222-8222-dddddddddddd"
Handler = Callable[[httpx.Request], httpx.Response]


def file_record(
    file_id: str,
    name: str,
    *,
    size: int = 100,
    file_type: str = "RAW",
    import_date: str = "2026-09-10T10:00:00",
) -> dict[str, object]:
    """Запись реестра файлов в формате ответа `GET /api/data/files`."""
    return {
        "id": file_id,
        "file_name": name,
        "size": size,
        "s3_path": f"s3://bucket/{name}",
        "import_date": import_date,
        "file_type": file_type,
    }


def default_files() -> list[dict[str, object]]:
    """Реестр с парой, дублем версии, несопоставленными и «прочими» файлами."""
    return [
        file_record(RAW_A, "Antminer_S19.raw.csv", size=1000),
        file_record(MARKUP_A, "Antminer_S19.markup.csv", size=300, file_type="LOADS"),
        file_record(RAW_A2, "Antminer_S19.raw.csv", size=1200, import_date="2026-09-15T09:00:00"),
        file_record(
            UNPAIRED_RAW, "Tektronix_PWS.raw.csv", size=800, import_date="2026-09-12T10:00:00"
        ),
        file_record(MARKUP_FREE, "Free_markup.markup.csv", size=90, file_type="LOADS"),
        file_record(MARKUP_NON_ASCII, "Разметка_день.markup.csv", size=95, file_type="LOADS"),
        file_record(OTHER_H5, "model_best.h5", size=5000, file_type="H5"),
        file_record(OTHER_ZIP, "report_2026.zip", size=7000, file_type="REPORT_ZIP"),
    ]


def spec_of(check_id: str):
    """Описание проверки каталога с проверкой наличия."""
    spec = catalog.find(check_id)
    assert spec is not None
    return spec


class FakeRegistry:
    """Подменённый сервер: реестр файлов и скачивание markup-файлов."""

    def __init__(self, files: list[dict[str, object]] | None = None, *, blocked: bool = True):
        self.files = {str(record["id"]): dict(record) for record in files or default_files()}
        self.bodies: dict[str, bytes] = {MARKUP_A: MARKUP_BODY, MARKUP_FREE: MARKUP_BODY}
        self.blocked = blocked

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого сервера."""
        path = request.url.path
        if path == "/api/data/files":
            query = dict(request.url.params)
            rows = [
                dict(record)
                for record in self.files.values()
                if not query.get("file_name") or query["file_name"] in str(record["file_name"])
            ]
            return httpx.Response(200, json={"files": rows, "count": len(rows)})
        if path.startswith("/api/data/file/") and path.endswith("/download"):
            file_id = path.split("/")[4]
            record = self.files.get(file_id)
            if record is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            if self.blocked and not str(record["file_name"]).isascii():
                return httpx.Response(
                    404, json={"detail": "UnicodeEncodeError: 'latin-1' codec can't encode"}
                )
            response = httpx.Response(
                200,
                content=self.bodies.get(file_id, b""),
                headers={"content-type": "application/octet-stream"},
            )
            response.headers.pop("content-length", None)
            return response
        return httpx.Response(404, json={"detail": "Not Found"})


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Решения оператора по записям в тестах пусты: файл правил не читается с диска."""
    monkeypatch.setattr(check_records, "load_overrides", Overrides)


def stand(handler: Handler, check_id: str, *, session=None, **params):
    """Контекст сценария поверх подменённого транспорта (без реальной сети)."""
    journal = Journal(max_records=200)
    transport = httpx.MockTransport(handler)
    client = ApiHttpClient(
        base_url=BASE_URL, timeout=5.0, transport=LoggingTransport(journal, inner=transport)
    )
    settings = TrainingServerSettings(base_url=BASE_URL, timeout=5.0)
    console = build_console_client(settings, journal, inner=transport)
    context = check_runner.AutomationContext(
        session=session or new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=Apis.build(client),
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
    )
    return context, journal


class IncompleteStats:
    """Минимальная статистика объединения для заглушки неполного манифеста."""

    files = 8
    records = 3
    duplicate_bases = 1
    coverage = 50


@dataclass(frozen=True)
class StubFile:
    """Минимальное «файл реестра» для заглушек: `id` и имя."""

    id: str
    file_name: str


class LostOtherOverview:
    """Заглушка объединения: «прочий» файл выпал из манифеста (ветка отказа `TC-REC-04`)."""

    records: tuple[()] = ()
    unpaired_raw: tuple[()] = ()
    unpaired_markup: tuple[()] = ()
    other_files = (StubFile(OTHER_H5, "model_best.h5"),)
    stats = IncompleteStats()

    def manifest_file_ids(self) -> list[str]:
        """Манифест без «прочих» файлов: пульт их потерял."""
        return []


class IncompleteOverview:
    """Заглушка объединения: манифест неполон (ветка отказа `TC-REC-02`)."""

    duplicate_bases: tuple[str, ...] = ("Antminer_S19",)
    stats = IncompleteStats()

    def manifest_is_complete(self) -> bool:
        """Манифест потерял файлы реестра."""
        return False

    def manifest_rows(self) -> list[dict[str, object]]:
        """Строки манифеста (одна — потеря остальных)."""
        return [{"record_id": "r-1"}]

    def manifest_file_ids(self) -> list[str]:
        """Идентификаторы файлов, попавших в манифест."""
        return [RAW_A]

    def for_base(self, base_name: str) -> tuple[()]:
        """Версии записи: у неполного манифеста их нет."""
        return ()


# ---------------------------------------------------------------------------
# TC-REC-01 — объединение RAW+markup
# ---------------------------------------------------------------------------
def test_pairing_reports_rule_and_stats():
    """Записи сформированы по правилу «имя + время импорта», статистика в доказательствах."""
    fake = FakeRegistry()
    context, _ = stand(fake.handler, "TC-REC-01")

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["rule"] == "имя + время импорта"
    assert outcome.evidence["stats"]["records"] >= 3
    assert outcome.evidence["stats"]["attached_markup"] == 1
    assert outcome.evidence["sample"][0]["rule"]


def test_pairing_skipped_without_pairs():
    """Реестр без пар RAW + markup — «пропущена»: объединять нечего."""
    files = [
        file_record(OTHER_H5, "model_best.h5", file_type="H5"),
        file_record(OTHER_ZIP, "report.zip", file_type="REPORT_ZIP"),
    ]
    fake = FakeRegistry(files)
    context, _ = stand(fake.handler, "TC-REC-01")

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет пар RAW + markup" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-REC-02 — дубли и полнота манифеста
# ---------------------------------------------------------------------------
def test_duplicates_manifest_is_complete():
    """Манифест содержит каждый файл реестра ровно один раз, версии видны."""
    fake = FakeRegistry()
    context, _ = stand(fake.handler, "TC-REC-02")

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["complete"] is True
    assert outcome.evidence["manifest_ids"] == len(fake.files)
    assert outcome.evidence["duplicate_bases"] == ["Antminer_S19"]
    versions = outcome.evidence["versions"]["Antminer_S19"]
    assert len(versions) == 2
    assert {row["raw_id"] for row in versions} == {RAW_A, RAW_A2}


def test_duplicates_manifest_fails_when_incomplete(monkeypatch: pytest.MonkeyPatch):
    """Неполный манифест — отказ: каждый файл реестра должен попасть в него ровно раз."""
    fake = FakeRegistry()
    context, _ = stand(fake.handler, "TC-REC-02")
    monkeypatch.setattr(check_records, "_overview", lambda _context: IncompleteOverview())

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "контроль полноты манифеста не пройден" in outcome.verdict
    assert outcome.evidence["complete"] is False


# ---------------------------------------------------------------------------
# TC-REC-04 — несопоставленные файлы
# ---------------------------------------------------------------------------
def test_unpaired_and_other_files_are_reported():
    """RAW без разметки, разметка без RAW и «прочие» файлы учтены в разделах и манифесте."""
    fake = FakeRegistry()
    context, _ = stand(fake.handler, "TC-REC-04")

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["raw_without_markup"] == 2
    assert outcome.evidence["markup_without_raw"] == 2
    assert outcome.evidence["other_files"] == 2
    assert outcome.evidence["examples"]["other"] == ["model_best.h5", "report_2026.zip"]
    assert outcome.evidence["other_in_records"] == []


def test_unpaired_fails_when_other_files_lost_from_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    """«Прочие» файлы обязаны попадать в манифест: их потеря — отказ."""
    fake = FakeRegistry()
    context, _ = stand(fake.handler, "TC-REC-04")
    monkeypatch.setattr(check_records, "_overview", lambda _context: LostOtherOverview())

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "прочие файлы отсутствуют в манифесте" in outcome.verdict
    assert outcome.evidence["other_not_in_manifest"] == ["model_best.h5"]


# ---------------------------------------------------------------------------
# TC-REC-05 — записи и чанки разметки
# ---------------------------------------------------------------------------
def test_markup_stats_are_saved_in_session():
    """Разбор доступной разметки: записи и чанки сохранены в сессии как клиентская оценка."""
    fake = FakeRegistry()
    session = new_session(base_url=BASE_URL)
    context, journal = stand(fake.handler, "TC-REC-05", session=session)

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["parsed"][0]["records"] == 3
    assert outcome.evidence["parsed"][0]["chunks"] == 2
    assert outcome.evidence["session_stats"] == 2
    stored = session.markup_stats[MARKUP_A]
    assert stored["records"] == 3 and stored["chunks"] == 2
    assert "клиентская оценка" in str(stored["status"])
    # не-ASCII разметка недоступна (дефект latin-1) — попала в blocked, а не в отказ
    assert outcome.evidence["blocked"][0]["reason"] == "не-ASCII имя (дефект latin-1)"
    assert journal.records[-1].label == "TC-REC-05"  # проба помечена меткой проверки


def test_markup_stats_limits_files_by_param():
    """Параметр `markup_files` ограничивает число разбираемых файлов."""
    files = [
        file_record(RAW_A, "Antminer_S19.raw.csv"),
        file_record(MARKUP_A, "Antminer_S19.markup.csv", file_type="LOADS"),
        file_record(MARKUP_FREE, "Free_markup.markup.csv", file_type="LOADS"),
    ]
    fake = FakeRegistry(files)
    session = new_session(base_url=BASE_URL)
    context, _ = stand(fake.handler, "TC-REC-05", session=session, markup_files="1")

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert len(outcome.evidence["parsed"]) == 1
    assert len(session.markup_stats) == 1


def test_markup_stats_blocked_when_nothing_downloads():
    """Если ни один markup-файл не скачался — «блокировано API» + замечание (FR-T7)."""
    files = [
        file_record(RAW_A, "Antminer_S19.raw.csv"),
        file_record(MARKUP_NON_ASCII, "Разметка_день.markup.csv", file_type="LOADS"),
    ]
    fake = FakeRegistry(files)
    session = new_session(base_url=BASE_URL)
    context, _ = stand(fake.handler, "TC-REC-05", session=session)

    result = check_runner.automate(context)

    assert result is not None and result.status == CheckStatus.BLOCKED
    assert "дефект latin-1" in result.verdict
    assert result.evidence["blocked"]
    assert session.markup_stats == {}


def test_markup_stats_skipped_without_markup_files():
    """Реестр без markup-файлов — «пропущена»."""
    files = [file_record(RAW_A, "Antminer_S19.raw.csv")]
    fake = FakeRegistry(files)
    context, _ = stand(fake.handler, "TC-REC-05")

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет markup-файлов" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-REC-06 — сравнение дублей одной записи
# ---------------------------------------------------------------------------
def test_duplicate_versions_compare_sizes_and_dates():
    """Версии одной записи сопоставлены, актуальная определена по `import_date`."""
    fake = FakeRegistry()
    context, _ = stand(fake.handler, "TC-REC-06")

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["base_name"] == "Antminer_S19"
    assert outcome.evidence["raw_size_variants"] == 2
    assert outcome.evidence["actual_source"] == "последняя по `import_date`"
    rows = outcome.evidence["versions"]
    assert {row["raw_size"] for row in rows} == {1000, 1200}
    # у актуальной версии есть разметка (сопоставлена по времени импорта)
    assert any(row["markup_id"] for row in rows)


def test_duplicate_versions_skipped_without_duplicates():
    """Без одноимённых версий проверка «пропущена»: сравнивать нечего."""
    files = [
        file_record(RAW_A, "Antminer_S19.raw.csv"),
        file_record(MARKUP_A, "Antminer_S19.markup.csv", file_type="LOADS"),
    ]
    fake = FakeRegistry(files)
    context, _ = stand(fake.handler, "TC-REC-06")

    outcome = check_records.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "сравнение дублей неприменимо" in outcome.verdict


def test_manual_check_has_no_scenario():
    """TC-REC-03 — ручная перепривязка пары: сценария нет, отметка оператора."""
    manual = spec_of("TC-REC-03")

    assert check_runner.scenario(manual) is None
