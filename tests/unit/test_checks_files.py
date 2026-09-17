"""Тесты сценариев проверок модуля «File Import» (TC-FILE-01…14, этап T3).

Стенд собирается без сети: `httpx.MockTransport` реализует реестр файлов, скачивание,
загрузку и удаление, повторяя особенности живого сервиса (замечания №1/№2/№5):

    * `GET /api/data/files` игнорирует `limit`/`offset` (клиентская пагинация);
    * `file_name` — подстрока, `file_type` — точное регистрозависимое совпадение;
    * скачивание не-ASCII имён отвечает `404 latin-1` (проверка «блокировано API»);
    * `POST /api/data/file` проверяет MIME части, а не содержимое файла.

Отдельное внимание — классу `live`: сценарии `TC-FILE-11/12/13` работают только с
именами `__TEST__…`, а `TC-FILE-13` обязан удалить созданный файл (самоочистка NFR-T4),
поэтому в тестах проверяются и реестр после прогона, и учёт `session.counters`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

import httpx

from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import engine as checks_engine
from acceptance.checks import files as check_files
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session, pending_test_entities
from acceptance.session import test_entities as session_test_entities
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
ASCII_ID = "11111111-1111-4111-8111-111111111111"
DUPLICATE_ID = "22222222-2222-4222-8222-222222222222"
MARKUP_ID = "33333333-3333-4333-8333-333333333333"
NON_ASCII_ID = "44444444-4444-4444-8444-444444444444"
OTHER_ID = "55555555-5555-4555-8555-555555555555"
NON_ASCII_NAME = "Отчёт_день.raw.csv"
Handler = Callable[[httpx.Request], httpx.Response]


def spec_of(check_id: str):
    """Описание проверки каталога с проверкой наличия."""
    spec = catalog.find(check_id)
    assert spec is not None
    return spec


def file_record(
    file_id: str,
    name: str,
    size: int,
    *,
    file_type: str = "RAW",
    import_date: str = "2026-09-10T10:00:00",
    s3_path: str = "s3://bucket/path",
) -> dict[str, object]:
    """Запись реестра файлов в формате ответа `GET /api/data/files`."""
    return {
        "id": file_id,
        "file_name": name,
        "size": size,
        "s3_path": s3_path,
        "import_date": import_date,
        "file_type": file_type,
    }


def default_files() -> list[dict[str, object]]:
    """Живой реестр для тестов: дубль имени, markup, не-ASCII и «прочий» файл."""
    return [
        file_record(ASCII_ID, "Antminer_S19.raw.csv", 120),
        file_record(
            DUPLICATE_ID,
            "Antminer_S19.raw.csv",
            200,
            import_date="2026-09-15T09:00:00",
            s3_path="s3://bucket/later",
        ),
        file_record(
            MARKUP_ID,
            "Antminer_S19.markup.csv",
            60,
            file_type="LOADS",
            import_date="2026-09-10T11:00:00",
        ),
        file_record(NON_ASCII_ID, NON_ASCII_NAME, 30, import_date="2026-09-11T10:00:00"),
        file_record(OTHER_ID, "model_report.zip", 4096, file_type="REPORT_ZIP"),
    ]


def many_files(count: int = 7) -> list[dict[str, object]]:
    """Реестр из `count` файлов: нужен там, где важно отличить страницу от реестра."""
    return [
        file_record(f"0000000{i}-0000-4000-8000-00000000000{i}", f"file_{i}.raw.csv", 10 + i)
        for i in range(count)
    ]


def _disposition(name: str) -> str:
    """`Content-Disposition` с ASCII-безопасным именем (RFC 6266 требует ASCII в заголовке)."""
    if name.isascii():
        return f'attachment; filename="{name}"'
    quoted = "".join(f"%{byte:02X}" for byte in name.encode("utf-8"))
    return f"attachment; filename*=UTF-8''{quoted}"


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


class FakeStand:
    """Подменённый сервер файлов: реестр, скачивание, загрузка и удаление."""

    def __init__(
        self,
        files: list[dict[str, object]] | None = None,
        *,
        ignore_limit: bool = True,
        non_ascii_blocked: bool = True,
        download_content_type: str = "application/octet-stream",
        extra_headers: dict[str, str] | None = None,
        accept_bad_upload: bool = False,
        reject_upload: bool = False,
        delete_fails: bool = False,
        count_override: int | None = None,
        upload_payload: dict[str, object] | None = None,
    ) -> None:
        self.files = {str(record["id"]): dict(record) for record in files or default_files()}
        self.bodies: dict[str, bytes] = {
            str(record["id"]): b"x" * int(record["size"]) for record in self.files.values()
        }
        self.ignore_limit = ignore_limit
        self.non_ascii_blocked = non_ascii_blocked
        self.download_content_type = download_content_type
        self.extra_headers = dict(extra_headers or {})
        self.accept_bad_upload = accept_bad_upload
        self.reject_upload = reject_upload
        self.delete_fails = delete_fails
        self.count_override = count_override
        self.upload_payload = dict(upload_payload) if upload_payload is not None else None
        self.deleted: list[str] = []
        self.uploads: list[str] = []

    @property
    def handler(self) -> Handler:
        """Обработчик транспорта (передаётся в `httpx.MockTransport`)."""
        return self._handle

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого сервера."""
        path = request.url.path
        query = dict(request.url.params)
        if path == "/api/data/files":
            return self._list(query)
        if path.startswith("/api/data/file/") and path.endswith("/download"):
            return self._download(path.split("/")[4])
        if path == "/api/data/file" and request.method == "POST":
            return self._upload(request)
        if request.method == "DELETE" and path.startswith("/api/"):
            return self._delete(path.rsplit("/", 1)[-1])
        return httpx.Response(404, json={"detail": "Not Found"})

    def _list(self, query: dict[str, str]) -> httpx.Response:
        """Реестр с фильтрами; `limit`/`offset` игнорируются (замечание к API P2)."""
        rows = [dict(record) for record in self.files.values()]

        def match(record: dict[str, object]) -> bool:
            if query.get("id") and str(record["id"]) != query["id"]:
                return False
            if query.get("file_name") and query["file_name"] not in str(record["file_name"]):
                return False
            if query.get("file_type") and str(record["file_type"]) != query["file_type"]:
                return False
            if query.get("import_date") and str(record["import_date"])[:10] != query["import_date"]:
                return False
            return True

        rows = [record for record in rows if match(record)]
        if not self.ignore_limit and query.get("limit"):
            offset = int(query.get("offset") or 0)
            rows = rows[offset : offset + int(query["limit"])]
        return httpx.Response(
            200,
            json={
                "files": rows,
                "count": self.count_override if self.count_override is not None else len(rows),
            },
        )

    def _download(self, file_id: str) -> httpx.Response:
        """Скачивание: не-ASCII имя → `404 latin-1`, иначе — файл с заголовками."""
        record = self.files.get(file_id)
        if record is None:
            return httpx.Response(404, json={"detail": "Not Found"})
        name = str(record["file_name"])
        if self.non_ascii_blocked and not name.isascii():
            return httpx.Response(
                404,
                json={
                    "detail": "UnicodeEncodeError: 'latin-1' codec can't encode characters "
                    "in position 0-6: ordinal not in range(256)"
                },
            )
        headers = {
            "content-type": self.download_content_type,
            "content-disposition": _disposition(name),
            **self.extra_headers,
        }
        response = httpx.Response(200, content=self.bodies.get(file_id, b""), headers=headers)
        # живой сервер отдаёт файл без `Content-Length`/`Accept-Ranges`/`ETag` (замечание №2),
        # поэтому заголовок, добавленный httpx при подготовке ответа, убирается
        response.headers.pop("content-length", None)
        return response

    def _upload(self, request: httpx.Request) -> httpx.Response:
        """Загрузка: MIME части и обязательные колонки проверяет сервер (замечание №5)."""
        body = request.content
        name_match = re.search(rb'filename="([^"]+)"', body)
        name = name_match.group(1).decode("utf-8") if name_match else "uploaded.csv"
        if self.reject_upload:
            return httpx.Response(500, json={"detail": "storage is unavailable"})
        if not self.accept_bad_upload:
            if b"application/octet-stream" in body:
                return httpx.Response(400, json={"detail": "Only CSV files are allowed"})
            if b"chunkID;timestamp" not in body:
                return httpx.Response(
                    400, json={"detail": "CSV must contain required columns: chunkID, timestamp"}
                )
        file_id = str(uuid4())
        marker = re.search(rb'name="file".*?\r\n\r\n', body, re.DOTALL)
        payload = body[marker.end() :].split(b"\r\n--", 1)[0] if marker else b""
        record = file_record(
            file_id,
            name,
            len(payload),
            import_date=datetime.now().isoformat(timespec="seconds"),
            s3_path=f"s3://bucket/{name}",
        )
        self.files[file_id] = record
        self.bodies[file_id] = payload
        self.uploads.append(name)
        # содержимое ответа загрузки задаётся тестом: `upload_payload`
        if self.upload_payload is not None:
            return httpx.Response(201, json=dict(self.upload_payload))
        return httpx.Response(201, json=dict(record))

    def _delete(self, file_id: str) -> httpx.Response:
        """Удаление: 404 для отсутствующего файла повторяет живую сборку (TC-FILE-14)."""
        if self.delete_fails:
            return httpx.Response(500, json={"detail": "storage is unavailable"})
        if file_id not in self.files:
            return httpx.Response(404, json={"detail": "Not Found"})
        self.files.pop(file_id, None)
        self.bodies.pop(file_id, None)
        self.deleted.append(file_id)
        return httpx.Response(204)


# ---------------------------------------------------------------------------
# Чтение реестра: TC-FILE-01…07
# ---------------------------------------------------------------------------
def test_list_registry_passes_and_reports_volume():
    """Реестр получен полностью: `count` совпадает, поля описаны."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-01")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["count"] == 5
    assert outcome.evidence["returned"] == 5
    assert "s3_path" in outcome.evidence["fields"]


def test_list_registry_fails_when_count_mismatches():
    """`count` больше выдачи — реестр приходит не полностью (UC-03)."""
    fake = FakeStand(count_override=99)
    context, _ = stand(fake.handler, "TC-FILE-01")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "count=99" in outcome.verdict


def test_name_filter_uses_substring():
    """`file_name` фильтрует по подстроке: выдача — подмножество реестра."""
    fake = FakeStand()
    context, journal = stand(fake.handler, "TC-FILE-02", file_name_needle="Antminer")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["found"] == 3
    assert outcome.evidence["of_total"] == 5
    assert journal.records[-1].path == "/api/data/files"


def test_name_filter_fails_when_server_ignores_it():
    """Сервер, игнорирующий фильтр, ловится по именам без подстроки."""
    fake = FakeStand()
    fake._list = lambda query: httpx.Response(  # type: ignore[method-assign]
        200, json={"files": list(fake.files.values()), "count": len(fake.files)}
    )
    context, _ = stand(fake.handler, "TC-FILE-02", file_name_needle="Antminer")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "без подстроки" in outcome.verdict


def test_name_filter_skips_non_ascii_needle_without_result():
    """Не-ASCII подстрока без выдачи — «пропущена»: дефект latin-1 вне этой проверки."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-02", file_name_needle="НетТакого")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "не-ASCII" in outcome.verdict


def test_name_filter_accepts_non_ascii_needle_with_result():
    """Не-ASCII подстрока с выдачей — проверка проходит (имя ищется по подстроке)."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-02", file_name_needle="Отчёт")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["found"] == 1
    assert outcome.evidence["names"] == [NON_ASCII_NAME]


def test_type_filter_records_case_sensitivity_as_fact():
    """Точный `file_type` даёт выдачу, строчный — пустую: регистрозависимость зафиксирована."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-03")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["types"] == ["LOADS", "RAW", "REPORT_ZIP"]
    assert outcome.evidence["file_type=LOADS"] == 1
    assert outcome.evidence["file_type=loads"] == 0
    assert "регистр влияет" in str(outcome.evidence["case_sensitive"])


def test_type_filter_notes_when_case_is_ignored():
    """Если сервер не различает регистр — это факт в вердикте (замечание к API)."""
    fake = FakeStand()
    original = fake._list

    def case_insensitive(query: dict[str, str]) -> httpx.Response:
        """Подменённый список: `file_type` сравнивается без учёта регистра."""
        rows = [
            dict(record)
            for record in fake.files.values()
            if not query.get("file_type")
            or str(record["file_type"]).lower() == query["file_type"].lower()
        ]
        return httpx.Response(200, json={"files": rows, "count": len(rows)})

    fake._list = case_insensitive  # type: ignore[method-assign]
    context, _ = stand(fake.handler, "TC-FILE-03")

    outcome = check_files.evaluate(context)

    fake._list = original  # type: ignore[method-assign]
    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "регистр не влияет" in str(outcome.evidence["case_sensitive"])
    assert "замечание к API" in outcome.verdict


def test_import_date_filter_keeps_only_that_day():
    """`import_date` фильтрует по календарной дате, время недоступно (BR-R2)."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-04")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["date"] == "2026-09-15"
    assert outcome.evidence["found"] == 1
    assert "времени недоступен" in str(outcome.evidence["note"])


def test_import_date_filter_fails_with_other_dates():
    """Сервер, игнорирующий дату, ловится по файлам других дат."""
    fake = FakeStand()
    fake._list = lambda query: httpx.Response(  # type: ignore[method-assign]
        200, json={"files": list(fake.files.values()), "count": len(fake.files)}
    )
    context, _ = stand(fake.handler, "TC-FILE-04")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "других дат" in outcome.verdict


def test_pagination_notes_client_side_behaviour():
    """`limit`/`offset` игнорируются: зафиксирована клиентская пагинация (NFR-4)."""
    fake = FakeStand(many_files(), ignore_limit=True)
    context, journal = stand(fake.handler, "TC-FILE-05")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["full_size"] == 7
    assert outcome.evidence["page_size"] == 7
    assert "клиентская пагинация" in str(outcome.evidence["mode"])
    assert journal.records[-1].query == "limit=5&offset=0"


def test_pagination_detects_server_side_slice():
    """Серверный срез различает страницы: зафиксирована серверная пагинация."""
    fake = FakeStand(many_files(), ignore_limit=False)
    context, _ = stand(fake.handler, "TC-FILE-05")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["full_size"] == 7
    assert outcome.evidence["page_size"] == 5
    assert outcome.evidence["mode"] == "серверная пагинация"


def test_pagination_fails_when_probe_answers_with_error():
    """Отказ на пробу `limit=5&offset=0` — отказ проверки с текстом ошибки."""
    fake = FakeStand()

    def broken(request: httpx.Request) -> httpx.Response:
        """Подменённый сервер: проба с `limit` отвечает 500, остальное — как обычно."""
        if request.url.params.get("limit"):
            return httpx.Response(500, text="boom")
        return fake._handle(request)

    context, _ = stand(broken, "TC-FILE-05")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "отвечает 500" in outcome.verdict


def test_volume_reports_totals_and_snapshot_drift():
    """Объём реестра и расхождение со снимком стенда попадают в доказательства."""
    fake = FakeStand()
    session = new_session(base_url=BASE_URL)
    session.snapshots["start"] = {
        "counts": {"files": 4},
        "files": {"total_bytes": 400},
    }
    context, _ = stand(fake.handler, "TC-FILE-06", session=session)

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["files"] == 5
    assert outcome.evidence["by_type"]["RAW"] == 3
    assert "реестр изменился с начала сессии" in str(outcome.evidence["drift"])
    assert "снимок стенда" not in outcome.verdict or "изменился" in outcome.verdict


def test_duplicates_describe_versions():
    """Дубли имён различаются `id`/`size`/`import_date`/`s3_path` (замечание №1)."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-07")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["name"] == "Antminer_S19.raw.csv"
    assert outcome.evidence["versions"] == 2
    assert outcome.evidence["distinct"]["id"] == 2
    assert outcome.evidence["checksum_field"] is False
    assert "полей `checksum`/`updated_at` в схеме нет" in outcome.verdict


def test_duplicates_skipped_without_duplicates():
    """Реестр без дублей — проверка «пропущена» (данные не позволяют её выполнить)."""
    unique = [
        file_record(ASCII_ID, "one.raw.csv", 10),
        file_record(MARKUP_ID, "one.markup.csv", 20, file_type="LOADS"),
    ]
    fake = FakeStand(unique)
    context, _ = stand(fake.handler, "TC-FILE-07")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "одноимённых файлов" in outcome.verdict


# ---------------------------------------------------------------------------
# Скачивание: TC-FILE-08…10
# ---------------------------------------------------------------------------
def test_download_ascii_compares_size_with_registry():
    """Скачанный ASCII-файл совпадает по размеру с записью реестра (UC-04)."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-08")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["file_id"] == MARKUP_ID  # автовыбор: малый markup-файл
    assert outcome.evidence["size_registry"] == outcome.evidence["size_downloaded"] == 60


def test_download_ascii_fails_on_size_mismatch():
    """Размер скачанного файла не совпал с реестром — отказ проверки."""
    fake = FakeStand()
    fake.bodies[MARKUP_ID] = b"short"
    context, _ = stand(fake.handler, "TC-FILE-08")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не совпадает" in outcome.verdict


def test_download_headers_are_reported_as_api_note():
    """Отсутствие `Content-Length`/`Accept-Ranges`/`ETag` — факт и замечание к API."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-09")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["missing"] == ["content-length", "accept-ranges", "etag"]
    assert outcome.evidence["content_type"] == "application/octet-stream"
    assert "заголовки скачивания: нет" in outcome.verdict


def test_download_headers_detects_streaming_support():
    """Если сервер отдаёт `Content-Length`/`ETag`, проверка это фиксирует."""
    fake = FakeStand(extra_headers={"etag": '"abc"', "accept-ranges": "bytes"})
    context, _ = stand(fake.handler, "TC-FILE-09")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "etag" in outcome.evidence["present"]
    assert outcome.evidence["content_type"] == "application/octet-stream"


def test_non_ascii_download_is_blocked_by_api():
    """`404 latin-1` на не-ASCII имя — «блокировано API» + автозамечание P0 (FR-T7)."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-10", file_id=NON_ASCII_ID)

    result = check_runner.automate(context)

    assert result is not None and result.status == CheckStatus.BLOCKED
    assert "404 latin-1" in result.verdict
    note = checks_engine.ensure_defect_note(context.session, spec_of("TC-FILE-10"), result)
    assert note is not None
    assert note["priority"] == "P0"
    assert note["module"] == "File Import"


def test_non_ascii_download_passes_when_fixed():
    """Если не-ASCII имя скачивается, дефект не воспроизводится — проверка пройдена."""
    fake = FakeStand(non_ascii_blocked=False)
    context, _ = stand(fake.handler, "TC-FILE-10")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "не воспроизводится" in outcome.verdict


def test_non_ascii_download_skipped_without_such_files():
    """Реестр без не-ASCII имён — проверка «пропущена»."""
    files = [file_record(ASCII_ID, "only.raw.csv", 10)]
    fake = FakeStand(files)
    context, _ = stand(fake.handler, "TC-FILE-10")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "не-ASCII именами" in outcome.verdict


# ---------------------------------------------------------------------------
# Запись: TC-FILE-11/12 (класс `live`, самоочистка NFR-T4)
# ---------------------------------------------------------------------------
def test_invalid_csv_is_rejected_with_detail():
    """CSV без обязательных колонок отклоняется с текстом `detail` (UC-02)."""
    fake = FakeStand()
    context, journal = stand(fake.handler, "TC-FILE-11")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "required columns" in str(outcome.evidence["detail"])
    assert outcome.evidence["file_name"].startswith("__TEST__")
    assert fake.uploads == []  # сервер ничего не создал
    assert journal.records[-1].path == "/api/data/file"


def test_invalid_csv_accepted_creates_failed_check_with_cleanup():
    """Если сервер принял «плохой» CSV, созданное убирается за собой, а проверка — отказ."""
    fake = FakeStand(accept_bad_upload=True)
    session = new_session(base_url=BASE_URL)
    context, _ = stand(fake.handler, "TC-FILE-11", session=session)

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "ожидался 400" in outcome.verdict
    assert outcome.evidence["cleanup"] == "файл удалён"
    assert not pending_test_entities(session)
    assert set(fake.files) == {str(record["id"]) for record in default_files()}


def test_wrong_mime_is_rejected():
    """MIME части проверяется сервером: `application/octet-stream` даёт 400 (замечание №5)."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-12")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["part_content_type"] == "application/octet-stream"
    assert "Only CSV files are allowed" in str(outcome.evidence["detail"])


def test_wrong_mime_accepted_creates_failed_check_with_cleanup():
    """Принятый файл с неверным MIME — отказ, созданное удаляется (NFR-T4)."""
    fake = FakeStand(accept_bad_upload=True)
    context, _ = stand(fake.handler, "TC-FILE-12")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "MIME-тип не проверяется" in outcome.verdict
    assert outcome.evidence["cleanup"] == "файл удалён"


# ---------------------------------------------------------------------------
# Круговой рейс и удаление: TC-FILE-13/14
# ---------------------------------------------------------------------------
def test_round_trip_uploads_lists_downloads_and_deletes():
    """Полный круговой рейс: файл создан, найден, скачан байт-в-байт и удалён."""
    fake = FakeStand()
    session = new_session(base_url=BASE_URL)
    context, journal = stand(fake.handler, "TC-FILE-13", session=session)

    result = check_runner.automate(context)

    assert result is not None and result.status == CheckStatus.PASSED
    assert result.evidence["file_name"].startswith("__TEST__")
    assert result.evidence["in_registry"] is True
    assert result.evidence["sha256_uploaded"] == result.evidence["sha256_downloaded"]
    assert result.evidence["deleted"] is True
    assert result.evidence["left_in_registry"] == 0
    assert fake.uploads == [result.evidence["file_name"]]
    assert set(fake.files) == {str(record["id"]) for record in default_files()}
    actions = [item["action"] for item in session_test_entities(session)]
    assert actions == ["создан", "удалён"]
    assert pending_test_entities(session) == []
    events = [item["event"] for item in session.history]
    assert events.index("test_entity_created") < events.index("test_entity_deleted")
    assert events[-1] == "check_recorded"
    assert len(journal.records) == 5  # upload → list → download → delete → list (проверка)
    assert [(record.method, record.path) for record in journal.records] == [
        ("POST", "/api/data/file"),
        ("GET", "/api/data/files"),
        ("GET", f"/api/data/file/{result.evidence['file_id']}/download"),
        ("DELETE", f"/api/{result.evidence['file_id']}"),
        ("GET", "/api/data/files"),
    ]
    assert {record.label for record in journal.records} == {"TC-FILE-13"}


def test_round_trip_rejects_non_test_file_name():
    """Сценарий не создаёт ничего, кроме `__TEST__…` (§9 ТЗ, NFR-T4)."""
    fake = FakeStand()
    context, _ = stand(fake.handler, "TC-FILE-13", csv_name="Antminer_S19.raw.csv")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "должно начинаться с `__TEST__`" in outcome.verdict
    assert fake.uploads == []


def test_round_trip_fails_when_download_differs_but_cleans_up():
    """Расхождение байт — отказ, но созданный файл всё равно удаляется."""
    fake = FakeStand()
    session = new_session(base_url=BASE_URL)
    original = fake._download

    def tampered(file_id: str) -> httpx.Response:
        """Скачивание портит содержимое: имитация ошибки хранилища."""
        response = original(file_id)
        if response.status_code == 200:
            return httpx.Response(200, content=response.content + b"!")
        return response

    fake._download = tampered  # type: ignore[method-assign]
    context, _ = stand(fake.handler, "TC-FILE-13", session=session)

    outcome = check_files.evaluate(context)

    fake._download = original  # type: ignore[method-assign]
    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "байты не совпадают" in outcome.verdict
    assert outcome.evidence["deleted"] is True
    assert not pending_test_entities(session)


def test_round_trip_fails_when_upload_rejected():
    """Отказ загрузки — отказ проверки без создания сущностей."""
    fake = FakeStand(reject_upload=True)
    session = new_session(base_url=BASE_URL)
    context, _ = stand(fake.handler, "TC-FILE-13", session=session)

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "отклонена" in outcome.verdict
    assert session_test_entities(session) == []


def test_round_trip_fails_when_delete_fails():
    """Неудачная самоочистка — отказ: файл остаётся в реестре, это видно в доказательствах."""
    fake = FakeStand(delete_fails=True)
    session = new_session(base_url=BASE_URL)
    context, _ = stand(fake.handler, "TC-FILE-13", session=session)

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "самоочистка не выполнена" in outcome.verdict
    assert outcome.evidence["deleted"] is False
    assert len(pending_test_entities(session)) == 1


def test_round_trip_reports_missing_id_in_upload_answer():
    """Ответ загрузки без `id`: файл создан, но управлять им нельзя — отказ."""
    fake = FakeStand(upload_payload={"status": "saved"})
    context, _ = stand(fake.handler, "TC-FILE-13")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не содержит `id`" in outcome.verdict


def test_delete_missing_file_passes_on_404():
    """Удаление несуществующего файла отвечает 404 — код согласован (UC-05)."""
    fake = FakeStand()
    context, journal = stand(fake.handler, "TC-FILE-14")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["status"] == 404
    assert "batch-удаления пары" in str(outcome.evidence["note"])
    assert journal.records[-1].method == "DELETE"


def test_delete_missing_file_fails_on_success():
    """2xx на удаление несуществующего файла — отказ (сервер сообщает неверный результат)."""
    fake = FakeStand()

    def always_ok(request: httpx.Request) -> httpx.Response:
        """Подменённый сервер: любой DELETE возвращает 204."""
        if request.method == "DELETE":
            return httpx.Response(204)
        return fake._handle(request)

    context, _ = stand(always_ok, "TC-FILE-14")

    outcome = check_files.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "ожидалась ошибка 404" in outcome.verdict
