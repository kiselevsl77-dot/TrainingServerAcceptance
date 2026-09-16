"""Тесты хелпера замечаний к API (FR-T7): шаблоны, фильтры, выгрузка.

Проверяется общий для всех экранов слой `acceptance.ui.common.api_notes`:
создание замечаний (вручную и из шаблона), дедупликация, сводка и выгрузка
в md/csv/json — то, из чего собирается раздел «Замечания к API» отчёта.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acceptance.notes import PRIORITIES, SOURCE_AUTO, SOURCE_OPERATOR
from acceptance.session import new_session
from acceptance.ui.common import api_notes


@pytest.fixture
def session():
    """Сессия испытаний в памяти (без записи на диск)."""
    value = new_session(base_url="http://test.local")
    value.info.operator_fio = "Иванов И.И."
    return value


@pytest.fixture(autouse=True)
def flashes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Изолирует тесты от Streamlit и диска: сообщения, сохранение, артефакты."""
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        api_notes, "set_flash", lambda level, message: captured.append((level, message))
    )
    monkeypatch.setattr(api_notes.state, "store_session", lambda session: session)
    monkeypatch.setattr(api_notes.st, "rerun", lambda: None)
    return captured


def _notes_sample() -> list[dict]:
    """Замечания P2/P0/P1 от оператора и из шаблона — для фильтров, сводок и выгрузок."""
    template = api_notes.note_from_template("task_id")
    assert template is not None
    return [
        api_notes.manual_note(
            "Игнорирование limit/offset у файлов",
            module="File Import",
            endpoint="GET /api/data/files",
            priority="P2",
            fact="Параметры limit/offset не влияют на ответ",
            expected="Серверная пагинация",
            reproduction="GET /api/data/files?limit=1",
        ).to_dict(),
        api_notes.manual_note(
            "Нет phase_connection в списке нагрузок",
            module="Loads",
            endpoint="GET /api/loads/list",
            priority="P0",
            fact="Поле отсутствует",
            expected="phase_connection обязателен",
            reproduction="GET /api/loads/list",
            check_id="TC-LOAD-01",
        ).to_dict(),
        template.to_dict(),
    ]


def test_templates_cover_known_defects():
    """Шаблоны известных дефектов API готовы к использованию оператором."""
    templates = api_notes.note_templates()
    titles = api_notes.template_titles()

    assert len(templates) == 11
    assert len(set(titles)) == len(titles)
    assert all(note.source == SOURCE_AUTO for note in templates)
    assert all(note.fact and note.expected and note.reproduction for note in templates)
    assert {note.module for note in templates} >= {"File Import", "Loads", "Datasets", "ML models"}


def test_find_template_by_partial_title_and_unknown_title():
    """Шаблон ищется по части заголовка; неизвестный заголовок даёт None."""
    assert api_notes.find_template("не-ASCII") is not None
    assert api_notes.find_template("task_id") is not None
    assert api_notes.find_template("такого дефекта нет") is None
    assert api_notes.note_from_template("такого дефекта нет") is None


def test_note_from_template_attaches_check_id_and_evidence():
    """Замечание из шаблона получает метку проверки и доказательства."""
    note = api_notes.note_from_template(
        "игнорирует limit/offset",
        check_id="TC-FILE-01",
        evidence="GET /api/data/files?limit=1 → 265 файлов",
    )

    assert note is not None
    assert note.check_id == "TC-FILE-01"
    assert "265 файлов" in note.evidence
    assert note.priority == "P1"
    assert note.module == "File Import"


def test_manual_note_normalizes_priority_and_source():
    """Ручное замечание: источник «оператор», приоритет приводится к известному."""
    note = api_notes.manual_note("Замечание оператора", priority="p0", module="Прочее")

    assert note.priority == "P0"
    assert note.source == SOURCE_OPERATOR

    wrong = api_notes.manual_note("Замечание оператора", priority="P9")
    assert wrong.priority == "P1"


def test_create_note_adds_to_session_and_deduplicates(session, flashes):
    """Замечание добавляется в сессию один раз (дедупликация по заголовку)."""
    note = api_notes.manual_note("Отсутствует updated_at", module="Прочее", priority="P1")

    assert api_notes.create_note(note, session=session, rerun=False) is True
    assert len(session.notes) == 1
    assert session.notes[0]["title"] == "Отсутствует updated_at"

    assert api_notes.create_note(note, session=session, rerun=False) is False
    assert len(session.notes) == 1
    assert [level for level, _ in flashes] == ["success", "info"]
    assert [item["event"] for item in session.history].count("api_note") == 1


def test_create_note_without_session_warns(session, flashes):
    """Без выбранной сессии замечание создаётся, но оператор получает предупреждение."""
    note = api_notes.manual_note("Замечание без сессии")

    assert api_notes.create_note(note, session=None, rerun=False) is False
    assert flashes[-1][0] == "warning"
    assert "не выбрана" in flashes[-1][1]


def test_create_note_from_template_reports_unknown_template(session, flashes):
    """Неизвестный шаблон — ошибка на экране, сессия не меняется."""
    assert (
        api_notes.create_note_from_template("нет такого дефекта", session=session, rerun=False)
        is False
    )
    assert session.notes == []
    assert flashes[-1][0] == "error"
    assert api_notes.NOT_FOUND_TEMPLATE in flashes[-1][1]


def test_create_note_from_template_adds_template(session, flashes):
    """Шаблон известного дефекта добавляется в сессию с меткой проверки."""
    added = api_notes.create_note_from_template(
        "Нет сущности «субдатасет»",
        session=session,
        check_id="TC-REC-06",
        evidence="пары RAW+markup объединяются только на клиенте",
        rerun=False,
    )

    assert added
    assert session.notes[0]["check_id"] == "TC-REC-06"
    assert "клиенте" in session.notes[0]["evidence"]
    assert session.notes[0]["source"] == SOURCE_AUTO


def test_notes_of_sorts_by_priority(session):
    """Замечания сессии отдаются в порядке отчёта: сначала P0."""
    sample = _notes_sample()
    session.notes.extend([sample[2], sample[0], sample[1]])  # P1, P2, P0

    ordered = api_notes.notes_of(session)

    assert [note["priority"] for note in ordered] == ["P0", "P1", "P2"]
    assert api_notes.notes_of(None) == []


def test_filter_notes_by_priority_module_source_link_and_search():
    """Фильтры реестра замечаний (приоритет, модуль, источник, привязка, поиск)."""
    notes = api_notes.notes_of(_Session_with(_notes_sample()))

    assert len(api_notes.filter_notes(notes, priority="P0")) == 1
    assert len(api_notes.filter_notes(notes, module="Loads")) == 1
    assert len(api_notes.filter_notes(notes, source=SOURCE_AUTO)) == 1
    assert len(api_notes.filter_notes(notes, linked="с привязкой к проверке")) == 1
    assert len(api_notes.filter_notes(notes, linked="без привязки")) == 2
    assert len(api_notes.filter_notes(notes, search="phase_connection")) == 1
    assert len(api_notes.filter_notes(notes, search="нет такого текста")) == 0
    assert len(api_notes.filter_notes(notes, priority="P1")) == 1


def _Session_with(notes: list[dict]):
    """Сессия в памяти с готовым списком замечаний (для фильтров и выгрузок)."""
    session = new_session(base_url="http://test.local")
    session.info.operator_fio = "Иванов И.И."
    session.notes = [dict(note) for note in notes]
    return session


def test_notes_rows_and_summary():
    """Таблица и сводка реестра: приоритеты, модули, источники, привязки."""
    notes = api_notes.notes_of(_Session_with(_notes_sample()))

    rows = api_notes.notes_rows(notes)
    summary = api_notes.notes_summary(notes)

    assert [row["№"] for row in rows] == [1, 2, 3]
    assert rows[0]["Приоритет"] == "P0"
    assert set(rows[0]) >= {"Модуль", "Замечание", "Эндпоинт", "Проверка", "Источник", "id"}
    assert summary["total"] == 3
    assert summary["by_priority"] == {"P0": 1, "P1": 1, "P2": 1}
    assert summary["by_module"]["Loads"] == 1
    assert summary["by_source"][SOURCE_AUTO] == 1
    assert summary["linked_to_checks"] == 1
    assert summary["without_check"] == 2
    assert set(summary["by_priority"]) == set(PRIORITIES)


def test_notes_exports_json_csv_and_markdown():
    """Выгрузки реестра: JSON (машинный), CSV (Excel), Markdown (раздел отчёта)."""
    notes = api_notes.notes_of(_Session_with(_notes_sample()))
    meta = {
        "session_id": "20260916-101500-abcd",
        "server_build": "dev@58ac72f12345",
        "base_url": "https://energomera.ai-center.online",
        "operator": "Иванов И.И.",
    }

    payload = json.loads(api_notes.notes_to_json(notes, meta))
    assert payload["meta"]["session_id"] == "20260916-101500-abcd"
    assert payload["summary"]["total"] == 3
    assert len(payload["notes"]) == 3
    assert payload["prospective_requirements"]

    csv_text = api_notes.notes_to_csv(notes)
    assert csv_text.splitlines()[0].startswith("note_id,priority,module,title")
    assert "phase_connection" in csv_text

    markdown = api_notes.notes_to_markdown(notes, meta=meta)
    assert markdown.startswith("## Замечания к API")
    assert "### P0 — блокирует корректную работу UI/проверки" in markdown
    assert "Всего замечаний: 3 — P0: 1, P1: 1, P2: 1" in markdown
    assert "dev@58ac72f12345" in markdown
    assert "**P1-1." in markdown
    assert "| № | Замечание | Модуль | Эндпоинт | Проверка | Источник |" in markdown

    assert "Замечаний к API нет." in api_notes.notes_to_markdown([])


def test_markdown_cells_are_single_line():
    """Поля выгрузки не ломают таблицу Markdown (переносы строк и `|`)."""
    note = api_notes.manual_note(
        "Заголовок",
        fact="первая строка\nвторая строка | с разделителем",
        module="Прочее",
    ).to_dict()

    markdown = api_notes.notes_to_markdown([note])

    assert "первая строка вторая строка" in markdown
    assert "\\|" in markdown
    assert sum(1 for line in markdown.splitlines() if line.startswith("|")) == 3


def test_save_notes_export_writes_three_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Сохранение реестра в артефакты: md, csv (utf-8-sig) и json."""
    monkeypatch.setattr(api_notes, "ARTIFACT_DIR", tmp_path)
    notes = api_notes.notes_of(_Session_with(_notes_sample()))

    paths = api_notes.save_notes_export(notes, meta={"session_id": "s-1"}, stamp="20260916-101500")

    assert set(paths) == {"markdown", "csv", "json"}
    assert paths["markdown"].name == "api_notes_20260916-101500.md"
    assert all(path.exists() and path.stat().st_size > 0 for path in paths.values())
    assert paths["csv"].read_bytes().startswith(b"\xef\xbb\xbf")  # BOM для Excel
    assert "Замечания к API" in paths["markdown"].read_text(encoding="utf-8")
    assert json.loads(paths["json"].read_text(encoding="utf-8"))["notes"]


def test_remove_note_deletes_and_writes_history(session):
    """Удаление ошибочного замечания: из сессии и с записью в историю."""
    note = api_notes.manual_note("Лишнее замечание")
    session.notes.append(note.to_dict())

    assert api_notes.remove_note(session, note.note_id) is True
    assert session.notes == []
    assert api_notes.remove_note(session, note.note_id) is False
    assert any(item["event"] == "api_note_removed" for item in session.history)


def test_prospective_rows_and_priority_hints():
    """Перспективные требования и пояснения приоритетов для интерфейса."""
    rows = api_notes.prospective_rows()

    assert len(rows) == 5
    assert any("субдатасет" in row["title"].lower() for row in rows)
    assert api_notes.priority_hint("P0") == "блокирует корректную работу UI/проверки"
    assert api_notes.priority_hint("p2").startswith("улучшение")
    assert api_notes.priority_hint("P9") == ""
