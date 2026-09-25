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
