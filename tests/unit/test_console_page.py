"""Тесты правил подтверждения консоли запросов (FR-T3, NFR-T4).

Проверяются решения, которые пульт применяет **перед** вызовом изменяющих,
удаляющих и ресурсоёмких операций: подтверждение крупных скачиваний, контроль
префикса `__TEST__`, двойное подтверждение удаления и извлечение `task_id`.

Виджеты Streamlit подменяются: значения считываются по ключам виджетов, поэтому
проверка не зависит от интерфейса.
"""

from __future__ import annotations

import json

import pytest

from acceptance import endpoints as ep
from acceptance.exchange import ExchangeResult
from acceptance.ui.pages import console

BIG_FILE = "66666666-6666-4666-8666-666666666666"
TEST_FILE = "77777777-7777-4777-8777-777777777777"
SMALL_FILE = "22222222-2222-4222-8222-222222222222"


class _FakeFile:
    """Файл реестра с минимально нужными полями (`id`, `file_name`, `size`)."""

    def __init__(self, file_id: str, file_name: str, size: int) -> None:
        self.id = file_id
        self.file_name = file_name
        self.size = size


@pytest.fixture
def widgets(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, bool], dict[str, str]]:
    """Подменяет виджеты Streamlit значениями по ключам (`flags`, `texts`)."""
    flags: dict[str, bool] = {}
    texts: dict[str, str] = {}
    monkeypatch.setattr(console.st, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(console.st, "caption", lambda *args, **kwargs: None)
    monkeypatch.setattr(console.st, "error", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        console.st, "checkbox", lambda *args, key=None, **kwargs: flags.get(str(key), False)
    )
    monkeypatch.setattr(
        console.st, "text_input", lambda *args, key=None, **kwargs: texts.get(str(key), "")
    )
    return flags, texts


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет кэш реестра файлов (без обращения к стенду)."""
    files = [
        _FakeFile(BIG_FILE, "Antminer_S19_3h.raw.csv", 60 * 1024 * 1024),
        _FakeFile(TEST_FILE, "__TEST__probe.raw.csv", 120),
        _FakeFile(SMALL_FILE, "Antminer_S19.markup.csv", 100),
    ]
    monkeypatch.setattr(console.state, "load_files", lambda: (files, None))


def _gate(spec, *, path_values=None, json_body=None, multipart=None, session=None):
    """Вызов правил подтверждения для операции."""
    return console._safety_gate(
        spec,
        path_values=path_values or {},
        json_body=json_body,
        multipart=multipart,
        session=session,
    )


def test_read_operation_needs_no_confirmation(widgets, registry):
    """Читающая операция выполняется без подтверждений."""
    allowed, details, run_card = _gate(ep.find("get /api/data/files"))

    assert allowed
    assert details["confirmations"] == []
    assert run_card is None


def test_small_or_unknown_download_needs_no_confirmation(widgets, registry):
    """Небольшой файл (или файл вне реестра) скачивается без подтверждения."""
    download = ep.find("get /api/data/file/{file_id}/download")

    assert _gate(download, path_values={"file_id": SMALL_FILE})[0] is True
    assert _gate(download, path_values={"file_id": "нет-в-реестре"})[0] is True


def test_large_download_requires_confirmation(widgets, registry):
    """Файл крупнее 50 МБ скачивается только после подтверждения оператора."""
    flags, _ = widgets
    spec = ep.find("get /api/data/file/{file_id}/download")
    key = f"console_binary_confirm_{spec.key}"

    assert _gate(spec, path_values={"file_id": BIG_FILE})[0] is False

    flags[key] = True
    allowed, details, _ = _gate(spec, path_values={"file_id": BIG_FILE})

    assert allowed
    assert "подтверждение скачивания крупного файла" in details["confirmations"]


def test_write_operation_requires_confirmation_and_test_prefix(widgets, registry):
    """Создание сущности: подтверждение + префикс `__TEST__` у имени."""
    flags, _ = widgets
    spec = ep.find("post /api/datasets/")
    key = f"console_confirm_{spec.key}"

    assert _gate(spec, json_body={"name": "dataset", "type": "direct_fill"})[0] is False
    assert _gate(spec, json_body={"name": "__TEST__dataset"})[0] is False, (
        "без подтверждения запуск запрещён"
    )

    flags[key] = True
    allowed, details, _ = _gate(spec, json_body={"name": "__TEST__dataset"})

    assert allowed
    assert details["names"] == ["__TEST__dataset"]


def test_load_creation_checks_title_and_category(widgets, registry):
    """У нагрузки контролируются оба имени: `title` и `category`."""
    flags, _ = widgets
    spec = ep.find("post /api/loads")
    flags[f"console_confirm_{spec.key}"] = True

    allowed, details, _ = _gate(
        spec, json_body={"title": "__TEST__load", "category": "свободный текст"}
    )

    assert allowed is False
    assert "свободный текст" in details["names"]


def test_write_without_created_names_needs_only_confirmation(widgets, registry):
    """Управление задачей новых сущностей не создаёт — достаточно подтверждения."""
    flags, _ = widgets
    spec = ep.find("post /api/tasks/{task_id}/pause")

    assert _gate(spec, path_values={"task_id": "t-1"})[0] is False

    flags[f"console_confirm_{spec.key}"] = True
    assert _gate(spec, path_values={"task_id": "t-1"})[0] is True


def test_destructive_with_test_entity_needs_delete_word_only(widgets, registry):
    """Удаление `__TEST__`-сущности: чекбокс + слово DELETE (без ввода id)."""
    flags, texts = widgets
    spec = ep.find("delete /api/{file_id}")

    flags[f"console_confirm_{spec.key}"] = True
    texts[f"console_delete_word_{spec.key}"] = "delete"

    allowed, details, _ = _gate(spec, path_values={"file_id": TEST_FILE})

    assert allowed
    assert details["names"] == ["__TEST__probe.raw.csv"]
    assert "id подтверждён" not in details["confirmations"]


def test_destructive_of_foreign_entity_requires_id(widgets, registry):
    """Удаление сущности без префикса `__TEST__`: двойное подтверждение с вводом id."""
    flags, texts = widgets
    spec = ep.find("delete /api/{file_id}")
    flags[f"console_confirm_{spec.key}"] = True
    texts[f"console_delete_word_{spec.key}"] = "DELETE"

    allowed, _, _ = _gate(spec, path_values={"file_id": SMALL_FILE})
    assert allowed is False, "без повторного ввода id удаление запрещено"

    texts[f"console_delete_id_{spec.key}"] = SMALL_FILE
    allowed, details, _ = _gate(spec, path_values={"file_id": SMALL_FILE})

    assert allowed
    assert "id подтверждён" in details["confirmations"]


def test_heavy_operation_requires_run_card(widgets, monkeypatch: pytest.MonkeyPatch):
    """Ресурсоёмкая операция запускается только с заполненной карточкой (FR-T10)."""
    card = console.RunCard(goal="проверить обучение", data="датасет x", responsible="Иванов И.И.")
    monkeypatch.setattr(console, "render_run_card", lambda spec, **kwargs: card)
    spec = ep.find("post /api/ml_models/models/{model_id}/train")

    allowed, _, returned = _gate(spec, path_values={"model_id": "m-1"}, json_body={})
    assert allowed is False
    assert returned is card

    card.confirmed = True
    allowed, details, _ = _gate(spec, path_values={"model_id": "m-1"}, json_body={})

    assert allowed
    assert details["run_card"]["goal"] == "проверить обучение"
    assert "подтверждение расхода ресурсов" in details["confirmations"]


def test_created_names_collects_body_fields_and_file():
    """Имена создаваемых сущностей берутся из тела JSON и multipart-файла."""
    names = console._created_names(
        json_body={"name": "__TEST__d", "type": "direct_fill", "batch_size": 8},
        multipart=console.MultipartPayload(file_name="__TEST__model.h5", content=b"x"),
    )

    assert set(names) == {"__TEST__d", "__TEST__model.h5"}


def test_task_id_is_extracted_from_response():
    """Идентификатор созданной задачи извлекается из ответа (для монитора задач T4)."""
    result = ExchangeResult(method="POST", path="/api/datasets/fill/d-1")

    result.body_json = {"task_id": "task-42", "status": "new"}
    assert console._task_id_from(result) == "task-42"

    result.body_json = {"id": "task-43"}
    assert console._task_id_from(result) == "task-43"

    result.body_json = {"detail": "нет"}
    assert console._task_id_from(result) == ""

    result.body_json = None
    assert console._task_id_from(result) == ""


def test_extra_query_parameters_are_parsed():
    """Свободные query-параметры разбираются построчно (`key=value`)."""
    parsed = console._parse_extra_query("limit=1\n offset = 20 \n\nбез-значения\nfile_type=RAW")

    assert parsed == {
        "limit": "1",
        "offset": "20",
        "без-значения": "",
        "file_type": "RAW",
    }


def test_call_note_describes_confirmations_and_run_card():
    """Примечание вызова содержит подтверждения, имена и карточку запуска."""
    card = console.RunCard(
        goal="цель",
        data="данные",
        responsible="Петров П.П.",
        artifacts="оставить как доказательство",
    )
    details = {"confirmations": ["подтверждение изменяющей операции"], "names": ["__TEST__x"]}

    note = console._call_note(details, card)

    assert "цель" in note
    assert "Петров П.П." in note
    assert "подтверждение изменяющей операции" in note
    assert "__TEST__x" in note
    assert json.dumps(details, ensure_ascii=False)
