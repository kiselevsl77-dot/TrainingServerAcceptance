"""Автоматические сценарии проверок модуля «File Import» (TC-FILE-01…14, этап T3).

Группа делится на три части:

    * **чтение** (`TC-FILE-01…10`) — реестр, его фильтры, объём, дубли имён,
      скачивание и заголовки ответа; проверки «дешёвые» и безопасные;
    * **запись** (`TC-FILE-11/12/13`) — загрузка CSV (класс `live`): две негативные
      пробы (неверные колонки и MIME) и круговой рейс `upload → list → download →
      delete`. Изменяющие проверки работают только с именами `__TEST__…`, требуют
      карточку запуска и **обязательно** убирают за собой созданное
      (`session.counters["test_entities"]`, NFR-T4);
    * **негативная проба** (`TC-FILE-14`) — удаление несуществующего файла.

Ожидаемо блокированная проверка (`TC-FILE-10`) возвращает статус «блокировано API»
с текстом дефекта latin-1 — движок сам формирует замечание к API (FR-T7).
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from acceptance.checks.registry import CheckStatus
from acceptance.checks.runner import (
    AutomationContext,
    CheckOutcome,
    PreconditionError,
    automate,
    evaluate,
    scenario,
)
from acceptance.endpoints import TEST_PREFIX
from acceptance.exchange import ExchangeResult, MultipartPayload
from acceptance.session import SNAP_START, add_test_entity, mark_test_entity_deleted
from client.errors import ApiError, ClientError, ServerUnavailableError
from client.schemas import FileMetadataListResponse, FileMetadataResponse
from lib.subdatasets import FileKind, classify, format_size

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: Предел размера файла, который сценарий качает сам (markup-файлы — единицы МБ;
#: крупные RAW до 2.1 ГБ качает оператор с подтверждением).
MAX_PROBE_BYTES = 8 * 1024 * 1024

#: Ожидаемый `Content-Type` скачивания (спецификация обещает JSON — замечание к API).
EXPECTED_DOWNLOAD_TYPE = "application/octet-stream"

#: MIME части с файлом: `text/csv` принимается, `application/octet-stream` — нет (замечание №5).
MIME_CSV = "text/csv"
MIME_BINARY = "application/octet-stream"

#: Заголовки, отсутствие которых проверяет TC-FILE-09.
STREAMING_HEADERS: tuple[str, ...] = ("content-length", "accept-ranges", "etag")

#: Поля элемента реестра, обязательные для объединения в записи (TC-FILE-01).
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "file_name",
    "size",
    "s3_path",
    "import_date",
    "file_type",
)

#: Строка RAW-CSV для кругового рейса (проверку колонок делает сервер).
ROUND_TRIP_BODY = "chunkID;timestamp;U_A_rms;I_A_rms\n1;2026-01-01T00:00:00;0.1;0.2\n"

#: CSV «без обязательных колонок» — негативная проба TC-FILE-11.
INVALID_BODY = "a;b\n1;2\n"


def _is_ascii(value: str) -> bool:
    """True, если имя состоит только из ASCII-символов (дефект latin-1 ему не мешает)."""
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _registry(context: AutomationContext) -> list[FileMetadataResponse]:
    """Реестр файлов стенда (`GET /api/data/files`).

    Raises:
        PreconditionError: реестр недоступен или пуст — проверять нечего.
    """
    files = list(_list(context).files)
    if not files:
        raise PreconditionError("реестр файлов пуст: проверки File Import неприменимы")
    return files


def _list(context: AutomationContext) -> FileMetadataListResponse:
    """Ответ реестра файлов целиком (список и `count`).

    Raises:
        PreconditionError: реестр недоступен (стенд не отвечает/нет адреса).
    """
    try:
        return context.files_api().list_files()
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        raise PreconditionError(f"реестр файлов не получен: {exc}") from exc


def _markup_files(files: list[FileMetadataResponse]) -> list[FileMetadataResponse]:
    """markup-файлы реестра (маркер `.markup` в имени — правило `lib.subdatasets`)."""
    return [file for file in files if classify(file.file_name)[0] is FileKind.MARKUP]


def _by_name(files: list[FileMetadataResponse], *, ascii_only: bool) -> list[FileMetadataResponse]:
    """Файлы с ASCII-именами или, наоборот, только с не-ASCII (TC-FILE-08/10)."""
    return [file for file in files if _is_ascii(file.file_name) == ascii_only]


def _pick_file(
    context: AutomationContext,
    files: list[FileMetadataResponse],
    *,
    ascii_only: bool,
) -> FileMetadataResponse:
    """Выбирает файл для скачивания: из параметров запуска либо автоматически.

    Автовыбор предпочитает небольшой markup-файл: он быстро качается и пригоден для
    разбора разметки (TC-REC-05). Параметр `file_id` позволяет оператору указать
    конкретный файл на карточке проверки.

    Raises:
        PreconditionError: подходящих файлов нет — оператору нужно указать `file_id`.
    """
    chosen = context.param("file_id")
    if chosen:
        found = next((file for file in files if str(file.id) == chosen), None)
        if found is not None:
            return found

    candidates = _by_name(files, ascii_only=ascii_only)
    ordered = sorted(candidates, key=lambda file: file.size)
    markup = [file for file in ordered if classify(file.file_name)[0] is FileKind.MARKUP]
    pool = markup or ordered
    small = [file for file in pool if file.size <= MAX_PROBE_BYTES]
    if small:
        pool = small
    if not pool:
        raise PreconditionError(
            "нет подходящих файлов в реестре: укажите `file_id` на карточке проверки"
        )
    return pool[0]


def _sha256(data: bytes) -> str:
    """Контрольная сумма содержимого (сверка байт в круговом рейсе, TC-FILE-13)."""
    return hashlib.sha256(data).hexdigest()


def _download(context: AutomationContext, file_id: str) -> tuple[bytes | None, str]:
    """Скачивает файл клиентом API, возвращая содержимое или текст ошибки."""
    try:
        response = context.files_api().download(file_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return response.content, ""


def _delete_file(context: AutomationContext, file_id: str) -> tuple[bool, str]:
    """Удаляет `__TEST__`-файл и отмечает это в учёте сессии (самоочистка, NFR-T4).

    Returns:
        Кортеж (удалено, текст ошибки).
    """
    try:
        context.files_api().delete(file_id)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    mark_test_entity_deleted(
        context.session, file_id, check_id=context.spec.check_id, note="самоочистка"
    )
    return True, ""


def _upload_probe(
    context: AutomationContext,
    *,
    file_name: str,
    body: str,
    content_type: str = "",
) -> ExchangeResult:
    """Загружает файл «сырым» клиентом: нужен статус ответа, а не исключение.

    Args:
        context: контекст проверки (нужны клиент проб и метка проверки).
        file_name: имя файла (`__TEST__…`).
        body: содержимое CSV.
        content_type: принудительный MIME части (пусто — по расширению `.csv`).
    """
    return context.probe_request(
        "POST",
        "/api/data/file",
        multipart=MultipartPayload(
            file_name=file_name,
            content=body.encode("utf-8"),
            file_type="RAW",
            description=f"проверка {context.spec.check_id}",
            content_type_override=content_type,
        ),
    )


def _test_file_name(context: AutomationContext) -> str:
    """Имя тестового файла: `__TEST__<проверка>_<сессия>.csv` (§9 ТЗ, NFR-T4)."""
    override = context.param("csv_name")
    if override:
        return override
    session_id = str(context.session.session_id or "session")
    return f"{TEST_PREFIX}{context.spec.check_id.replace('-', '_')}_{session_id}.csv"


def _register_entity(
    context: AutomationContext, *, entity_id: str, entity_type: str
) -> dict[str, Any]:
    """Регистрирует созданный `__TEST__`-объект в учёте сессии (NFR-T4, TC-CLEAN-02)."""
    session_id = str(context.session.session_id)
    return add_test_entity(
        context.session,
        entity_id=entity_id,
        entity_type=entity_type,
        check_id=context.spec.check_id,
        note=f"создан проверкой {context.spec.check_id} в сессии {session_id}",
    )


def _detail_text(result: ExchangeResult) -> str:
    """Текст ошибки сервера: строка `detail` или первая ошибка валидации."""
    body = result.body_json
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        return str(detail[0])
    return result.body_text[:300] or (result.error or "")


# ---------------------------------------------------------------------------
# Чтение: реестр, фильтры, объём, дубли (TC-FILE-01…07)
# ---------------------------------------------------------------------------
def _list_registry(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-01: реестр получен полностью, `count` = числу элементов (UC-03, BR-F1)."""
    response = _list(context)
    files = list(response.files)
    evidence: dict[str, Any] = {
        "count": response.count,
        "returned": len(files),
        "fields": list(FileMetadataResponse.model_fields),
    }
    problems: list[str] = []
    if not files:
        problems.append("реестр пуст: содержимое проверить нельзя")
    if response.count != len(files):
        problems.append(f"count={response.count} не равен числу элементов files={len(files)}")
    missing = [field for field in REQUIRED_FIELDS if field not in FileMetadataResponse.model_fields]
    if missing:
        problems.append("в схеме элемента нет полей: " + ", ".join(missing))
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    return CheckOutcome(
        CheckStatus.PASSED,
        f"реестр получен: файлов {len(files)}, count совпадает, обязательные поля на месте",
        evidence,
    )


def _name_filter(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-02: `file_name` фильтрует по подстроке (UC-03, NFR-4)."""
    files = _registry(context)
    needle = context.param("file_name_needle")
    if not needle:
        stem = files[0].file_name.rsplit(".", 1)[0]
        needle = stem[-6:] if len(stem) >= 6 else stem
    if not needle:
        raise PreconditionError(
            "в реестре нет имён, пригодных для подстроки: задайте `file_name_needle`"
        )

    try:
        filtered = context.files_api().list_files(file_name=needle)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"фильтр file_name={needle} не отвечает: {exc}",
            {"needle": needle, "error": str(exc)},
        )

    rows = list(filtered.files)
    evidence: dict[str, Any] = {
        "needle": needle,
        "status": 200,
        "found": len(rows),
        "of_total": len(files),
        "names": [file.file_name for file in rows[:5]],
    }
    if not rows:
        if not _is_ascii(needle):
            return CheckOutcome(
                CheckStatus.SKIPPED,
                f"по не-ASCII подстроке «{needle}» ничего не найдено — фильтр проверить нельзя",
                evidence,
            )
        return CheckOutcome(
            CheckStatus.FAILED,
            f"подстрока «{needle}» есть в реестре, но фильтр вернул пустую выдачу",
            evidence,
        )

    wrong = [file.file_name for file in rows if needle.lower() not in file.file_name.lower()]
    if wrong:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"фильтр file_name={needle} вернул имена без подстроки: {wrong[:3]}",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"фильтр file_name: «{needle}» → {len(rows)} из {len(files)} файлов, все имена содержат подстроку",
        evidence,
    )


def _type_filter(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-03: `file_type` фильтрует точно и регистрозависимо (UC-03, NFR-4)."""
    files = _registry(context)
    types = sorted({str(file.file_type) for file in files if str(file.file_type).strip()})
    if not types:
        raise PreconditionError("в реестре нет значений file_type: фильтр проверить нельзя")

    exact = next((value for value in types if value == value.upper()), types[0])
    try:
        rows = list(context.files_api().list_files(file_type=exact).files)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED, f"фильтр file_type={exact} не отвечает: {exc}", {"error": str(exc)}
        )

    evidence: dict[str, Any] = {
        "types": types,
        f"file_type={exact}": len(rows),
    }
    wrong = [file.file_name for file in rows if str(file.file_type) != exact]
    if wrong:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"фильтр file_type={exact} вернул файлы других типов: {wrong[:3]}",
            evidence,
        )
    if not rows:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"фильтр file_type={exact} не вернул ни одного файла, хотя такие в реестре есть",
            evidence,
        )

    lowered = exact.lower()
    if lowered != exact:
        try:
            rows_lower = list(context.files_api().list_files(file_type=lowered).files)
        except (ApiError, ClientError, ServerUnavailableError) as exc:
            rows_lower = []
            evidence["error_lower"] = str(exc)
        evidence[f"file_type={lowered}"] = len(rows_lower)
        evidence["case_sensitive"] = (
            "регистр влияет: строчное значение не даёт выдачи"
            if not rows_lower
            else f"регистр не влияет: строчное значение вернуло {len(rows_lower)} файлов (замечание к API)"
        )
    verdict = f"фильтр file_type={exact} → {len(rows)} файлов"
    if evidence.get("case_sensitive", "").startswith("регистр не влияет"):
        verdict += " · " + str(evidence["case_sensitive"])
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _import_date_filter(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-04: `import_date` фильтрует по календарной дате (UC-03, BR-R2)."""
    files = _registry(context)
    dates = sorted({file.import_date.date() for file in files})
    chosen = dates[-1]
    try:
        rows = list(context.files_api().list_files(import_date=chosen.isoformat()).files)
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"фильтр import_date={chosen} не отвечает: {exc}",
            {"date": chosen.isoformat(), "error": str(exc)},
        )

    wrong = [file.file_name for file in rows if file.import_date.date() != chosen]
    evidence: dict[str, Any] = {
        "date": chosen.isoformat(),
        "dates_in_registry": len(dates),
        "found": len(rows),
        "of_total": len(files),
        "note": "параметр принимает только дату `YYYY-MM-DD`: фильтр по времени недоступен",
    }
    if wrong:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"фильтр import_date={chosen} вернул файлы других дат: {wrong[:3]}",
            evidence,
        )
    if not rows:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"фильтр import_date={chosen} не вернул файлов, хотя такие в реестре есть",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"фильтр import_date={chosen}: {len(rows)} из {len(files)} файлов, все — этой даты",
        evidence,
    )


def _pagination(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-05: `limit`/`offset` влияют на выдачу или игнорируются (NFR-4).

    Параметры не описаны в спецификации, поэтому проба идёт «сырым» клиентом: важен
    факт — работает серверная пагинация или реестр всегда приходит целиком
    (в этом случае пульт пагинирует список сам, замечание к API уровня P2).
    """
    files = list(_list(context).files)
    probe = context.probe_request("GET", "/api/data/files", query={"limit": 5, "offset": 0})
    body = probe.body_json if isinstance(probe.body_json, dict) else {}
    page = body.get("files") if isinstance(body.get("files"), list) else None
    evidence: dict[str, Any] = {
        "query": probe.query,
        "status": probe.status,
        "full_size": len(files),
        "page_size": len(page) if page is not None else None,
        "error": probe.error or None,
    }
    if probe.status is None:
        return CheckOutcome(
            CheckStatus.FAILED, f"проба limit/offset не отвечает: {probe.error}", evidence
        )
    if int(probe.status) >= 400:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"limit=5&offset=0 отвечает {probe.status}: {_detail_text(probe)}",
            evidence,
        )
    if page is None:
        return CheckOutcome(
            CheckStatus.FAILED, "ответ на limit=5&offset=0 не содержит списка `files`", evidence
        )
    if not page and files:
        return CheckOutcome(
            CheckStatus.FAILED,
            "страница limit=5 пуста при непустом реестре: пагинация ломает выдачу",
            evidence,
        )
    if len(page) <= 5 and len(page) < len(files):
        evidence["mode"] = "серверная пагинация"
    else:
        # параметры не описаны в спецификации: пульт пагинирует список сам (P2)
        evidence["mode"] = (
            "клиентская пагинация (параметры limit/offset игнорируются — замечание к API P2)"
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"limit=5 → {len(page)} из {len(files)} файлов ({evidence['mode']})",
        evidence,
    )


def _volume(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-06: объём реестра, суммарный размер и типы попадают в снимок стенда (UC-03)."""
    files = _registry(context)
    by_type: dict[str, int] = {}
    for file in files:
        key = str(file.file_type)
        by_type[key] = by_type.get(key, 0) + 1

    snapshot = context.session.snapshots.get(SNAP_START) or {}
    snapshot_files = (snapshot.get("files") or {}) if isinstance(snapshot, dict) else {}
    total = sum(file.size for file in files)
    evidence: dict[str, Any] = {
        "files": len(files),
        "total_bytes": total,
        "total_label": format_size(total),
        "by_type": by_type,
        "snapshot_files": snapshot_files.get("total_bytes"),
        "snapshot_count": (snapshot.get("counts") or {}).get("files")
        if isinstance(snapshot.get("counts"), dict)
        else None,
        "endpoint": "get /api/data/files",
    }
    verdict = f"файлов {len(files)}, суммарный размер {format_size(total)}, типы: {by_type}"
    if evidence["snapshot_count"] is not None and int(evidence["snapshot_count"]) != len(files):
        evidence["drift"] = (
            f"реестр изменился с начала сессии: было {evidence['snapshot_count']}, стало {len(files)}"
        )
        verdict += " · " + str(evidence["drift"])
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _duplicates(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-07: дубли имён видны в реестре и различаются полями (замечание №1, BR-R2)."""
    files = _registry(context)
    counts: dict[str, int] = {}
    for file in files:
        counts[file.file_name] = counts.get(file.file_name, 0) + 1
    duplicated = sorted(name for name, count in counts.items() if count > 1)
    schema_fields = tuple(FileMetadataResponse.model_fields)
    evidence: dict[str, Any] = {
        "duplicated_names": duplicated[:5],
        "duplicated_total": len(duplicated),
        "schema_fields": list(schema_fields),
        "checksum_field": "checksum" in schema_fields,
        "updated_at_field": "updated_at" in schema_fields,
    }
    if not duplicated:
        return CheckOutcome(
            CheckStatus.SKIPPED,
            "одноимённых файлов в реестре нет: дубли и их отличие проверить нельзя",
            evidence,
        )

    name = next((item for item in duplicated if _is_ascii(item)), duplicated[0])
    rows = [file for file in files if file.file_name == name]
    variants = {
        "id": len({str(file.id) for file in rows}),
        "size": len({file.size for file in rows}),
        "import_date": len({str(file.import_date) for file in rows}),
        "s3_path": len({str(file.s3_path) for file in rows}),
    }
    evidence.update({"name": name, "versions": len(rows), "distinct": variants})
    if variants["id"] != len(rows):
        return CheckOutcome(
            CheckStatus.FAILED,
            f"версии «{name}» не различаются `id`: {len(rows)} записей, уникальных {variants['id']}",
            evidence,
        )
    verdict = f"дубли имён: «{name}» — {len(rows)} версии, различаются " + ", ".join(
        key for key, value in variants.items() if value == len(rows)
    )
    if not evidence["checksum_field"]:
        verdict += " · полей `checksum`/`updated_at` в схеме нет (замечание к API)"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _download_ascii(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-08: файл с ASCII-именем скачивается, размер совпадает с реестром (UC-04)."""
    files = _registry(context)
    file = _pick_file(context, files, ascii_only=True)
    content, error = _download(context, str(file.id))
    evidence: dict[str, Any] = {
        "file_id": str(file.id),
        "file_name": file.file_name,
        "size_registry": file.size,
        "size_downloaded": len(content) if content is not None else None,
        "error": error or None,
    }
    if content is None:
        return CheckOutcome(
            CheckStatus.FAILED, f"файл «{file.file_name}» не скачан: {error}", evidence
        )
    if len(content) != file.size:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"размер скачанного файла {len(content)} не совпадает с `size` реестра {file.size}",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"файл «{file.file_name}» скачан: {format_size(len(content))}, размер совпал с реестром",
        evidence,
    )


def _download_headers(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-09: заголовки скачивания — нет `Content-Length`/`Range`/`ETag` (замечание №2).

    Проба идёт «сырым» клиентом: нужны заголовки ответа. Ожидаемый результат по
    документации испытаний — заголовков стриминга нет; это фиксируется как замечание
    к API (P1), а не как отказ проверки.
    """
    files = _registry(context)
    file = _pick_file(context, files, ascii_only=True)
    probe = context.probe_request("GET", f"/api/data/file/{file.id}/download", binary=True)
    headers = dict(probe.headers)
    present = [name for name in STREAMING_HEADERS if name in headers]
    missing = [name for name in STREAMING_HEADERS if name not in headers]
    evidence: dict[str, Any] = {
        "file_id": str(file.id),
        "file_name": file.file_name,
        "status": probe.status,
        "content_type": probe.content_type,
        "size_bytes": probe.size_bytes,
        "size_registry": file.size,
        "content_disposition_name": probe.file_name,
        "present": present,
        "missing": missing,
        "content_length": headers.get("content-length"),
    }
    if probe.status is None:
        return CheckOutcome(CheckStatus.FAILED, f"скачивание не отвечает: {probe.error}", evidence)
    if int(probe.status) >= 400:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"скачивание отвечает {probe.status}: {_detail_text(probe)}",
            evidence,
        )
    verdict = (
        "заголовки скачивания: нет " + ", ".join(f"`{name}`" for name in missing)
        if missing
        else "заголовки скачивания: `Content-Length`/`Accept-Ranges`/`ETag` присутствуют"
    )
    if probe.content_type and probe.content_type != EXPECTED_DOWNLOAD_TYPE:
        verdict += f" · `Content-Type` = {probe.content_type} (спецификация обещает JSON)"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _download_non_ascii(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-10: не-ASCII имя — ожидаемо `404 latin-1` (замечание P0, «блокировано API»)."""
    files = _registry(context)
    non_ascii = _by_name(files, ascii_only=False)
    if not non_ascii:
        return CheckOutcome(
            CheckStatus.SKIPPED,
            "в реестре нет файлов с не-ASCII именами: дефект не воспроизводится",
            {"files": len(files)},
        )

    file = _pick_file(context, non_ascii, ascii_only=False)
    probe = context.probe_request("GET", f"/api/data/file/{file.id}/download", binary=True)
    detail = _detail_text(probe)
    evidence: dict[str, Any] = {
        "file_id": str(file.id),
        "file_name": file.file_name,
        "status": probe.status,
        "detail": detail[:300],
        "size_registry": file.size,
        "error": probe.error or None,
    }
    if probe.status is None:
        return CheckOutcome(CheckStatus.FAILED, f"скачивание не отвечает: {probe.error}", evidence)
    if int(probe.status) == 404 and "latin-1" in detail:
        return CheckOutcome(
            CheckStatus.BLOCKED,
            "скачивание не-ASCII имени не работает: 404 latin-1 — замечание к API (P0), "
            "записи с такими именами недоступны пульту",
            evidence,
        )
    if int(probe.status) == 200:
        return CheckOutcome(
            CheckStatus.PASSED,
            f"не-ASCII имя «{file.file_name}» скачалось: дефект на этой сборке не воспроизводится",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.FAILED,
        f"не-ASCII имя «{file.file_name}»: ответ {probe.status} — {detail[:120]}",
        evidence,
    )


# ---------------------------------------------------------------------------
# Запись: загрузка CSV и круговой рейс (TC-FILE-11…14, класс `live`)
# ---------------------------------------------------------------------------
def _upload_invalid_csv(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-11: CSV без обязательных колонок отклоняется с текстом `detail` (UC-02)."""
    name = _test_file_name(context)
    if not name.startswith(TEST_PREFIX):
        return CheckOutcome(
            CheckStatus.FAILED,
            f"имя тестового файла должно начинаться с `{TEST_PREFIX}` (§9 ТЗ): {name}",
            {"file_name": name},
        )

    result = _upload_probe(context, file_name=name, body=INVALID_BODY)
    detail = _detail_text(result)
    payload = result.body_json if isinstance(result.body_json, dict) else {}
    evidence: dict[str, Any] = {
        "file_name": name,
        "status": result.status,
        "detail": detail[:300],
        "error": result.error or None,
        "request_bytes": len(INVALID_BODY.encode("utf-8")),
    }
    if result.status is None:
        return CheckOutcome(CheckStatus.FAILED, f"загрузка не выполнена: {result.error}", evidence)

    created_id = str(payload.get("id") or "")
    if created_id:
        # сервер принял файл без обязательных колонок: созданное убираем за собой (NFR-T4)
        _register_entity(context, entity_id=created_id, entity_type="файл RAW")
        deleted, cleanup_error = _delete_file(context, created_id)
        evidence["created_id"] = created_id
        evidence["cleanup"] = (
            "файл удалён" if deleted else f"самоочистка не выполнена: {cleanup_error}"
        )
        return CheckOutcome(
            CheckStatus.FAILED,
            f"CSV без обязательных колонок принят ({result.status}): ожидался 400 с текстом `detail`",
            evidence,
        )
    if int(result.status) == 400 and detail:
        return CheckOutcome(
            CheckStatus.PASSED,
            f"CSV без обязательных колонок отклонён: 400 «{detail[:120]}»",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.FAILED,
        f"ответ {result.status} без разобранного `detail`: ожидался 400 — {detail[:120]}",
        evidence,
    )


def _upload_wrong_mime(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-12: MIME части проверяется — `application/octet-stream` даёт 400 (замечание №5)."""
    name = _test_file_name(context)
    if not name.startswith(TEST_PREFIX):
        return CheckOutcome(
            CheckStatus.FAILED,
            f"имя тестового файла должно начинаться с `{TEST_PREFIX}` (§9 ТЗ): {name}",
            {"file_name": name},
        )

    result = _upload_probe(context, file_name=name, body=ROUND_TRIP_BODY, content_type=MIME_BINARY)
    detail = _detail_text(result)
    payload = result.body_json if isinstance(result.body_json, dict) else {}
    evidence: dict[str, Any] = {
        "file_name": name,
        "part_content_type": MIME_BINARY,
        "status": result.status,
        "detail": detail[:300],
        "error": result.error or None,
    }
    if result.status is None:
        return CheckOutcome(CheckStatus.FAILED, f"загрузка не выполнена: {result.error}", evidence)

    created_id = str(payload.get("id") or "")
    if created_id:
        _register_entity(context, entity_id=created_id, entity_type="файл RAW")
        deleted, cleanup_error = _delete_file(context, created_id)
        evidence["created_id"] = created_id
        evidence["cleanup"] = (
            "файл удалён" if deleted else f"самоочистка не выполнена: {cleanup_error}"
        )
        return CheckOutcome(
            CheckStatus.FAILED,
            f"часть с `{MIME_BINARY}` принята ({result.status}): MIME-тип не проверяется",
            evidence,
        )
    if int(result.status) == 400:
        return CheckOutcome(
            CheckStatus.PASSED,
            f"часть с `{MIME_BINARY}` отклонена: 400 «{detail[:120]}» — сервер проверяет MIME части",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.FAILED,
        f"ответ {result.status}: ожидался 400 «Only CSV files are allowed» — {detail[:120]}",
        evidence,
    )


def _round_trip(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-13: `upload → list → download → delete` без следов в реестре (UC-02…UC-05)."""
    name = _test_file_name(context)
    if not name.startswith(TEST_PREFIX):
        return CheckOutcome(
            CheckStatus.FAILED,
            f"имя тестового файла должно начинаться с `{TEST_PREFIX}` (§9 ТЗ): {name}",
            {"file_name": name},
        )

    body = context.param("csv_body") or ROUND_TRIP_BODY
    content = body.encode("utf-8")
    problems: list[str] = []
    evidence: dict[str, Any] = {
        "file_name": name,
        "request_bytes": len(content),
        "sha256_uploaded": _sha256(content),
    }

    uploaded = _upload_probe(context, file_name=name, body=body)
    evidence["upload_status"] = uploaded.status
    evidence["upload_detail"] = _detail_text(uploaded)[:200]
    if uploaded.status is None:
        return CheckOutcome(
            CheckStatus.FAILED, f"загрузка не выполнена: {uploaded.error}", evidence
        )
    if int(uploaded.status) >= 400:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"загрузка «{name}» отклонена ({uploaded.status}): {evidence['upload_detail']}",
            evidence,
        )

    payload = uploaded.body_json if isinstance(uploaded.body_json, dict) else {}
    file_id = str(payload.get("id") or "")
    evidence["file_id"] = file_id
    evidence["response"] = {key: str(value) for key, value in payload.items()}
    if not file_id:
        return CheckOutcome(
            CheckStatus.FAILED,
            "ответ загрузки не содержит `id`: файл создан, но управлять им нельзя",
            evidence,
        )

    _register_entity(
        context, entity_id=file_id, entity_type=f"файл {payload.get('file_type') or 'RAW'}"
    )

    try:
        listed: list[FileMetadataResponse] | None = list(
            context.files_api().list_files(file_id=file_id).files
        )
    except (ApiError, ClientError, ServerUnavailableError) as exc:
        listed = None
        problems.append(f"реестр после загрузки не отвечает: {exc}")
    evidence["in_registry"] = bool(listed) if listed is not None else None
    if listed is not None and not listed:
        problems.append("загруженный файл не найден в реестре фильтром по `id`")

    downloaded, error = _download(context, file_id)
    if downloaded is None:
        problems.append(f"скачивание загруженного файла не удалось: {error}")
    else:
        evidence["downloaded_bytes"] = len(downloaded)
        evidence["sha256_downloaded"] = _sha256(downloaded)
        if downloaded != content:
            problems.append("скачанные байты не совпадают с загруженными")

    deleted, delete_error = _delete_file(context, file_id)
    evidence["deleted"] = deleted
    if not deleted:
        problems.append(f"самоочистка не выполнена: {delete_error}")
    else:
        try:
            left: list[FileMetadataResponse] | None = list(
                context.files_api().list_files(file_id=file_id).files
            )
        except (ApiError, ClientError, ServerUnavailableError) as exc:
            left = None
            problems.append(f"проверка удаления не выполнена: {exc}")
        evidence["left_in_registry"] = len(left) if left is not None else None
        if left:
            problems.append("файл остался в реестре после удаления")

    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    return CheckOutcome(
        CheckStatus.PASSED,
        f"круговой рейс выполнен: «{name}» создан, найден в реестре, скачан "
        f"({len(content)} байт совпали), удалён — следов не осталось",
        evidence,
    )


def _delete_missing(context: AutomationContext) -> CheckOutcome:
    """TC-FILE-14: удаление несуществующего файла отвечает согласованной ошибкой (UC-05)."""
    unknown = context.param("unknown_file_id") or uuid4().hex
    probe = context.probe_request("DELETE", f"/api/{unknown}")
    detail = _detail_text(probe)
    evidence: dict[str, Any] = {
        "file_id": unknown,
        "status": probe.status,
        "detail": detail[:300],
        "error": probe.error or None,
        "note": "batch-удаления пары RAW+markup в API нет: уборка — два отдельных запроса",
    }
    if probe.status is None:
        return CheckOutcome(
            CheckStatus.FAILED, f"проба удаления не отвечает: {probe.error}", evidence
        )
    if 200 <= int(probe.status) <= 299:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"удаление несуществующего файла отвечает {probe.status}: ожидалась ошибка 404",
            evidence,
        )
    if 500 <= int(probe.status) <= 599:
        return CheckOutcome(
            CheckStatus.FAILED,
            f"удаление несуществующего файла отвечает {probe.status} (5xx)",
            evidence,
        )
    if int(probe.status) == 404:
        return CheckOutcome(
            CheckStatus.PASSED,
            f"удаление несуществующего файла: 404 «{detail[:120]}» — код согласован",
            evidence,
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"удаление несуществующего файла: {probe.status} «{detail[:120]}» — вариант зафиксирован",
        evidence,
    )


#: Сценарии проверок модуля «File Import»: ключ — `CheckSpec.automation`.
AUTOMATIONS = {
    "files.list_registry": _list_registry,
    "files.name_filter": _name_filter,
    "files.type_filter": _type_filter,
    "files.import_date_filter": _import_date_filter,
    "files.pagination": _pagination,
    "files.volume": _volume,
    "files.duplicates": _duplicates,
    "files.download_ascii": _download_ascii,
    "files.download_headers": _download_headers,
    "files.download_non_ascii": _download_non_ascii,
    "files.upload_invalid_csv": _upload_invalid_csv,
    "files.upload_wrong_mime": _upload_wrong_mime,
    "files.round_trip": _round_trip,
    "files.delete_missing": _delete_missing,
}
