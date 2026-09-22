"""Тесты предпросмотра `__TEST__`-данных (`acceptance/test_payloads.py`).

Пожелание заказчика (21.09.2026, п. 5): до запуска видеть, что именно уйдёт на
стенд. Тела и имена `__TEST__`-сущностей формирует код сценариев, поэтому тест
следит за двумя вещами:

    * предпросмотр **не расходится с кодом** — константы CSV берутся из
      `acceptance.checks.files`, имя файла совпадает с правилом `_test_file_name`;
    * предпросмотр **не молчит**: у каждой проверки, которая что-то создаёт,
      payload описан, а у проверок только для чтения честно сказано, что их нет.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from acceptance import test_payloads
from acceptance.checks import catalog
from acceptance.checks import files as files_checks

SESSION = SimpleNamespace(session_id="20260921-101500-abcd")


def _payloads(check_id: str, session: SimpleNamespace = SESSION):
    """Предпросмотр payload'ов проверки каталога."""
    spec = catalog.find(check_id)
    assert spec is not None, check_id
    return test_payloads.payload_previews(spec, session)


def test_reading_checks_have_no_payload():
    """Проверки только для чтения payload не создают — блок говорит об этом прямо."""
    previews = _payloads("TC-FILE-01")

    assert previews == []
    lines = test_payloads.preview_lines(previews)

    assert len(lines) == 1
    assert "только читают данные" in lines[0]
    assert "payload не создаётся" in test_payloads.summary(previews)


def test_file_upload_payload_matches_scenario_name():
    """Имя CSV-файла совпадает с правилом сценария загрузки (`__TEST__<проверка>_<сессия>.csv`)."""
    previews = _payloads("TC-FILE-13")
    payload = next(item for item in previews if item.kind == "файл")

    assert payload.name == "__TEST__TC_FILE_13_20260921-101500-abcd.csv"
    assert payload.removed_after is True
    assert payload.target.startswith("POST /api/data/file")
    assert "file_type=RAW" in payload.content


def test_file_payload_content_comes_from_scenario_constants():
    """Содержимое и MIME берутся из тех же констант, что использует сценарий."""
    payload = next(item for item in _payloads("TC-FILE-13") if item.kind == "файл")

    header = files_checks.ROUND_TRIP_BODY.splitlines()[0]
    assert header in payload.content
    assert files_checks.MIME_CSV in payload.content


def test_invalid_csv_probe_describes_negative_body():
    """Негативная проба с «плохим» CSV описывает именно это тело."""
    spec = catalog.find("TC-FILE-11")
    assert spec is not None

    previews = test_payloads.payload_previews(spec, SESSION, params={"csv_body": "a;b\n1;2\n"})
    payload = next(item for item in previews if item.kind == "файл")

    assert "без обязательных колонок" in payload.content
    assert files_checks.INVALID_BODY.strip().replace("\n", " | ") in payload.content


def test_operator_override_of_name_is_shown():
    """Имя, заданное оператором в параметрах проверки, показывается как есть."""
    spec = catalog.find("TC-FILE-13")
    assert spec is not None

    previews = test_payloads.payload_previews(
        spec, SESSION, params={"csv_name": "__TEST__mine.csv"}
    )

    assert previews[0].name == "__TEST__mine.csv"


def test_dataset_creation_payload_is_marked_as_generated():
    """Датасет: имя генерируется при выполнении, тип и описание известны заранее."""
    payload = next(item for item in _payloads("TC-DS-01") if item.kind == "датасет")

    assert payload.is_generated is True
    assert payload.name.startswith(f"{test_payloads.TEST_PREFIX}dataset-")
    assert "direct_fill" in payload.content
    assert payload.removed_after is True


def test_fill_payload_describes_body_and_cleanup_note():
    """Наполнение датасета: описано тело из `file_ids` и уборка `__TEST__`-сущностей."""
    payload = next(item for item in _payloads("TC-DS-05") if item.kind == "наполнение датасета")

    assert "file_ids" in payload.content
    assert payload.removed_after is False
    assert "TC-CLEAN" in payload.note


def test_every_creating_check_gets_a_preview():
    """У каждой проверки, создающей данные, предпросмотр не пуст (нет «слепых» запусков)."""
    creating = [
        spec
        for spec in catalog.CHECKS
        if any(
            operation
            in (test_payloads.UPLOAD_FILE_OPERATION, test_payloads.CREATE_DATASET_OPERATION)
            or operation == test_payloads.FILL_DATASET_OPERATION
            for operation in spec.endpoints
        )
    ]

    assert creating, "в каталоге есть проверки, создающие `__TEST__`-данные"
    for spec in creating:
        previews = test_payloads.payload_previews(spec, SESSION)
        assert previews, spec.check_id


def test_previews_are_serializable_for_report():
    """Предпросмотр превращается в плоские словари (для сессии и отчёта)."""
    payload = _payloads("TC-FILE-13")[0].to_dict()

    assert set(payload) == {"kind", "target", "name", "content", "removed_after", "note"}
    assert all(isinstance(value, (str, bool)) for value in payload.values())


@pytest.mark.parametrize("check_id", ["TC-FILE-11", "TC-FILE-12", "TC-DS-01", "TC-DS-05"])
def test_preview_lines_are_human_readable(check_id: str):
    """Строки блока читаются оператором: имя, содержимое и уборка названы явно."""
    lines = test_payloads.preview_lines(_payloads(check_id))

    text = "\n".join(lines)
    assert "имя/идентификатор" in text
    assert "содержимое" in text
    assert "удаляется после проверки" in text
