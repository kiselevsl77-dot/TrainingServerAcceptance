"""Тесты карточки запуска (`acceptance/run_card.py`): поля, подтверждение, доказательство.

Карточка запуска обязательна для изменяющих и ресурсоёмких проверок (`FR-P-19`,
`AC-P-13`): без цели, данных, ответственного и подтверждения расхода ресурсов проверка
не запускается. Заполненная карточка уходит в доказательства результата, поэтому из
протокола и отчёта видно, на каких данных получен вердикт.

Тесты перенесены из прежнего `test_ui_components.py` вместе с кодом: логика карточки
переехала из UI-слоя в ядро, когда пульт стал тонким API.
"""

from __future__ import annotations

from acceptance import run_card
from acceptance.programme import ProgrammeItem


def _item() -> ProgrammeItem:
    """Пункт программы: изменяющая проверка с понятными целью и ожиданием."""
    return ProgrammeItem(
        check_id="TC-LOAD-03",
        order=1,
        check_class="live",
        title="Запуск нагрузки",
        module="loads",
        group="Loads",
        requirement="FR-T5",
        expected="202 Accepted",
    )


def test_defaults_prefill_goal_and_data() -> None:
    """Заготовка карточки подставляет цель и данные, чтобы оператор правил, а не набирал."""
    values = run_card.defaults(_item(), session_id="20260922-2f6a", author="Петров П.П.")

    assert values["goal"] == "TC-LOAD-03: Запуск нагрузки"
    assert "20260922-2f6a" in values["data"]
    assert "__TEST__" in values["data"]
    assert values["responsible"] == "Петров П.П."
    assert values["artifacts"] in run_card.ARTIFACT_ACTIONS
    assert values["confirm"] is False


def test_requires_fields_and_confirmation() -> None:
    """Без ответственного и подтверждения расхода ресурсов карточка не принимается."""
    values = run_card.defaults(_item())

    assert run_card.validate(values) == ["Ответственный", "Подтверждаю расход ресурсов"]

    values["responsible"] = "Иванов И.И."
    values["confirm"] = True
    assert run_card.validate(values) == []

    values["data"] = "   "
    assert run_card.validate(values) == [run_card.FIELD_LABELS["data"]]


def test_evidence_records_operator_decision() -> None:
    """Доказательство фиксирует решение оператора и не молчит о незаполненных полях."""
    values = run_card.defaults(_item(), session_id="20260922-2f6a", author="Петров П.П.")
    values["confirm"] = True

    evidence = run_card.evidence(values, author="Петров П.П.", at="2026-09-22T12:03:00")

    assert evidence["at"] == "2026-09-22T12:03:00"
    assert evidence["author"] == "Петров П.П."
    assert evidence["goal"].startswith("TC-LOAD-03:")
    assert evidence["responsible"] == "Петров П.П."
    assert evidence["confirm"] is True
    assert evidence["filled"] is True
    assert evidence["missing"] == []

    empty = run_card.evidence({"goal": "", "confirm": False})
    assert empty["filled"] is False
    assert empty["missing"] == [
        run_card.FIELD_LABELS["goal"],
        run_card.FIELD_LABELS["data"],
        run_card.FIELD_LABELS["responsible"],
        run_card.FIELD_LABELS["confirm"],
    ]


def test_defaults_put_dataset_into_the_data_field() -> None:
    """Датасет проверки подставляется в «Данные, на которых выполняется» (`llm_tasks/49`, 1б).

    На ML-проверке `__TEST__`-подпись была бы неправдой: обучение идёт на настоящем
    датасете стенда, и оператор обязан видеть его в карточке до запуска.
    """
    values = run_card.defaults(
        _item(),
        session_id="20260922-2f6a",
        author="Петров П.П.",
        data_hint="датасет «skfu-train» (104 файла)",
    )

    assert values["data"] == "датасет «skfu-train» (104 файла), сессия 20260922-2f6a"
    assert "__TEST__" not in values["data"]
    assert values["dataset_id"] == "" and values["model_id"] == ""
    assert run_card.ML_FIELDS == ("dataset_id", "model_id")


def test_dataset_and_model_are_required_only_where_used() -> None:
    """Датасет и модель обязательны не везде, а только там, где проверка их использует.

    Иначе пульт требовал бы датасет у каждой изменяющей проверки и мешал бы оператору
    (`llm_tasks/49`, задача 1б).
    """
    values = run_card.defaults(_item(), author="Петров П.П.")
    values["confirm"] = True

    assert run_card.validate(values) == [], "обычной проверке датасет не нужен"
    assert run_card.validate(values, required_extra=run_card.ML_FIELDS) == [
        run_card.FIELD_LABELS["dataset_id"],
        run_card.FIELD_LABELS["model_id"],
    ]
    assert run_card.validate(values, required_extra=("dataset_id",)) == [
        run_card.FIELD_LABELS["dataset_id"]
    ]


def test_evidence_keeps_dataset_and_model_labels() -> None:
    """Доказательство хранит и идентификаторы выбранных датасета и модели, и подписи.

    По протоколу должно быть видно не только «на каком датасете считали», но и как он
    назывался и сколько в нём файлов (`llm_tasks/49`, вопрос 5 = а).
    """
    values = run_card.defaults(_item(), session_id="20260922-2f6a", author="Петров П.П.")
    values.update(
        {
            "dataset_id": "11111111-1111-1111-1111-111111111111",
            "dataset_label": "skfu-train · файлов 104",
            "model_id": "22222222-2222-2222-2222-222222222222",
            "model_label": "skfu-net · cnn · v3",
            "confirm": True,
        }
    )

    evidence = run_card.evidence(values, author="Петров П.П.", at="2026-09-22T12:03:00")

    assert evidence["dataset_id"] == values["dataset_id"]
    assert evidence["dataset_label"] == "skfu-train · файлов 104"
    assert evidence["model_id"] == values["model_id"]
    assert evidence["model_label"] == "skfu-net · cnn · v3"
    assert evidence["filled"] is True
