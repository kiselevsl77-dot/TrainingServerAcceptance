"""Тесты артефактов ресурсоёмких проверок: кандидаты, решения и панель (`FR-T10`).

Проверяется то, что нельзя увидеть на стенде и легко сделать по-разному:

* **кто кандидат** — только `__TEST__`-модели и файлы `.h5`/`.onnx` (чужой стенд
  полон моделей испытаний, рушить их пульт не имеет права), с честной отметкой
  «наша сессия» / «прошлый прогон»;
* **какие слова принимаются** — панельные `keep`/`delete` и формулировки карточки
  запуска; всё остальное отвергается, а не угадывается;
* **как решение видно отчёту** — запись в `session.artifacts` с `entity_id` и
  `action`, по которой закрывается `TC-CLEAN-04`;
* **что показывает панель** — итог «сколько решений принято» и предупреждение, что
  `TC-CLEAN-04` ставить рано, пока объекты ждут решения.
"""

from __future__ import annotations

from typing import Any

from acceptance import run_card
from acceptance import session as session_api
from client.schemas import FileMetadataResponse, ModelMetadata
from pult import artifacts as artifacts_api
from pult import view

SESSION_ID = "20261001-120000-abcd"
MODEL_ID = "11111111-2222-4333-8444-555555555555"
FILE_ID = "66666666-7777-4888-9999-000000000000"


def model_row(model_id: str, name: str, *, version: str = "cnn_1d.7f3a91c2") -> ModelMetadata:
    """Запись реестра моделей (`ModelMetadata`)."""
    return ModelMetadata.model_validate(
        {
            "id": model_id,
            "file_id": FILE_ID,
            "architecture": "cnn_1d",
            "version": version,
            "name": name,
            "description": None,
            "phase_type": "single",
            "output_size": 2,
            "config": {"architecture": "cnn_1d"},
            "metrics": {"accuracy": 0.91},
            "pruning_mask": None,
            "created_at": "2026-10-01T11:00:00",
            "updated_at": "2026-10-01T12:30:00",
            "signals": None,
            "signal_aliases": None,
        }
    )


def file_row(file_id: str, name: str, *, size: int = 97544) -> FileMetadataResponse:
    """Запись реестра файлов (`FileMetadataResponse`)."""
    return FileMetadataResponse.model_validate(
        {
            "id": file_id,
            "file_name": name,
            "size": size,
            "s3_path": f"s3://bucket/{name}",
            "import_date": "2026-10-01T11:05:00",
            "file_type": "ONNX",
        }
    )


def decision(entity_id: str, action: str, *, check_id: str = "TC-TR-04") -> dict[str, Any]:
    """Запись решения об артефакте (то, что писал сценарий или оператор в пульте)."""
    return {
        "at": "2026-10-01T13:00:00",
        "kind": "модель",
        "name": f"__TEST__TC_TR_02_{SESSION_ID}_trained",
        "path": "",
        "size_bytes": 0,
        "note": "результат проверки оставлен артефактом",
        "entity_id": entity_id,
        "action": action,
        "check_id": check_id,
    }


# ---------------------------------------------------------------------------
# Кто кандидат
# ---------------------------------------------------------------------------
def test_candidates_are_only_test_models_and_artifact_files() -> None:
    """Кандидаты — `__TEST__`-модели и файлы `.h5`/`.onnx`; чужое не предлагается."""
    models = [
        model_row(MODEL_ID, f"__TEST__TC_TR_02_{SESSION_ID}_trained"),
        model_row("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "wifi-detector-prod"),
    ]
    files = [
        file_row(FILE_ID, f"__TEST__TC_TR_06_{SESSION_ID}_trained.onnx"),
        file_row("11111111-2222-4333-8444-999999999999", "__TEST__session_weights.h5"),
        file_row("22222222-3333-4444-8555-666666666666", "__TEST__raw_capture.csv"),
        file_row("33333333-4444-4555-8666-777777777777", "stand_dataset.csv"),
    ]

    rows = artifacts_api.candidate_rows(models, files, session_id=SESSION_ID)

    assert [(row["kind"], row["name"]) for row in rows] == [
        ("model", f"__TEST__TC_TR_02_{SESSION_ID}_trained"),
        ("file", f"__TEST__TC_TR_06_{SESSION_ID}_trained.onnx"),
        ("file", "__TEST__session_weights.h5"),
    ], "CSV и чужие имена в кандидаты не попадают"
    assert [row["ours"] for row in rows] == [True, True, False]
    assert rows[0]["size_bytes"] is None, "у модели размер не приходит реестром"
    assert rows[1]["size_bytes"] == 97544


def test_candidates_keep_own_session_first() -> None:
    """Следы прошлых прогонов идут после своих: оператор сначала видит свою работу."""
    models = [
        model_row("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "__TEST__TC_MOD_02_прошлый_trained"),
        model_row(MODEL_ID, f"__TEST__TC_TR_02_{SESSION_ID}_trained"),
    ]

    rows = artifacts_api.candidate_rows(models, [], session_id=SESSION_ID)

    assert [row["ours"] for row in rows] == [True, False]
    assert SESSION_ID in rows[0]["name"]


# ---------------------------------------------------------------------------
# Какие слова принимаются
# ---------------------------------------------------------------------------
def test_decision_words_accept_panel_and_card_wording() -> None:
    """Принимаются слова панели и формулировки карточки запуска (и ничего больше)."""
    assert artifacts_api.normalize_decision("keep") == artifacts_api.DECISION_KEEP
    assert artifacts_api.normalize_decision(" DELETE ") == artifacts_api.DECISION_DELETE
    for word in run_card.ARTIFACT_ACTIONS:
        assert artifacts_api.normalize_decision(word), f"карточка запуска: «{word}»"
    assert artifacts_api.normalize_decision("оставить") == ""
    assert artifacts_api.normalize_decision(None) == ""


def test_card_wording_matches_run_card_actions() -> None:
    """Словарь панели не разошёлся с карточкой запуска (иначе решение не распознается)."""
    assert set(artifacts_api.CARD_DECISIONS) == set(run_card.ARTIFACT_ACTIONS)


# ---------------------------------------------------------------------------
# Итог и подсказка: когда TC-CLEAN-04 ставить рано
# ---------------------------------------------------------------------------
def test_summary_counts_decisions_and_pending() -> None:
    """Итог панели: сколько решено и сколько объектов ещё ждёт решения."""
    rows = [
        {"entity_id": MODEL_ID, "kind": "model", "name": "a", "ours": True},
        {"entity_id": FILE_ID, "kind": "file", "name": "b", "ours": True},
        {
            "entity_id": "cccccccc-dddd-4eee-8fff-000000000000",
            "kind": "file",
            "name": "c",
            "ours": False,
        },
    ]
    index = artifacts_api.decision_index([decision(MODEL_ID, session_api.ARTIFACT_KEPT)])

    data = artifacts_api.summary(rows, index)

    assert data == {
        "total": 3,
        "ours": 2,
        "foreign": 1,
        "kept": 1,
        "deleted": 0,
        "decided": 1,
        "undecided": 2,
    }
    note = artifacts_api.conclusion(rows, index)
    assert "ждут решения: 2" in note
    assert "TC-CLEAN-04 ставить рано" in note


def test_last_decision_wins_and_closes_clean_04() -> None:
    """Решение можно переписать: побеждает последнее, и тогда `TC-CLEAN-04` закрыт."""
    records = [
        decision(MODEL_ID, session_api.ARTIFACT_KEPT),
        decision(MODEL_ID, session_api.ARTIFACT_DELETED),
    ]
    rows = [{"entity_id": MODEL_ID, "kind": "model", "name": "a", "ours": True}]

    index = artifacts_api.decision_index(records)

    assert index[MODEL_ID]["action"] == session_api.ARTIFACT_DELETED
    data = artifacts_api.summary(rows, index)
    assert (data["kept"], data["deleted"], data["undecided"]) == (0, 1, 0)
    assert "решение принято по каждому объекту" in artifacts_api.conclusion(rows, index)


def test_evidence_export_is_not_a_decision() -> None:
    """Выгруженный манифест решением не считается: у него нет `entity_id` и `action`."""
    export = {
        "kind": "records_manifest (CSV)",
        "name": "manifest.csv",
        "path": "x",
        "action": "",
    }

    assert artifacts_api.decision_index([export]) == {}


# ---------------------------------------------------------------------------
# Панель: строки и предупреждение
# ---------------------------------------------------------------------------
def test_artifacts_view_shows_decision_and_actions() -> None:
    """Строка панели несёт объект, решение, время решения и обе кнопки."""
    rows = artifacts_api.candidate_rows(
        [model_row(MODEL_ID, f"__TEST__TC_TR_02_{SESSION_ID}_trained")],
        [file_row(FILE_ID, "__TEST__session_weights.h5")],
        session_id=SESSION_ID,
    )
    records = [decision(MODEL_ID, session_api.ARTIFACT_KEPT)]

    panel = view.artifacts_view(rows, records)

    model_card, file_card = panel["rows"]
    assert model_card["kind_label"] == "модель"
    assert model_card["decision"] == "keep"
    assert model_card["decision_label"] == session_api.ARTIFACT_KEPT
    assert model_card["decided"] is True
    assert model_card["check_id"] == "TC-TR-04"
    assert model_card["owner"] == "наша сессия"
    assert [item["action"] for item in model_card["actions"]] == ["keep", "delete"]
    assert file_card["size"] == "97 544 Б"
    assert file_card["decided"] is False
    assert file_card["decision_label"] == "решение не принято"
    assert panel["summary"]["undecided"] == 1


def test_artifacts_view_empty_stand_says_so() -> None:
    """Пустой стенд — не ошибка: панель пишет, что решать нечего."""
    panel = view.artifacts_view([], [])

    assert panel["rows"] == []
    assert panel["summary"]["total"] == 0
    assert "Кандидатов нет" in panel["note"]


def test_decided_object_still_counted_after_removal() -> None:
    """Убранный со стенда объект не «исчезает» из итога: решение осталось в сессии.

    Дефект найден живой приёмкой 01.10.2026: после «🗑 удалить» список кандидатов
    пустел, и панель сообщала «кандидатов нет» — хотя решение принято и его надо
    видеть, иначе `TC-CLEAN-04` ставится вслепую.
    """
    records = [
        decision(MODEL_ID, session_api.ARTIFACT_KEPT),
        decision(MODEL_ID, session_api.ARTIFACT_DELETED),
    ]

    note = artifacts_api.conclusion([], artifacts_api.decision_index(records))
    data = artifacts_api.summary([], artifacts_api.decision_index(records))

    assert "решения зафиксированы" in note
    assert "удалено 1" in note
    assert data["decided"] == 1 and data["undecided"] == 0
    assert artifacts_api.conclusion([], {}) == (
        "Кандидатов нет: тяжёлые проверки не оставили объектов на стенде."
    )
