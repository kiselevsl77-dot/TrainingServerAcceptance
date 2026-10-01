"""Тесты правил карточки модели (контекст К2, Э1 плана `llm_tasks/59`).

Структуры в тестах — с живого стенда (01.10.2026), а не выдуманные: карточка обязана
работать с тем, что реально приходит в `GET /api/ml_models/models`. Проверяется то, что
ломается незаметно: группы сигналов (их число сверяется с `output_size` при обучении),
классы, история обучения и проверок, статистика датасета и отбор «конкретной» модели.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from client.schemas import ModelMetadata
from pult import models as models_api


def _model(**overrides: Any) -> dict[str, Any]:
    """Модель в форме ответа стенда: сигналы, классы, слои и метрики."""
    payload: dict[str, Any] = {
        "id": "3d2d1139-7d05-4874-98b6-df69de77259e",
        "name": "stft_mlp-skfu-train-dev (trained)",
        "architecture": "stft_mlp",
        "version": "2",
        "phase_type": "three",
        "output_size": 2,
        "description": "боевая модель стенда",
        "file_id": "11111111-2222-4333-8444-555555555555",
        "created_at": "2026-09-14T19:00:00",
        "updated_at": "2026-09-21T15:14:26",
        "signals": [
            {
                "signal_num": 0,
                "device_id": "Antminer S19",
                "device_info": {"load_id": "Antminer S19", "category": "Номинальная нагрузка"},
            },
            {
                "signal_num": 0,
                "device_id": "Whatsminer M30S",
                "device_info": {"load_id": "Whatsminer M30S", "category": "Номинальная нагрузка"},
            },
            {
                "signal_num": 1,
                "device_id": "__OTHER__",
                "device_info": {"load_id": "__OTHER__", "category": "Прочая нагрузка"},
            },
        ],
        "signal_aliases": {"0": "miner", "1": "not_miner"},
        "config": {
            "layers": [
                {"layer_type": "input", "parameters": {"size": 64}},
                {"layer_type": "dense", "parameters": {"units": 32, "activation": "relu"}},
                {"layer_type": "dense", "parameters": {"units": 2}},
            ]
        },
    }
    payload.update(overrides)
    return payload


def _trained(**overrides: Any) -> dict[str, Any]:
    """Обученная модель: метрики с живого стенда (`loss`, `accuracy`, состав датасета)."""
    payload = _model()
    payload["metrics"] = {
        "dataset_id": "06b5d3f3-eedb-450f-afc8-2ef33f6b7cf9",
        "dataset_stats": {
            "total_chunks": 386869,
            "valid_chunks": 386869,
            "filtered_chunks": 386869,
            "loads_count": 52,
            "loads": {"Antminer S19": 10},
        },
        "training_history": {"loss": [0.06698943079180995]},
        "testing_history": {
            "tests": [
                {
                    "accuracy": 0.9765274365274366,
                    "report_file_id": "2222aeab-0fbc-4112-8e7b-393957e68fcf",
                    "dataset_id": "3f403a48-1d11-4df8-8885-4773ad41bac4",
                    "tested_at": "2026-09-15T08:26:25.522875",
                }
            ]
        },
        "original_model_id": "9c0d2c25-1f11-4c0c-9b1e-6ceb0ee0f0f1",
        "batch_size": 64,
        "epochs": 1,
        "learning_rate": 0.001,
        "preprocessing_converter": "stft",
        "use_streaming": True,
        "memory_usage": {"rss_mb": 512.5, "vms_mb": 1024.0, "percent": 12.5},
        "augmentation_enabled": False,
    }
    payload.update(overrides)
    return payload


def test_signal_groups_are_grouped_by_signal_num() -> None:
    """Группы сигналов собираются по `signal_num`: число групп — как `output_size`."""
    groups = models_api.signal_groups(_model())

    assert [group["signal_num"] for group in groups] == ["0", "1"]
    assert groups[0]["count"] == 2
    assert groups[0]["devices"] == ["Antminer S19", "Whatsminer M30S"]
    assert groups[0]["categories"] == ["Номинальная нагрузка"]
    assert groups[1]["devices_text"] == "__OTHER__"


def test_signal_groups_tolerate_empty_and_foreign_values() -> None:
    """Пустые и чужие значения не ломают карточку: модель без сигналов — не ошибка."""
    assert models_api.signal_groups(_model(signals=[])) == []
    assert models_api.signal_groups(_model(signals=None)) == []
    assert models_api.signal_groups(_model(signals=["строка", {"device_id": ""}])) == []


def test_classes_layers_and_metrics_summary() -> None:
    """Классы, слои и сводка метрик читаются в «человеческом» виде."""
    card = _trained()

    assert models_api.class_rows(card) == [
        {"signal_num": "0", "alias": "miner"},
        {"signal_num": "1", "alias": "not_miner"},
    ]

    layers = models_api.layer_rows(card)
    assert [layer["layer_type"] for layer in layers] == ["input", "dense", "dense"]
    assert layers[1]["parameters"] == "units=32, activation=relu"

    summary = models_api.metrics_summary(card)
    labels = {row["label"]: row["value"] for row in summary["rows"]}
    assert summary["trained"] is True
    assert summary["label"] == models_api.TRAINED_LABEL
    assert labels["Препроцессинг"] == "stft"
    assert labels["Потоковая загрузка"] == "включена"
    assert "Аугментация" not in labels, "ложная аугментация не показывается"
    assert labels["Память (RSS, МБ)"] == "512.5"
    assert summary["accuracy_label"] == "0.9765"
    assert summary["origin_label"] == models_api.RESULT_LABEL


def test_untrained_model_is_honest_about_missing_metrics() -> None:
    """Модель без метрик честно называется «не обучена», а не показывает нули."""
    summary = models_api.metrics_summary(_model())

    assert summary["trained"] is False
    assert summary["label"] == models_api.NOT_TRAINED_LABEL
    assert summary["rows"] == []
    assert summary["accuracy_label"] == ""
    assert summary["dataset_stats"] == []


def test_training_and_testing_history_are_renderable_rows() -> None:
    """Истории обучения и проверок превращаются в строки; чужая форма не роняет карточку."""
    card = _trained()

    assert models_api.training_rows(card) == [{"epoch": "1", "loss": "0.0669894"}]
    assert models_api.testing_rows(card)[0]["accuracy"] == "0.9765"
    assert models_api.testing_rows(card)[0]["dataset_id"] == "3f403a48-1d11-4df8-8885-4773ad41bac4"

    assert models_api.training_rows(_model()) == []
    broken = _trained()
    broken["metrics"]["training_history"] = {"loss": "нет"}
    broken["metrics"]["testing_history"] = {"tests": ["строка"]}
    assert models_api.training_rows(broken) == []
    assert models_api.testing_rows(broken) == []


def test_dataset_stats_show_only_scalars() -> None:
    """Статистика датасета печатает числа и прячет вложенные объекты (`loads`)."""
    rows = models_api.dataset_stat_rows(_trained())

    labels = [row["label"] for row in rows]
    assert "total_chunks" in labels
    assert "loads" not in labels, "вложенный объект в карточке не печатается"
    assert next(row["value"] for row in rows if row["label"] == "loads_count") == "52"


def test_filter_models_selects_by_properties() -> None:
    """Отбор «конкретной» модели работает по свойствам: обученные и свои — первыми."""
    battle = _model(
        id="b-1",
        name="boevaya-stft",
        architecture="stft_mlp",
        output_size=2,
        description="боевая модель стенда",
    )
    trained = _trained(
        id="t-1",
        name="__TEST__stft_mlp-train-dev",
        updated_at="2026-09-21",
        description="модель испытаний",
    )
    other = _model(
        id="o-1",
        name="cnn-2",
        architecture="cnn",
        output_size=3,
        phase_type="single",
        description="экспериментальная ветка",
    )
    registry = [battle, other, trained]

    assert [item["id"] for item in models_api.filter_models(registry)] == ["t-1", "b-1", "o-1"]
    assert [item["id"] for item in models_api.filter_models(registry, only_trained=True)] == ["t-1"]
    assert [item["id"] for item in models_api.filter_models(registry, only_battle=True)] == [
        "b-1",
        "o-1",
    ]
    assert [item["id"] for item in models_api.filter_models(registry, only_test=True)] == ["t-1"]
    assert [item["id"] for item in models_api.filter_models(registry, architecture="cnn")] == [
        "o-1"
    ]
    assert [item["id"] for item in models_api.filter_models(registry, output_size="3")] == ["o-1"]
    assert [item["id"] for item in models_api.filter_models(registry, phase_type="single")] == [
        "o-1"
    ]
    assert [item["id"] for item in models_api.filter_models(registry, query="БОЕВАЯ")] == ["b-1"]
    assert [item["id"] for item in models_api.filter_models(registry, query="o-1")] == ["o-1"]
    assert models_api.filter_models(registry, query="нет-такой") == []


def test_filter_options_are_built_from_registry() -> None:
    """Варианты фильтров строятся по реестру: показываем то, что на стенде есть."""
    options = models_api.filter_options([_model(), _model(architecture="cnn", output_size=3)])

    assert [item["value"] for item in options["architectures"]] == ["cnn", "stft_mlp"]
    assert [item["value"] for item in options["output_sizes"]] == ["2", "3"]
    assert [item["value"] for item in options["phases"]] == ["three"]


def test_card_collects_choice_state_and_file_size() -> None:
    """Карточка помечает выбранную модель и находит размер `.h5` в реестре файлов."""
    session = SimpleNamespace(ml_profile={"model_id": "t-1"})
    file_row = SimpleNamespace(id="f-1", file_name="model.h5", size=2048)

    card = models_api.card(_trained(id="t-1", file_id="f-1"), file=file_row, session=session)

    assert card["chosen"] is True
    assert card["file"]["size"] == 2048
    assert card["file"]["known"] is True
    assert card["file"]["name"] == "model.h5"
    assert card["signal_count"] == 2
    assert card["trained_label"] == models_api.TRAINED_LABEL
    assert {"label": "Имя", "value": "stft_mlp-skfu-train-dev (trained)"} in card["rows"]

    foreign = models_api.card(_trained(id="t-1"), session=session)
    assert foreign["chosen"] is True, "выбор виден и без реестра файлов"
    assert foreign["file"]["known"] is False


def test_row_marks_owner_and_training_state() -> None:
    """Строка таблицы различает свои (`__TEST__`) и боевые модели, обученные и нет."""
    own = models_api.row(_trained(name="__TEST__stft_mlp-train-dev"))
    battle = models_api.row(_model(id="b-1", name="boevaya-stft"))

    assert own["test_model"] is True and own["owner"] == "испытания"
    assert battle["test_model"] is False and battle["owner"] == "боевая"
    assert own["trained"] is True and battle["trained"] is False
    assert own["accuracy"] == "0.9765"
    assert battle["accuracy"] == ""
    assert own["signal_count"] == 2


def test_rules_work_with_stand_schema_objects() -> None:
    """Правила принимают и pydantic-модель стенда (`ModelMetadata`), а не только словарь."""
    model = ModelMetadata.model_validate(_trained())

    assert models_api.model_name(model).startswith("stft_mlp")
    assert models_api.signal_groups(model)[1]["devices"] == ["__OTHER__"]
    assert models_api.metrics_summary(model)["accuracy_label"] == "0.9765"
    assert models_api.row(model)["owner"] == "боевая"


def test_chosen_model_resolves_profile_choice() -> None:
    """Выбранная в профиле модель находится в реестре; снятый выбор даёт None."""
    registry = [_model(id="b-1"), _trained(id="t-1")]

    assert models_api.chosen_model(registry, {"model_id": "t-1"})["id"] == "t-1"
    assert models_api.chosen_model(registry, {"model_id": "нет-такой"}) is None
    assert models_api.chosen_model(registry, {}) is None
