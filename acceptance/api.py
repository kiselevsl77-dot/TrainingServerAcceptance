"""Сборка клиента API с логирующим транспортом и снимок стенда (FR-T1, FR-T6).

`build_client` создаёт `ApiHttpClient` с `LoggingTransport`, поэтому **любой**
запрос пульта попадает в журнал (память + JSONL сессии). `Apis` — агрегат
сервисов по модулям `SOM1.json`. `take_stand_snapshot` собирает снимок стенда:
версию сервера и счётчики реестров на начало/конец сессии испытаний.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx

from acceptance.config import PultConfig
from acceptance.http_log import Journal, LoggingTransport
from acceptance.logging_setup import log_event
from acceptance.session import now_iso
from client.datasets import DatasetsApi
from client.errors import ClientError
from client.files import FilesApi
from client.http import ApiHttpClient
from client.loads import LoadsApi
from client.models import ModelsApi
from client.settings import TrainingServerSettings
from client.system import SystemApi
from client.tasks import TasksApi

#: Лимит записей, запрашиваемых для оценки размера реестров в снимке стенда.
SNAPSHOT_PAGE_LIMIT = 1


def build_client(
    settings: TrainingServerSettings,
    journal: Journal,
    *,
    config: PultConfig | None = None,
    inner: httpx.BaseTransport | None = None,
) -> ApiHttpClient:
    """Создаёт httpx-клиент, все запросы которого фиксируются в журнале.

    Args:
        settings: адрес и таймауты испытуемого сервера.
        journal: журнал обмена (память процесса пульта).
        config: настройки журналирования (лимит и сохранение тел).
        inner: подменяемый транспорт (используется в тестах).
    """
    resolved = config or PultConfig()
    transport = LoggingTransport(
        journal,
        inner=inner,
        body_limit=resolved.body_limit,
        log_bodies=resolved.log_bodies,
    )
    return ApiHttpClient(base_url=settings.base_url, timeout=settings.timeout, transport=transport)


@dataclass
class Apis:
    """Агрегат сервисов API по модулям спецификации."""

    system: SystemApi
    files: FilesApi
    loads: LoadsApi
    models: ModelsApi
    datasets: DatasetsApi
    tasks: TasksApi

    @classmethod
    def build(cls, client: ApiHttpClient, *, download_timeout: float | None = None) -> Apis:
        """Собирает сервисы поверх одного httpx-клиента."""
        return cls(
            system=SystemApi(client),
            files=FilesApi(client, download_timeout=download_timeout),
            loads=LoadsApi(client),
            models=ModelsApi(client),
            datasets=DatasetsApi(client),
            tasks=TasksApi(client),
        )


def take_stand_snapshot(apis: Apis) -> dict[str, Any]:
    """Снимок стенда: версия сервера и счётчики реестров (FR-T1).

    Ошибки отдельных запросов не прерывают снимок — они попадают в раздел
    `errors`, чтобы в отчёте было видно, что именно не удалось получить.
    """
    snapshot: dict[str, Any] = {
        "at": now_iso(),
        "version": None,
        "counts": {},
        "files": {},
        "errors": {},
    }

    try:
        snapshot["version"] = apis.system.version()
    except ClientError as exc:
        snapshot["errors"]["version"] = str(exc)

    try:
        files = apis.files.list_files().files
        names = Counter(file.file_name for file in files)
        snapshot["counts"]["files"] = len(files)
        snapshot["files"] = {
            "total_bytes": sum(file.size for file in files),
            "unique_names": len(names),
            "duplicate_names": sum(1 for count in names.values() if count > 1),
            "by_type": dict(Counter(file.file_type for file in files)),
        }
    except ClientError as exc:
        snapshot["errors"]["files"] = str(exc)

    try:
        snapshot["counts"]["loads"] = apis.loads.list_loads(limit=SNAPSHOT_PAGE_LIMIT).result_size
    except ClientError as exc:
        snapshot["errors"]["loads"] = str(exc)

    try:
        snapshot["counts"]["models"] = len(apis.models.list_models())
    except ClientError as exc:
        snapshot["errors"]["models"] = str(exc)

    try:
        snapshot["counts"]["datasets"] = apis.datasets.list_datasets().count
    except ClientError as exc:
        snapshot["errors"]["datasets"] = str(exc)

    try:
        snapshot["counts"]["tasks"] = apis.tasks.list_tasks(limit=SNAPSHOT_PAGE_LIMIT).count
    except ClientError as exc:
        snapshot["errors"]["tasks"] = str(exc)

    log_event(
        "stand_snapshot_taken",
        "Снимок стенда сформирован",
        module="stand",
        payload={
            "counts": snapshot["counts"],
            "files": snapshot["files"],
            "errors": snapshot["errors"],
            "server_build": _build_label(snapshot["version"]),
        },
    )
    return snapshot


def _build_label(version: Any) -> str:
    """Краткая подпись сборки сервера из ответа `GET /version`."""
    if not isinstance(version, dict):
        return "не определена"
    revision = str(version.get("revision") or version.get("commit") or "")[:12]
    branch = str(version.get("branch") or "")
    if revision and branch:
        return f"{branch}@{revision}"
    return revision or branch or "не определена"
