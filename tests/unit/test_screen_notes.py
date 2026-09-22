"""Тесты экрана «Замечания к API» (`SCR-403`) и статусов замечания.

Замечание — документ разбора (`FR-P-37`, `DR-P-6`): приоритет, модуль, эндпоинт, факт,
ожидание, воспроизведение, доказательства, статус. Экран ведёт реестр, создаёт замечания
**из проверки, из известного дефекта и вручную** (`FR-P-38`), собирает комплект разработчику
(`FR-P-40`) и показывает, что мешает готовности отчёта (`FR-P-48`).

Проверяется и то, что экран не подменяет прогон: команды запуска у него нет (`IR-P-4`), а
статусы проверок он не трогает (`DR-P-5`).
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

from acceptance import notes as notes_api
from acceptance import results as results_api
from acceptance.checks.registry import CheckStatus
from acceptance.session import TestSession as SessionModel
from acceptance.session import new_session
from acceptance.ui.screens import scr403_notes

#: Команды запуска: в разборе их быть не должно (`IR-P-4`).
RUN_COMMANDS = (
    "execute_next",
    "run_batch_tech",
    "auto_step",
    "execute_call",
    "execute_check",
    "from acceptance.runner",
)

#: Идентификатор проверки с известным дефектом API (реестр `notes.KNOWN_DEFECTS`).
KNOWN_CHECK = "TC-FILE-10"


def _session_with_defect() -> SessionModel:
    """Сессия с отказом и блокировкой: кандидаты в замечания (`FR-P-31`)."""
    session = new_session(base_url="http://test.local")
    results_api.record(
        session,
        {"check_id": "TC-SYS-03", "status": str(CheckStatus.FAILED), "verdict": "405 вместо 404"},
    )
    results_api.record(
        session,
        {"check_id": KNOWN_CHECK, "status": str(CheckStatus.BLOCKED), "verdict": "дефект API"},
    )
    results_api.record(
        session,
        {"check_id": "TC-SYS-01", "status": str(CheckStatus.PASSED), "verdict": "ок"},
    )
    return session


def _note(
    title: str = "Расхождение по проверке",
    *,
    module: str = "File Import",
    priority: str = "P1",
    reproduction: str = "curl -X GET http://test.local/api/data/files",
    check_id: str | None = None,
    evidence: str = "",
    status: str = notes_api.NOTE_OPEN,
) -> notes_api.ApiNote:
    """Замечание для тестов: по умолчанию P1 с воспроизведением (замечание полное)."""
    return notes_api.new_note(
        title,
        module=module,
        priority=priority,
        reproduction=reproduction,
        check_id=check_id,
        evidence=evidence,
        status=status,
    )


# ---------------------------------------------------------------------------
# Реестр замечаний: строки, фильтры, статусы
# ---------------------------------------------------------------------------
def test_note_rows_describe_registry():
    """Строка реестра: приоритет, статус, модуль, проверка, связи и полнота (`FR-P-37`)."""
    full = _note(check_id=KNOWN_CHECK, evidence="#88, #92")
    partial = _note("Без воспроизведения", priority="P0", reproduction="")

    rows = scr403_notes.note_rows([full, partial])

    assert [row["priority"] for row in rows] == ["P0", "P1"], "P0 показываются первыми"
    assert rows[0]["mark"] == scr403_notes.INCOMPLETE_MARK
    assert rows[1]["status"] == notes_api.NOTE_OPEN
    assert rows[1]["check_id"] == KNOWN_CHECK
    assert rows[1]["links"] == f"проверка {KNOWN_CHECK} · обмены #88, #92"
    assert rows[0]["check_id"] == scr403_notes.DASH


def test_note_links_require_check_and_evidence():
    """Связи «замечание ↔ проверка ↔ обмены» показываются, только если они есть (`FR-P-38`)."""
    assert scr403_notes.note_links(_note()) == ""
    assert scr403_notes.note_links(_note(check_id=KNOWN_CHECK)) == f"проверка {KNOWN_CHECK}"


def test_filters_narrow_registry():
    """Фильтры реестра: приоритет, модуль, статус, поиск и «только без воспроизведения»."""
    notes = [
        _note("Дефект скачивания", module="File Import", priority="P0"),
        _note("Нет состава датасета", module="Datasets", priority="P1"),
        _note("Без воспроизведения", module="Datasets", priority="P2", reproduction=""),
        _note("Закрытый дефект", module="Datasets", priority="P1", status=notes_api.NOTE_CLOSED),
    ]

    by_priority = scr403_notes.filter_notes(notes, priority="P0")
    assert [note.title for note in by_priority] == ["Дефект скачивания"]

    by_module = scr403_notes.filter_notes(notes, module="Datasets")
    assert len(by_module) == 3

    closed = scr403_notes.filter_notes(notes, status_value=notes_api.NOTE_CLOSED)
    assert [note.title for note in closed] == ["Закрытый дефект"]

    found = scr403_notes.filter_notes(notes, search="датасета")
    assert [note.title for note in found] == ["Нет состава датасета"]

    incomplete = scr403_notes.filter_notes(notes, only_incomplete=True)
    assert [note.title for note in incomplete] == ["Без воспроизведения"]
    assert len(scr403_notes.filter_notes(notes, priority=scr403_notes.ANY)) == 4


def test_filter_options_list_registry_values():
    """Варианты фильтров: «все», приоритеты по старшинству, модули и статусы выборки."""
    notes = [
        _note("A", module="File Import", priority="P2"),
        _note("B", module="Datasets", priority="P0", status=notes_api.NOTE_CLOSED),
    ]

    assert scr403_notes.priority_options(notes) == (scr403_notes.ANY, "P0", "P2")
    assert scr403_notes.module_options(notes) == (scr403_notes.ANY, "Datasets", "File Import")
    assert scr403_notes.status_filter_options(notes) == (
        scr403_notes.ANY,
        notes_api.NOTE_OPEN,
        notes_api.NOTE_CLOSED,
    )


def test_incomplete_note_blocks_report_readiness():
    """Замечание без воспроизведения неполно и блокирует готовность отчёта (`FR-P-48`)."""
    partial = _note("Без воспроизведения", priority="P0", reproduction="")

    assert scr403_notes.is_complete(_note()) is True
    assert scr403_notes.is_complete(partial) is False
    note = scr403_notes.incomplete_note([partial])
    assert scr403_notes.INCOMPLETE_MARK in note
    assert "FR-P-48" in note
    assert scr403_notes.incomplete_note([_note()]) == ""


def test_defects_note_counts_statuses():
    """Сводка реестра считает статусы и неполные замечания (`DR-P-6`)."""
    notes = [_note(status=notes_api.NOTE_CLOSED), _note("Второе", reproduction="")]

    summary = scr403_notes.defects_note(notes)

    assert "Замечаний 2" in summary
    assert f"{notes_api.NOTE_CLOSED}: 1" in summary
    assert "без воспроизведения: 1" in summary
    assert "Замечаний нет" in scr403_notes.defects_note([])


# ---------------------------------------------------------------------------
# Статусы замечания в ядре (`FR-P-37`, `DR-P-6`)
# ---------------------------------------------------------------------------
def test_note_status_lifecycle_requires_comment_on_close():
    """Статус замечания меняется ядром: закрытие без комментария отклоняется (`FR-P-37`)."""
    note = _note()

    assert note.status == notes_api.NOTE_OPEN
    notes_api.set_status(note, notes_api.NOTE_IN_PROGRESS, comment="передано разработчику")
    assert note.status == notes_api.NOTE_IN_PROGRESS
    assert note.status_note == "передано разработчику"

    for bad in ("нет-такого", ""):
        try:
            notes_api.set_status(note, bad)
        except ValueError as exc:
            assert "статус" in str(exc)
        else:
            raise AssertionError(f"статус {bad!r} принят быть не должен")

    try:
        notes_api.set_status(note, notes_api.NOTE_CLOSED)
    except ValueError as exc:
        assert "комментария" in str(exc)
    else:
        raise AssertionError("закрытие без комментария принято быть не должно")

    notes_api.set_status(note, notes_api.NOTE_CLOSED, comment="перепрогон на новой сборке")
    assert note.status == notes_api.NOTE_CLOSED


def test_apply_status_updates_session_registry():
    """Смена статуса правит реестр сессии, не переписывая остальные поля замечания."""
    session = _session_with_defect()
    note = _note(check_id=KNOWN_CHECK, evidence="#88")
    session.notes.append(note.to_dict())

    updated = notes_api.apply_status(
        session.notes, note.note_id, notes_api.NOTE_IN_PROGRESS, comment="взято в работу"
    )

    assert updated["status"] == notes_api.NOTE_IN_PROGRESS
    assert updated["status_note"] == "взято в работу"
    assert updated["evidence"] == "#88", "остальные поля замечания не перезаписываются"
    assert session.notes[0] is updated
    assert notes_api.find_note(session.notes, note.note_id) is not None
    assert notes_api.find_note(session.notes, "нет-такого") is None


def test_apply_status_rejects_unknown_note():
    """Статус нельзя записать замечанию, которого нет в реестре сессии (`DR-P-6`)."""
    session = _session_with_defect()

    try:
        notes_api.apply_status(session.notes, "нет-такого", notes_api.NOTE_CLOSED, comment="закрыл")
    except ValueError as exc:
        assert "не найдено" in str(exc)
    else:
        raise AssertionError("неизвестное замечание принято быть не должно")


# ---------------------------------------------------------------------------
# Создание замечаний: из проверки, из дефекта, связи и комплект
# ---------------------------------------------------------------------------
def test_defect_candidates_list_failed_and_blocked_checks():
    """Кандидаты в замечания — отказы и блокировки сессии (`FR-P-31`)."""
    session = _session_with_defect()

    assert scr403_notes.defect_candidates(session) == ["TC-SYS-03", KNOWN_CHECK]
    assert scr403_notes.defect_candidates(new_session(base_url="http://test.local")) == []


def test_note_from_record_uses_catalog_and_known_defect():
    """Замечание из проверки берёт описание каталога и известного дефекта (`FR-P-38`)."""
    note = scr403_notes.note_from_record(KNOWN_CHECK, evidence="#88, #92")

    assert note.check_id == KNOWN_CHECK
    assert note.evidence == "#88, #92"
    assert note.priority == scr403_notes.priority_for(KNOWN_CHECK)
    assert note.module and note.fact and note.expected
    assert note.reproduction == "", (
        "воспроизведение заполняет оператор — неполнота видна (`FR-P-48`)"
    )

    unknown = scr403_notes.note_from_record("TC-UNKNOWN-99")
    assert unknown.check_id == "TC-UNKNOWN-99"
    assert unknown.priority == "P1"


def test_note_card_lines_show_all_required_fields():
    """Карточка замечания показывает поля `FR-P-37` и связи `FR-P-38`."""
    note = _note(check_id=KNOWN_CHECK, evidence="#88")
    notes_api.set_status(note, notes_api.NOTE_CLOSED, comment="перепрогон подтвердил исправление")

    text = "\n".join(scr403_notes.note_card_lines(note))

    assert f"приоритет: {note.priority}" in text
    assert f"статус: {notes_api.NOTE_CLOSED}" in text
    assert "воспроизведение:" in text
    assert "доказательства: обмены #88" in text
    assert "перепрогон подтвердил исправление" in text
    assert scr403_notes.INCOMPLETE_MARK in "\n".join(
        scr403_notes.note_card_lines(_note(reproduction=""))
    )


def test_prospective_rows_are_separate_registry():
    """Перспективные требования — отдельный реестр, не влияющий на статусы (`FR-P-39`)."""
    rows = scr403_notes.prospective_rows()

    assert rows, "реестр перспективных требований не должен быть пустым"
    assert all(row["title"] for row in rows)
    assert all("why" in row for row in rows)


def test_bundle_texts_include_notes_and_links():
    """Комплект разработчику: `md`, `json` и `csv` с теми же замечаниями (`FR-P-40`)."""
    notes = [_note(check_id=KNOWN_CHECK, evidence="#88"), _note("Второе", priority="P0")]

    markdown = scr403_notes.bundle_markdown(notes)
    payload = json.loads(scr403_notes.bundle_json(notes))
    rows = scr403_notes.bundle_csv(notes).splitlines()

    assert "# Замечания к API" in markdown
    assert f"связи: проверка {KNOWN_CHECK}" in markdown, "связь с проверкой идёт и в комплект"
    assert "обмены #88" in markdown
    assert payload["count"] == 2
    assert payload["notes"][0]["priority"] == "P0", "P0 идёт первым"
    assert rows[0].startswith("note_id,priority,status,"), "колонки совпадают с отчётом"

    session: SessionModel = new_session(base_url="http://test.local")
    assert scr403_notes.bundle_name(session) == f"notes_{session.session_id}"


def test_save_bundle_registers_artifacts(tmp_path: Path, monkeypatch):
    """Комплект замечаний сохраняется в артефакты сессии тремя файлами (`FR-P-40`)."""
    session: SessionModel = new_session(base_url="http://test.local")
    monkeypatch.setattr(scr403_notes, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(scr403_notes, "ensure_dirs", lambda: None)

    path = scr403_notes.save_bundle(session, [_note()])

    assert path.parent == tmp_path and path.exists()
    kinds = [item["kind"] for item in session.artifacts]
    assert kinds == ["notes_md", "notes_json", "notes_csv"]
    assert len(list(tmp_path.iterdir())) == 3


# ---------------------------------------------------------------------------
# Запреты экрана (`IR-P-4`, `DR-P-5`)
# ---------------------------------------------------------------------------
def test_notes_screen_has_no_run_commands():
    """Замечания не запускают проверки: проверка исправления — на «Прогоне» (`IR-P-4`)."""
    source = pathlib.Path(str(scr403_notes.__file__)).read_text(encoding="utf-8")

    for command in RUN_COMMANDS:
        assert command not in source, f"в разборе найдена команда запуска {command}"
    assert "queue.begin(" not in source
    assert "results_api.record(" not in source, "экран замечаний не меняет результаты (`DR-P-5`)"
