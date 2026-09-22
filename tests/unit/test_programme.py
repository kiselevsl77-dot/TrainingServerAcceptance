"""Тесты программы сессии (`acceptance/programme.py`).

Проверяются свойства, на которых держатся требования сценария планирования:
объединение наборов по ИЛИ с дедупликацией и происхождением (`FR-P-68`), покрытие
разделов и обоснование сокращения (`FR-P-69`), шаблоны программ (`FR-P-70`),
а также правила ревизий и утверждения (по аналогии с `FR-P-66`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from acceptance import programme as programme_api
from acceptance import sets as sets_api
from acceptance.checks import catalog


def revision_of(programme: programme_api.Programme, number: int) -> programme_api.ProgrammeRevision:
    """Архивная ревизия программы с проверкой, что она есть (помощник для типизации)."""
    found = programme.find_revision(number)
    assert found is not None, f"ревизия {number} программы не найдена"
    return found


@pytest.fixture()
def library() -> sets_api.SetsLibrary:
    """Стартовая библиотека: набор на каждый раздел плюс «Смоук-минимум»."""
    return sets_api.library_from_catalog(author="Иванов И.И.")


@pytest.fixture()
def programme(library: sets_api.SetsLibrary) -> programme_api.Programme:
    """Программа из трёх наборов с пересечением (система, записи, смоук-минимум)."""
    return programme_api.Programme.from_sets(
        library, ["SET-SYS", "SET-REC", sets_api.SMOKE_SET_ID], author="Петров П.П."
    )


def test_union_deduplicates_and_keeps_provenance(programme: programme_api.Programme):
    """Объединение по ИЛИ: пересечения учтены один раз, происхождение сохранено."""
    sys_ids = [spec.check_id for spec in catalog.by_group("TC-SYS")]
    rec_ids = [spec.check_id for spec in catalog.by_group("TC-REC")]
    smoke_ids = sets_api.smoke_check_ids()

    assert programme.size == len({*sys_ids, *rec_ids, *smoke_ids})
    assert len(programme.check_ids) == len(set(programme.check_ids))  # дублей нет

    first = programme.find("TC-SYS-01")
    assert first is not None
    assert first.source_ids == ["SET-SYS", sets_api.SMOKE_SET_ID]
    assert "SET-SYS" in first.source_note


def test_union_keeps_order_of_first_set_and_mandatory_first(library: sets_api.SetsLibrary):
    """Порядок наследуется по приоритету наборов, обязательные пункты идут первыми."""
    programme = programme_api.Programme.from_sets(library, ["SET-LOAD", "SET-SYS"])

    assert programme.check_ids == [spec.check_id for spec in catalog.by_group("TC-LOAD")] + [
        spec.check_id for spec in catalog.by_group("TC-SYS")
    ]

    with_smoke = programme_api.Programme.from_sets(library, [sets_api.SMOKE_SET_ID, "SET-SYS"])

    assert with_smoke.mandatory_ids  # смоук-минимум есть
    assert with_smoke.check_ids[: len(with_smoke.mandatory_ids)] == with_smoke.mandatory_ids


def test_rebuild_reports_delta_and_intersections(library: sets_api.SetsLibrary):
    """Пересборка сообщает, что добавилось и ушло, и сколько проверок из пересечений."""
    programme = programme_api.Programme.from_sets(library, ["SET-SYS"])
    report = programme.rebuild(library, ["SET-SYS", "SET-REC"])

    assert report["sets"] == 2
    assert report["kept"] == len(catalog.by_group("TC-SYS"))
    assert report["added"] == [spec.check_id for spec in catalog.by_group("TC-REC")]
    assert report["removed"] == []

    report = programme.rebuild(library, ["SET-SYS", sets_api.SMOKE_SET_ID])
    # TC-SYS-01 приходит и из набора раздела, и из смоук-минимума, TC-REC-01 — из записи и смоука
    assert report["intersections"] == 2


def test_mandatory_check_cannot_be_excluded(programme: programme_api.Programme):
    """Обязательные пункты (смоук-минимум) не исключаются из программы (`FR-P-70`)."""
    mandatory = programme.mandatory_ids[0]

    with pytest.raises(ValueError):
        programme.exclude([mandatory])

    assert programme.find(mandatory) is not None


def test_optional_check_can_be_excluded_and_delta_shows_it(programme: programme_api.Programme):
    """Необязательный пункт исключается, и дельта показывает изменение состава."""
    before = programme.size

    removed = programme.exclude(["TC-SYS-05"])

    assert removed == ["TC-SYS-05"]
    assert programme.size == before - 1
    assert programme.find("TC-SYS-05") is None


def test_move_and_reorder_keep_mandatory_boundary(programme: programme_api.Programme):
    """Ручная правка порядка не вытесняет обязательные пункты из начала программы."""
    boundary = len(programme.mandatory_ids)
    optional = programme.items[boundary].check_id

    assert programme.move(optional, -1) is False  # через границу обязательной части — нельзя
    assert programme.move(optional, 1) is True
    assert programme.items[boundary].check_id != optional

    programme.reorder(["TC-REC-02", "TC-SYS-02"])

    assert programme.check_ids[boundary : boundary + 2] == ["TC-REC-02", "TC-SYS-02"]
    assert all(item.mandatory for item in programme.items[:boundary])


def test_manual_check_can_be_added_to_programme(programme: programme_api.Programme):
    """Ручная правка состава: проверку можно добавить в программу (`FR-P-68`)."""
    assert programme.add_check("TC-LOAD-02") is True
    assert programme.add_check("TC-LOAD-02") is False  # повторно не добавляется
    item = programme.find("TC-LOAD-02")

    assert item is not None
    assert item.source_ids == ["вручную"]
    assert item.title  # снимок описания подтянут из каталога


def test_coverage_counts_programme_against_target(library: sets_api.SetsLibrary):
    """Покрытие разделов: «в программе / всего» против целевого объёма (`FR-P-69`)."""
    programme = programme_api.Programme.from_sets(library, ["SET-SYS"])

    rows = {row["section"]: row for row in programme.coverage()}

    assert rows["TC-SYS"]["in_programme"] == len(catalog.by_group("TC-SYS"))
    assert rows["TC-SYS"]["target"] == len(catalog.by_group("TC-SYS"))  # набор объёма «полный»
    assert rows["TC-SYS"]["uncovered"] is False
    assert rows["TC-MOD"]["target"] == 0  # раздел не выбран — и не требуется
    assert programme.uncovered_sections() == []


def test_coverage_reports_uncovered_section_after_exclusion(programme: programme_api.Programme):
    """Исключение пунктов из полного набора делает раздел непокрытым (`FR-P-69`)."""
    programme.exclude(["TC-REC-02", "TC-REC-03"])

    uncovered = programme.uncovered_sections()

    assert [row["section"] for row in uncovered] == ["TC-REC"]
    assert uncovered[0]["missing"] == 2


def test_warnings_describe_empty_unknown_and_intersections(library: sets_api.SetsLibrary):
    """Предупреждения: пустая программа, проверки вне каталога, пересечение наборов."""
    empty = programme_api.Programme()
    assert empty.warnings() == [
        "в программе нет ни одной проверки: выберите наборы в разделе «Наборы»"
    ]

    programme = programme_api.Programme.from_sets(library, ["SET-SYS", sets_api.SMOKE_SET_ID])
    programme.add_check("TC-SYS-99")
    notes = programme.warnings(library)

    assert any("не описаны в каталоге" in note for note in notes)
    assert any("пересечение наборов" in note for note in notes)


def test_empty_set_is_reported_in_warnings(library: sets_api.SetsLibrary):
    """Пустой набор в программе — отдельное предупреждение (`FR-P-69`)."""
    empty = library.create(title="Пустой", section="TC-TASK")
    programme = programme_api.Programme.from_sets(library, [empty.set_id, "SET-SYS"])

    assert any(empty.set_id in note and "пуст" in note for note in programme.warnings(library))


def test_summary_kpi(programme: programme_api.Programme):
    """KPI программы: пункты, обязательные, наборы, разделы, пересечения."""
    summary = programme.summary()

    assert summary["items"] == programme.size
    assert summary["mandatory"] == len(programme.mandatory_ids)
    assert summary["sets"] == 3
    assert summary["sections"] >= 2
    assert summary["sections_uncovered"] == 0
    assert summary["intersections"] == 2
    assert summary["status"] == programme_api.PROGRAMME_DRAFT
    assert summary["coverage"][0]["section"] == "TC-SYS"


def test_rows_take_status_from_results_not_from_programme(programme: programme_api.Programme):
    """Статус в таблице программы берётся из результатов — единственного следа (`DR-P-5`)."""
    rows = {row["check_id"]: row for row in programme.rows()}
    assert rows["TC-SYS-01"]["status"] == ""

    results = {
        "TC-SYS-01": {"status": "успех", "icon": "✅", "verdict": "ответ 200"},
    }
    rows = {row["check_id"]: row for row in programme.rows(results)}

    assert rows["TC-SYS-01"]["status"] == "успех"
    assert rows["TC-SYS-01"]["icon"] == "✅"
    assert rows["TC-SYS-01"]["sources_count"] == 2
    assert rows["TC-REC-01"]["status"] == ""


def test_approve_records_revision_and_locks_edits(programme: programme_api.Programme):
    """Утверждение фиксирует ревизию, автора и время; правка состава блокируется."""
    stored = programme.approve(by="Петров П.П.", comment="программа согласована")

    assert programme.is_approved
    assert programme.approved["author"] == "Петров П.П."
    assert programme.approved["at"]
    assert stored.approved is True
    assert stored.revision == programme.revision
    assert programme.revision_rows()[0]["approved"] is True

    with pytest.raises(ValueError):
        programme.add_check("TC-LOAD-02")
    with pytest.raises(ValueError):
        programme.approve(by="Петров П.П.")


def test_approve_with_reduction_requires_justification(library: sets_api.SetsLibrary):
    """Утверждение с сокращением требует обоснования (`FR-P-69`, `AC-P-26`)."""
    reduced = programme_api.Programme.from_sets(library, ["SET-FILE"])
    reduced.exclude(["TC-FILE-02"])

    with pytest.raises(ValueError):
        reduced.approve(by="Петров П.П.")

    reduced.approve(by="Петров П.П.", reduction="проверка перенесена на этап приёмки")

    assert reduced.approved["reduction"] == "проверка перенесена на этап приёмки"
    assert reduced.reduction == "проверка перенесена на этап приёмки"


def test_empty_programme_cannot_be_approved():
    """Пустую программу утвердить нельзя."""
    with pytest.raises(ValueError):
        programme_api.Programme().approve(by="Петров П.П.")


def test_revise_opens_new_revision_and_keeps_previous_results(programme: programme_api.Programme):
    """Новая ревизия не переписывает утверждённую: результаты прошлой не теряются."""
    programme.approve(by="Петров П.П.", comment="первичная программа")
    approved = list(programme.check_ids)

    programme.revise(author="Петров П.П.", comment="добавлены нагрузки")
    programme.add_check("TC-LOAD-02")

    archived = programme.find_revision(1)

    assert archived is not None
    assert archived.check_ids == approved
    assert archived.approved is True
    assert programme.revision == 2
    assert programme.is_approved is False
    assert programme.approved == {}
    assert archived.check_ids == approved  # архив не изменился


def test_restore_revision_opens_new_revision(programme: programme_api.Programme):
    """Восстановление архивной ревизии — это новая ревизия, история сохраняется."""
    programme.approve(by="Петров П.П.")
    programme.revise(author="Петров П.П.")
    programme.exclude(["TC-SYS-05"])
    shortened = list(programme.check_ids)

    programme.restore_revision(1)

    assert programme.revision == 3
    assert programme.is_approved is False
    assert "TC-SYS-05" in programme.check_ids
    assert revision_of(programme, 2).check_ids == shortened
    assert revision_of(programme, 1).size == len(programme.check_ids)


def test_delta_reports_added_and_removed(programme: programme_api.Programme):
    """Дельта с предыдущей ревизией показывает добавленные и убранные проверки."""
    programme.approve(by="Петров П.П.")
    programme.revise(author="Петров П.П.")
    programme.exclude(["TC-REC-02"])
    programme.add_check("TC-LOAD-02")

    delta = programme.delta()

    assert delta["revision"] == 1
    assert delta["missing"] is False
    assert delta["added"] == ["TC-LOAD-02"]
    assert delta["removed"] == ["TC-REC-02"]
    assert programme.delta(1)["added"] == ["TC-LOAD-02"]

    unknown = programme.delta(99)  # запрошенной ревизии в архиве нет
    assert unknown["revision"] is None
    assert unknown["missing"] is True
    assert unknown["added"] == [] and unknown["removed"] == []


def test_revision_rows_mark_current_and_approved(programme: programme_api.Programme):
    """История ревизий помечает текущую и утверждённые ревизии."""
    programme.approve(by="Петров П.П.", comment="первая")
    programme.revise(author="Петров П.П.", comment="вторая")

    rows = programme.revision_rows()

    assert [row["revision"] for row in rows] == [2, 1]
    assert rows[0]["current"] is True and rows[0]["approved"] is False
    assert rows[1]["current"] is False and rows[1]["approved"] is True
    assert rows[1]["comment"] == "первая"


def test_template_full_takes_all_sections_without_smoke(library: sets_api.SetsLibrary):
    """«Полная программа» — наборы всех разделов, смоук-минимум в неё не входит."""
    full = programme_api.template_programme(programme_api.TEMPLATE_FULL, library, author="Петров")

    assert full.size == len(catalog.CHECKS)
    assert full.mandatory_ids == []
    assert sets_api.SMOKE_SET_ID not in full.selected_set_ids()


def test_template_smoke_is_mandatory_minimum(library: sets_api.SetsLibrary):
    """«Смоук-минимум» — только обязательные проверки."""
    smoke = programme_api.template_programme(programme_api.TEMPLATE_SMOKE, library)

    assert smoke.check_ids == sets_api.smoke_check_ids()
    assert smoke.mandatory_ids == smoke.check_ids
    assert smoke.selected_set_ids() == [sets_api.SMOKE_SET_ID]


def test_template_regress_takes_failures_and_their_smoke(library: sets_api.SetsLibrary):
    """«Регресс после исправления»: отказы прошлой сессии плюс смоук их разделов."""
    regress = programme_api.template_programme(
        programme_api.TEMPLATE_REGRESS,
        library,
        failed_ids=["TC-FILE-02", "TC-TASK-01", "TC-FILE-02"],
    )

    assert set(regress.check_ids) == {"TC-FILE-01", "TC-FILE-02", "TC-TASK-01", "TC-TASK-02"}
    assert regress.mandatory_ids == ["TC-FILE-01", "TC-TASK-02"]  # смоук идёт первым
    assert regress.check_ids[:2] == regress.mandatory_ids
    assert regress.set_refs[0]["set_id"].startswith(programme_api.TEMPLATE_SOURCE)


def test_unknown_template_is_rejected(library: sets_api.SetsLibrary):
    """Неизвестный шаблон — ошибка, а не молчаливая пустая программа."""
    with pytest.raises(ValueError):
        programme_api.template_programme("шаблон из будущего", library)


def test_programme_survives_session_roundtrip(programme: programme_api.Programme):
    """Программа сохраняется в сессию и читается обратно (схема v7)."""
    session = SimpleNamespace(programme={}, history=[])

    def add_history(event: str, message: str = "", **payload: Any) -> None:
        session.history.append((event, message, payload))

    session.add_history = add_history
    programme.approve(by="Петров П.П.")
    programme_api.save_programme(
        session, programme, event="programme_updated", message="программа утверждена"
    )
    restored = programme_api.load_programme(session)

    assert restored.check_ids == programme.check_ids
    assert restored.revision == programme.revision
    assert restored.status == programme_api.PROGRAMME_APPROVED
    assert revision_of(restored, 1).approved is True
    assert restored.approved["author"] == "Петров П.П."
    assert session.history[-1][0] == "programme_updated"
    assert session.history[-1][2]["items"] == programme.size


def test_save_programme_without_event_writes_nothing_to_history(programme: programme_api.Programme):
    """Сохранение без события не пишет историю сессии (обычная правка черновика)."""
    session = SimpleNamespace(programme={}, history=[])

    def add_history(event: str, message: str = "", **payload: Any) -> None:
        session.history.append((event, message, payload))

    session.add_history = add_history
    programme_api.save_programme(session, programme)

    assert session.history == []
    assert session.programme["items"]


def test_from_dict_tolerates_missing_and_unknown_fields():
    """Чтение программы терпимо к отсутствующим и лишним полям (`DR-P-12`)."""
    restored = programme_api.Programme.from_dict(
        {
            "revision": "3",
            "status": "неизвестный статус",
            "items": [{"check_id": "TC-SYS-01"}, {"check_id": "TC-SYS-02", "mandatory": True}],
            "set_refs": [{"set_id": "SET-SYS", "revision": 1}],
            "лишнее поле": 1,
        }
    )

    assert restored.revision == 3
    assert restored.status == programme_api.PROGRAMME_DRAFT
    assert restored.check_ids == ["TC-SYS-01", "TC-SYS-02"]
    assert restored.mandatory_ids == ["TC-SYS-02"]
    assert restored.selected_set_ids() == ["SET-SYS"]
    assert restored.find("TC-SYS-01").source_ids == []
    assert programme_api.Programme.from_dict(None).size == 0
    assert programme_api.Programme.from_dict({}).status == programme_api.PROGRAMME_DRAFT
