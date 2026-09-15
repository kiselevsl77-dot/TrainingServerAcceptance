"""Тесты клиента датасетов (FR-T5): пути, фильтры, тела запросов, наполнение.

Модуль добавлен в пульт (в основном UI он появится на этапе 3), поэтому тесты
фиксируют соответствие спецификации `SOM1.json`: фильтры списка `name`,
`creation_date`, `description`, `type`; состав датасета в ответе отсутствует
(замечание к API), а `fill` не возвращает `task_id`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from client.datasets import DatasetsApi
from client.errors import NotFoundError

DATASET = {
    "id": "6f2b4e6f-0000-4000-8000-000000000001",
    "creation_date": "2026-09-15T10:00:00",
    "name": "ds-test",
    "description": "датасет испытаний",
    "type": "direct_fill",
}


def test_list_datasets_passes_known_filters(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"datasets": [DATASET], "count": 1})

    response = DatasetsApi(client_factory(handler)).list_datasets(
        name="ds", dataset_type="direct_fill"
    )

    assert seen["path"] == "/api/datasets/"
    assert seen["query"] == "name=ds&type=direct_fill"
    assert response.count == 1
    assert response.datasets[0].name == "ds-test"


def test_list_datasets_without_filters_sends_no_query(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"datasets": [], "count": 0})

    DatasetsApi(client_factory(handler)).list_datasets()

    assert seen["query"] == ""


def test_create_dataset_payload(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json=DATASET)

    created = DatasetsApi(client_factory(handler)).create_dataset(
        name="ds-test", description="датасет испытаний"
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/datasets/"
    assert seen["body"] == {
        "name": "ds-test",
        "description": "датасет испытаний",
        "type": "direct_fill",
    }
    assert created.type == "direct_fill"


def test_update_dataset_sends_only_provided_fields(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=DATASET)

    DatasetsApi(client_factory(handler)).update_dataset(DATASET["id"], name="ds-renamed")

    assert seen["method"] == "PUT"
    assert seen["body"] == {"name": "ds-renamed"}


def test_delete_dataset_uses_dataset_path(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"message": "Dataset deleted successfully"})

    DatasetsApi(client_factory(handler)).delete_dataset(DATASET["id"])

    assert seen["method"] == "DELETE"
    assert seen["path"] == f"/api/datasets/{DATASET['id']}"


def test_fill_dataset_accepts_string_ids_and_sends_uuids(client_factory):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json={"status": "accepted"})

    raw_id = "11111111-1111-4111-8111-111111111111"
    markup_id = "22222222-2222-4222-8222-222222222222"

    DatasetsApi(client_factory(handler)).fill_dataset(
        DATASET["id"],
        raw_file_ids=[raw_id],
        markup_file_ids=[markup_id],
    )

    assert seen["path"] == f"/api/datasets/fill/{DATASET['id']}"
    assert seen["body"] == {"raw_file_ids": [raw_id], "markup_file_ids": [markup_id]}


def test_get_dataset_propagates_404(client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Dataset not found"})

    with pytest.raises(NotFoundError):
        DatasetsApi(client_factory(handler)).get_dataset(DATASET["id"])
