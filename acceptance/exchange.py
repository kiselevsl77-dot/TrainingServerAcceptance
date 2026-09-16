"""Выполнение произвольного запроса консоли и запись об обмене (FR-T3, FR-T6).

`client.http.ApiHttpClient` отдаёт наружу только **раскодированное тело** ответа,
а консоли испытаний нужны ещё статус, заголовки, длительность, размер и сырое тело
(в том числе бинарное — скачивание файлов и ONNX-моделей). Поэтому консоль работает
«сырым» `httpx.Client`, обёрнутым в **тот же** `LoggingTransport`, что и остальные
экраны: любой вызов консоли попадает в `Journal` и в JSONL сессии наравне с
запросами экранов и проверок (требование FR-T3), включая усечённые тела, размеры
и ошибки соединения.

Особенности, важные для испытаний:

    * `execute_request` **никогда не бросает исключений**: сетевые сбои, 4xx/5xx и
      некорректные URL возвращаются полями результата — оператор видит их в UI,
      а проверка/замечание к API формируются из `ExchangeResult`;
    * тело читается с подбором кодировки (`utf-8` → `cp1251` → `latin-1`), поэтому
      дефект `latin-1` виден как есть, а не как «кракозябры» интерпретатора;
    * бинарные ответы не сохраняются в журнал телами — только размер и имя файла
      из `Content-Disposition` (`PULT_BODY_LIMIT` относится к текстовым телам).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from acceptance.config import DEFAULT_BODY_LIMIT, PultConfig
from acceptance.http_log import Journal, LoggingTransport, label_context
from client.files import guess_content_type
from client.http import error_detail, filename_from_content_disposition
from client.settings import TrainingServerSettings

#: Классы ошибок вызова (для UI, замечаний к API и отчёта).
ERROR_NONE = ""
ERROR_NETWORK = "network"
ERROR_NOT_FOUND = "not_found"
ERROR_VALIDATION = "validation"
ERROR_API = "api"
ERROR_REQUEST = "request"

ERROR_KIND_LABELS: dict[str, str] = {
    ERROR_NONE: "ответ получен",
    ERROR_NETWORK: "сервер недоступен (сеть, таймаут)",
    ERROR_NOT_FOUND: "ресурс не найден (404)",
    ERROR_VALIDATION: "ошибка валидации запроса (422)",
    ERROR_API: "ошибка API (4xx/5xx)",
    ERROR_REQUEST: "запрос не отправлен (некорректные параметры)",
}

#: Маркеры текстовых `Content-Type` (остальное считается файлом).
TEXT_MARKERS = ("json", "text", "csv", "xml", "javascript", "urlencoded", "html")

#: Кодировки, пробуемые при чтении тела (по порядку).
CHARSET_FALLBACKS = ("utf-8", "cp1251", "latin-1")

#: Заголовки, важные оператору при разборе результата.
IMPORTANT_HEADERS = (
    "content-type",
    "content-length",
    "content-disposition",
    "content-encoding",
    "etag",
    "last-modified",
    "location",
    "server",
    "date",
    "retry-after",
    "www-authenticate",
)


def charset_from_content_type(content_type: str | None) -> str:
    """Кодировка из `Content-Type` (`charset=...`), если она указана."""
    if not content_type:
        return ""
    for part in content_type.split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"').lower()
    return ""


def is_text_content(content_type: str | None) -> bool:
    """True, если тело такого типа имеет смысл показывать текстом."""
    if not content_type:
        return False
    lowered = content_type.lower()
    return any(marker in lowered for marker in TEXT_MARKERS)


def decode_body(content: bytes, content_type: str | None = None) -> tuple[str, str]:
    """Читает тело ответа, подбирая кодировку.

    Returns:
        Кортеж (текст, использованная кодировка). Последний резерв — `latin-1`,
        который декодирует любые байты (поэтому дефект сервера с `latin-1`
        виден именно как текст, а не как ошибка декодирования).
    """
    declared = charset_from_content_type(content_type)
    candidates = ([declared] if declared else []) + [
        encoding for encoding in CHARSET_FALLBACKS if encoding != declared
    ]
    for encoding in candidates:
        try:
            return content.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace"), "utf-8/replace"


def parse_json(text: str) -> Any:
    """Разбирает JSON-тело (None, если тело не JSON)."""
    try:
        return json.loads(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class MultipartPayload:
    """Файл для multipart-запроса (загрузка данных, модели)."""

    file_name: str
    content: bytes
    file_type: str = ""
    description: str = ""
    field: str = "file"

    @property
    def content_type(self) -> str:
        """MIME-тип части с файлом (совпадает с логикой `client.files`)."""
        return guess_content_type(self.file_name)

    def as_files(self) -> dict[str, tuple[str, bytes, str]]:
        """Части multipart-запроса в формате httpx."""
        return {self.field: (self.file_name, self.content, self.content_type)}

    def form_data(self) -> dict[str, str]:
        """Текстовые поля формы (тип файла, описание)."""
        data: dict[str, str] = {}
        if self.file_type:
            data["file_type"] = self.file_type
        if self.description:
            data["description"] = self.description
        return data


@dataclass
class ExchangeResult:
    """Результат одного вызова консоли: запрос, ответ (или ошибка), измерения."""

    method: str
    path: str
    label: str = ""
    query: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    status: int | None = None
    duration_ms: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = ""
    body_text: str = ""
    body_json: Any = None
    body_truncated: bool = False
    size_bytes: int = 0
    binary: bool = False
    file_name: str | None = None
    encoding: str = ""
    request_body: str = ""
    error: str = ""
    error_kind: str = ERROR_NONE
    journal_seq: int | None = None
    content: bytes | None = None

    @property
    def ok(self) -> bool:
        """True, если сервер ответил статусом < 400."""
        return self.status is not None and self.status < 400

    @property
    def is_error(self) -> bool:
        """True для сетевых ошибок и ответов 4xx/5xx."""
        return not self.ok

    @property
    def error_label(self) -> str:
        """Человекочитаемый класс ошибки вызова."""
        return ERROR_KIND_LABELS.get(self.error_kind, self.error_kind or "ошибка")

    @property
    def status_label(self) -> str:
        """Статус ответа для интерфейса («нет ответа» при сетевой ошибке)."""
        return str(self.status) if self.status is not None else "нет ответа"

    @property
    def url(self) -> str:
        """Путь с query-строкой (без схемы и хоста)."""
        if not self.query:
            return self.path
        return f"{self.path}?{urlencode(self.query)}"

    @property
    def one_line(self) -> str:
        """Краткая строка результата (для истории консоли и журнала)."""
        outcome = self.error if self.error else self.status_label
        return (
            f"{self.method} {self.url} → {outcome} · {self.duration_ms:.0f} мс · "
            f"{self.size_bytes} байт"
        )

    def important_headers(self) -> dict[str, str]:
        """Только значимые заголовки (полный список — в `headers`)."""
        return {key: value for key, value in self.headers.items() if key in IMPORTANT_HEADERS}

    def body_preview(self, limit: int = DEFAULT_BODY_LIMIT) -> str:
        """Тело ответа для показа оператору (с пометкой об усечении)."""
        if self.binary:
            name = self.file_name or "файл"
            return f"<файл: {name}, {self.size_bytes} байт, {self.content_type or 'тип не указан'}>"
        if len(self.body_text) <= limit:
            return self.body_text
        return f"{self.body_text[:limit]}\n… (показано {limit} из {len(self.body_text)} символов)"

    def matches(self, needle: str) -> bool:
        """True, если подстрока встречается в результатах вызова (фильтр истории)."""
        haystack = " ".join(
            [self.method, self.url, self.status_label, self.error, self.label]
        ).lower()
        return needle.strip().lower() in haystack

    def as_dict(self) -> dict[str, Any]:
        """Представление для журнала, сессии и отчёта (без содержимого файлов)."""
        return {
            "method": self.method,
            "path": self.path,
            "query": dict(self.query),
            "label": self.label,
            "started_at": self.started_at,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "encoding": self.encoding,
            "binary": self.binary,
            "file_name": self.file_name,
            "body_truncated": self.body_truncated,
            "request_body": self.request_body,
            "error": self.error,
            "error_kind": self.error_kind,
            "journal_seq": self.journal_seq,
        }

    def as_curl(self, base_url: str = "") -> str:
        """Команда `curl` для воспроизведения запроса (в замечание к API, в отчёт)."""
        target = f"{base_url.rstrip('/')}{self.url}"
        parts = [f"curl -X {self.method} '{target}'"]
        if self.request_body.startswith("<multipart"):
            parts.append(f"-F 'file=@<файл>'  # {self.request_body.strip('<>')}")
        elif self.request_body:
            parts.append("-H 'Content-Type: application/json'")
            parts.append(f"-d '{self.request_body}'")
        return " \\\n  ".join(parts)


def build_console_client(
    settings: TrainingServerSettings,
    journal: Journal,
    *,
    config: PultConfig | None = None,
    inner: httpx.BaseTransport | None = None,
    timeout: float | None = None,
) -> httpx.Client:
    """Создаёт «сырой» httpx-клиент консоли с логирующим транспортом.

    В отличие от `ApiHttpClient`, отдаёт объект ответа целиком (статус, заголовки,
    тело), но журналируется тем же `LoggingTransport` — записи консоли видны в
    журнале пульта и в JSONL сессии наравне с запросами экранов.
    """
    resolved = config or PultConfig()
    transport = LoggingTransport(
        journal,
        inner=inner,
        body_limit=resolved.body_limit,
        log_bodies=resolved.log_bodies,
    )
    return httpx.Client(
        base_url=settings.base_url.rstrip("/"),
        timeout=timeout if timeout is not None else settings.timeout,
        transport=transport,
        follow_redirects=False,
    )


def execute_request(
    client: httpx.Client,
    *,
    method: str,
    path: str,
    query: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    multipart: MultipartPayload | None = None,
    label: str = "",
    binary: bool = False,
    timeout: float | None = None,
    body_limit: int = DEFAULT_BODY_LIMIT,
    journal: Journal | None = None,
    keep_content: bool = False,
) -> ExchangeResult:
    """Выполняет запрос консоли и возвращает результат (исключений не бросает).

    Args:
        client: httpx-клиент консоли (`build_console_client`).
        method: HTTP-метод операции.
        path: путь запроса (path-параметры уже подставлены).
        query: query-параметры (пустые значения отбрасываются).
        json_body: JSON-тело запроса.
        multipart: файл и текстовые поля multipart-формы.
        label: метка проверки чек-листа (`TC-…`) — попадает во все записи журнала.
        binary: тело ответа — файл (скачивание), показывается как размер и имя.
        timeout: таймаут вызова (для скачиваний — `TRAINING_SERVER_DOWNLOAD_TIMEOUT`).
        body_limit: лимит показываемого текстового тела (`PULT_BODY_LIMIT`).
        journal: журнал обмена — для привязки результата к номеру записи.
        keep_content: сохранить тело ответа в результате (для сохранения артефакта).
    """
    params = {
        str(key): str(value) for key, value in (query or {}).items() if value not in (None, "")
    }
    result = ExchangeResult(
        method=method.strip().upper(),
        path=path,
        label=label,
        query=params,
        started_at=datetime.now().isoformat(timespec="seconds"),
        request_body=_request_body_text(json_body=json_body, multipart=multipart),
    )

    kwargs: dict[str, Any] = {}
    if params:
        kwargs["params"] = params
    if multipart is not None:
        kwargs["files"] = multipart.as_files()
        form = multipart.form_data()
        if form:
            kwargs["data"] = form
    elif json_body is not None:
        kwargs["json"] = dict(json_body)
    if timeout is not None:
        kwargs["timeout"] = timeout

    started = time.perf_counter()
    with label_context(label):
        try:
            response = client.request(result.method, path, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError) as exc:
            # сетевая ошибка/некорректный запрос: обмен уже записан транспортом
            result.duration_ms = (time.perf_counter() - started) * 1000
            result.error = f"{type(exc).__name__}: {exc}"
            result.error_kind = ERROR_NETWORK if isinstance(exc, httpx.HTTPError) else ERROR_REQUEST
            result.journal_seq = _last_seq(journal, result.method, path)
            return result

    return _read_response(
        response,
        result=result,
        duration_ms=(time.perf_counter() - started) * 1000,
        binary=binary,
        body_limit=body_limit,
        keep_content=keep_content,
        journal=journal,
    )


def _read_response(
    response: httpx.Response,
    *,
    result: ExchangeResult,
    duration_ms: float,
    binary: bool,
    body_limit: int,
    keep_content: bool,
    journal: Journal | None,
) -> ExchangeResult:
    """Разбирает ответ сервера в результат вызова консоли."""
    result.duration_ms = duration_ms
    result.status = response.status_code
    result.headers = {key.lower(): value for key, value in response.headers.items()}
    result.content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    content = response.content
    result.size_bytes = len(content)
    result.journal_seq = _last_seq(journal, result.method, result.path)

    if response.status_code >= 400:
        result.error = error_detail(response)
        result.error_kind = error_kind_for_status(response.status_code)

    if response.status_code == 204 or not content:
        return result

    if keep_content:
        result.content = content

    if binary or not is_text_content(result.content_type):
        result.binary = True
        result.file_name = filename_from_content_disposition(
            response.headers.get("content-disposition")
        )
        return result

    text, encoding = decode_body(content, response.headers.get("content-type"))
    result.encoding = encoding
    result.body_truncated = len(text) > body_limit
    result.body_text = text[:body_limit]
    if "json" in result.content_type:
        result.body_json = parse_json(result.body_text if not result.body_truncated else text)
    return result


def error_kind_for_status(status: int) -> str:
    """Класс ошибки по статусу ответа."""
    if status == 404:
        return ERROR_NOT_FOUND
    if status == 422:
        return ERROR_VALIDATION
    return ERROR_API


def _request_body_text(
    *, json_body: Mapping[str, Any] | None, multipart: MultipartPayload | None
) -> str:
    """Текстовое представление тела запроса (для журнала, замечаний и отчёта)."""
    if multipart is not None:
        fields = ", ".join(f"{key}={value}" for key, value in multipart.form_data().items())
        suffix = f", поля: {fields}" if fields else ""
        return f"<multipart: {multipart.file_name}, {len(multipart.content)} байт{suffix}>"
    if json_body is not None:
        return json.dumps(dict(json_body), ensure_ascii=False)
    return ""


def _last_seq(journal: Journal | None, method: str, path: str) -> int | None:
    """Номер последней записи журнала по этому запросу (привязка к отчёту)."""
    if journal is None:
        return None
    for record in reversed(journal.records):
        if record.method == method and record.path == path:
            return record.seq
    return None
