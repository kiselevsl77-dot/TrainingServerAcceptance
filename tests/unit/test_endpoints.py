"""Тесты реестра эндпоинтов консоли запросов (FR-T3).

Главная проверка — **совпадение реестра со спецификацией**: блок `ENDPOINTS`
в `acceptance/endpoints.py` должен быть в точности тем, что генерирует
`tools/gen_endpoints.py` из `docs/SOM1.json`. Если спецификация изменится,
а реестр не пересобрали, тест это покажет.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acceptance import endpoints as ep
from tools.gen_endpoints import (
    BEGIN_MARKER,
    END_MARKER,
    TARGET_PATH,
    build_specs,
    differences,
    load_spec,
)

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "SOM1.json"
HTTP_METHODS = ("get", "post", "put", "delete", "patch")


@pytest.fixture(scope="module")
def spec() -> dict:
    """Спецификация испытуемого сервера (вендорная копия)."""
    return load_spec(SPEC_PATH)


def _operations(spec: dict) -> list[tuple[str, str]]:
    """Пары (метод в нижнем регистре, путь) всех операций спецификации."""
    return [
        (method, path)
        for path, item in spec["paths"].items()
        for method in item
        if method in HTTP_METHODS
    ]


def test_registry_matches_specification(spec: dict):
    """Реестр совпадает со спецификацией (защита от дрейфа реестра)."""
    text = TARGET_PATH.read_text(encoding="utf-8")

    assert BEGIN_MARKER in text
    assert END_MARKER in text
    # сверка по значениям (устойчива к форматированию кода в файле)
    assert differences(build_specs(spec), ep.ENDPOINTS) == []


def test_registry_covers_every_specification_operation(spec: dict):
    """В реестре есть все операции спецификации (32) и нет лишних."""
    keys = set(ep.endpoint_keys())
    expected = {f"{method} {path}" for method, path in _operations(spec)}

    assert keys == expected
    assert len(ep.ENDPOINTS) == 32


def test_modules_cover_specification_tags(spec: dict):
    """Модули реестра — теги спецификации."""
    tags = {
        operation["tags"][0]
        for item in spec["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and operation.get("tags")
    }

    assert set(ep.module_names()) == tags
    assert len(ep.MODULES) == 6
    assert all(ep.by_module(module) for module in ep.MODULES)


def test_path_parameters_declared_in_specification():
    """Каждый `{параметр}` пути объявлен как path-параметр операции."""
    for spec_ in ep.ENDPOINTS:
        placeholders = {
            part.split("{")[1].split("}")[0] for part in spec_.path.split("/") if "{" in part
        }
        declared = {param.name for param in spec_.path_params()}

        assert placeholders == declared, spec_.key


def test_safety_classification_matches_rules():
    """Классы безопасности: чтение, изменение, удаление, ресурсоёмкие."""
    read_keys = {spec.key for spec in ep.ENDPOINTS if spec.safety == ep.Safety.READ}
    destructive = [spec.path for spec in ep.ENDPOINTS if spec.is_destructive]
    heavy = {spec.path for spec in ep.ENDPOINTS if spec.is_heavy}

    assert heavy == {
        "/api/datasets/fill/{dataset_id}",
        "/api/ml_models/models/{model_id}/train",
        "/api/ml_models/{model_id}/check",
        "/api/ml_models/models/{model_id}/inference",
    }
    assert set(destructive) == {
        "/api/{file_id}",
        "/api/datasets/{dataset_id}",
        "/api/ml_models/models/{model_id}",
    }
    assert "get /health" in read_keys
    assert "post /api/ml_models/models/{model_id}/inference/single" in read_keys
    assert ep.find("post /api/tasks/test").safety == ep.Safety.WRITE
    assert not [
        spec_ for spec_ in ep.ENDPOINTS if spec_.method == "GET" and spec_.safety != ep.Safety.READ
    ]


def test_confirmation_and_test_prefix_flags():
    """Изменяющие/удаляющие операции требуют подтверждения и префикса `__TEST__`."""
    assert not ep.find("get /api/data/files").requires_confirmation
    assert ep.find("post /api/data/file").requires_confirmation
    assert not ep.find("post /api/data/file").is_binary
    assert ep.requires_test_prefix(ep.find("post /api/data/file"))
    assert ep.requires_test_prefix(ep.find("delete /api/{file_id}"))
    # обучение/проверка/инференс новых сущностей не создают — префикс не нужен
    assert not ep.requires_test_prefix(ep.find("post /api/ml_models/models/{model_id}/train"))


def test_find_by_key_and_operation_id():
    """Поиск операции по ключу и по `operationId` спецификации."""
    assert ep.find("get /version").path == "/version"
    assert ep.find("health_check_health_get").key == "get /health"
    assert ep.find("get /api/нет-такого") is None
    assert ep.by_operation_id("version_version_get").path == "/version"


def test_render_path_and_missing_parameters():
    """Подстановка path-параметров и контроль незаполненных."""
    download = ep.find("get /api/data/file/{file_id}/download")

    assert ep.render_path(download, {"file_id": "11111111-1111-4111-8111-111111111111"}) == (
        "/api/data/file/11111111-1111-4111-8111-111111111111/download"
    )
    assert ep.missing_path_params(download, {}) == ("file_id",)
    assert ep.missing_path_params(download, {"file_id": "x"}) == ()
    # незаполненный параметр остаётся в пути — запрос не должен уходить на сервер
    assert ep.render_path(download, {}) == "/api/data/file/{file_id}/download"


def test_match_path_recognizes_real_requests():
    """Сопоставление фактического запроса с операцией реестра (для отчёта)."""
    assert ep.match_path("GET", "/health").key == "get /health"
    assert ep.match_path("get", "/api/data/files").key == "get /api/data/files"
    assert ep.match_path("DELETE", "/api/abc-123").key == "delete /api/{file_id}"
    assert ep.match_path("GET", "/api/tasks/2f1b").key == "get /api/tasks/{task_id}"
    assert ep.match_path("GET", "/api/нет-такого") is None


def test_body_templates_are_filled_from_schemas():
    """Заготовки тел запросов: JSON из схем, multipart с полями формы."""
    dataset = ep.find("post /api/datasets/")
    upload = ep.find("post /api/data/file")
    version = ep.find("get /version")

    assert dataset.body_kind == ep.BODY_JSON
    assert ep.parse_body_sample(dataset) == {"name": "<name>", "type": "<type>"}
    assert upload.body_kind == ep.BODY_MULTIPART
    assert "file: binary" in upload.body_fields
    assert upload.body_sample == ""
    assert not version.has_body
    assert ep.parse_body_sample(version) == {}


def test_binary_downloads_are_marked_binary():
    """Скачивания помечены как файл (спецификация описывает их как JSON)."""
    for key in (
        "get /api/data/file/{file_id}/download",
        "get /api/ml_models/models/{model_id}/download_onnx",
    ):
        assert ep.find(key).is_binary, key


def test_known_endpoint_defects_are_documented():
    """Известные особенности эндпоинтов попадают в реестр (подсказки оператору)."""
    assert "latin-1" in ep.find("get /api/data/file/{file_id}/download").note
    assert "limit/offset" in ep.find("get /api/data/files").note
    assert "phase_connection" in ep.find("get /api/loads/list").note
    assert "task_id" in ep.find("post /api/datasets/fill/{dataset_id}").note
    assert sum(1 for spec_ in ep.ENDPOINTS if spec_.note) >= 10


def test_menu_labels_are_unique_and_descriptive():
    """Подписи операций для интерфейса уникальны и содержат метод с путём."""
    labels = [spec.menu_label for spec in ep.ENDPOINTS]

    assert len(labels) == len(set(labels))
    assert all(spec.method in spec.title and spec.path in spec.title for spec in ep.ENDPOINTS)
    assert ep.find("get /version").summary


def test_numeric_and_optional_parameters_are_described():
    """Числовые параметры пагинации распознаются, обязательных у чтения нет."""
    loads = ep.find("get /api/loads/list")
    numeric = {param.name for param in loads.query_params() if param.is_number}

    assert numeric == {"limit", "offset"}
    assert all(param.location == ep.QUERY for param in loads.query_params())
    assert ep.find("get /api/data/files").query_params()
    assert all(not param.required for param in ep.find("get /api/data/files").query_params())


def test_enum_and_default_parameters_are_detected():
    """Перечисления и значения по умолчанию берутся из спецификации."""
    tasks = ep.find("get /api/tasks/")
    task_type = next(param for param in tasks.params if param.name == "task_type")
    limit = next(param for param in tasks.params if param.name == "limit")

    assert task_type.is_enum
    assert "training" in task_type.enum
    assert "из перечня" in task_type.label
    assert limit.default == "100"
    assert limit.is_number


def test_registry_is_serializable_and_has_legend():
    """Операции описываются словарём — попадают в журнал и отчёт."""
    payload = json.dumps([spec_.to_dict() for spec_ in ep.ENDPOINTS], ensure_ascii=False)

    assert payload.count('"path"') == len(ep.ENDPOINTS)
    assert ep.safety_legend()[0]["safety"] == str(ep.Safety.READ)
    assert len(ep.safety_legend()) == len(ep.SAFETY_ORDER)


def test_parameter_format_comes_from_specification():
    """Формат параметра из спецификации виден оператору в консоли (этап T4).

    Стенд принимает `start_date`/`end_date` только как дату-время: строка из одной
    даты отвечает 422, поэтому подпись поля и подсказка обязаны называть формат.
    """
    endpoint = ep.find("get /api/tasks/")
    start = endpoint.param("start_date")
    limit = endpoint.param("limit")

    assert start is not None and limit is not None
    assert start.format == "date-time"
    assert "date-time" in start.label
    assert "YYYY-MM-DDTHH:MM:SS" in start.format_hint
    assert limit.format == ""
    assert limit.format_hint == ""
    assert limit.label == "limit (integer)"
