"""Тесты экрана «Журнал обмена» (`SCR-402`).

Журнал — машинный след испытаний (`FR-P-28`, `FR-P-57`, `DR-P-4`): по нему разбирают прогон
и воспроизводят результат вне пульта. Здесь держатся правила экрана:

    * выборка по метке даёт полный след проверки, а порядок — «свежие сверху» (разбор идёт
      с последнего обмена);
    * тела усечены лимитом с пометкой, а не потеряны (`NFR-P-4`);
    * собственной команды запуска у экрана нет: повтор — «▶ Следующая» на `SCR-301` (`IR-P-4`).

Записи журнала создаются вручную (`HttpExchange`), поэтому проверка не зависит от стенда.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

from acceptance.http_log import HttpExchange
from acceptance.session import TestSession as SessionModel
from acceptance.session import new_session
from acceptance.ui.screens import scr402_journal

#: Команды запуска: в журнале их быть не должно (`IR-P-4`).
RUN_COMMANDS = (
    "execute_next",
    "run_batch_tech",
    "auto_step",
    "execute_call",
    "execute_check",
    "from acceptance.runner",
)

#: Метки проверок для выборки журнала.
LABEL_A = "TC-FILE-13"
LABEL_B = "TC-SYS-01"


def _record(
    seq: int,
    *,
    label: str = LABEL_A,
    method: str = "GET",
    path: str = "/api/data/files",
    query: str = "",
    status: int | None = 200,
    duration_ms: float = 412.0,
    error: str | None = None,
    request_bytes: int | None = 128,
    response_bytes: int | None = 2048,
    truncated: bool = False,
) -> HttpExchange:
    """Запись обмена для тестов: значения по умолчанию — успешный ответ `200`."""
    return HttpExchange(
        seq=seq,
        started_at="2026-09-22T12:02:14.221",
        method=method,
        path=path,
        query=query,
        status=status,
        duration_ms=duration_ms,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        content_type="application/json",
        error=error,
        label=label,
        request_body='{"file_name": "тест"}',
        response_body='{"count": 1}',
        body_truncated=truncated,
    )


def _records() -> list[HttpExchange]:
    """Три обмена: успешный по одной метке, ошибка 404 и обмен вне прогона (без метки)."""
    return [
        _record(214, label=LABEL_A, duration_ms=412.0),
        _record(215, label=LABEL_B),
        _record(
            216,
            label=LABEL_A,
            path="/api/data/file/несущ./download",
            status=404,
            error=None,
            duration_ms=88.0,
        ),
    ]


# ---------------------------------------------------------------------------
# Строки журнала, фильтры и сводка
# ---------------------------------------------------------------------------
def test_screen_rows_describe_exchange():
    """Строка журнала: номер, время, знак исхода, статус, метод, путь, метка, мс и объём."""
    rows = scr402_journal.screen_rows(_records())

    assert [row["seq"] for row in rows] == [214, 215, 216]
    first = rows[0]
    assert first["mark"] == "✅"
    assert first["status"] == "200"
    assert first["method"] == "GET"
    assert first["path"] == "/api/data/files"
    assert first["label"] == LABEL_A
    assert first["duration"] == "412 мс"
    assert "128 Б" in first["size"] and "2 КБ" in first["size"]
    assert first["at"] == "12:02:14"

    failed = rows[2]
    assert failed["mark"] == "❌"
    assert failed["status"] == "404"


def test_screen_rows_show_network_error_and_query():
    """Ошибка соединения показывается текстом, query-строка — частью пути (`DR-P-4`)."""
    record = _record(1, status=None, error="таймаут 15 с", query="limit=10")

    row = scr402_journal.screen_rows([record])[0]

    assert row["mark"] == "❌"
    assert row["status"] == "таймаут 15 с"
    assert row["path"] == "/api/data/files?limit=10"
    assert row["error"] == "таймаут 15 с"


def test_bytes_text_formats_sizes():
    """Объёмы показываются в байтах, килобайтах и мегабайтах."""
    assert scr402_journal.bytes_text(0) == "0 Б"
    assert scr402_journal.bytes_text(512) == "512 Б"
    assert scr402_journal.bytes_text(2048) == "2 КБ"
    assert scr402_journal.bytes_text(131072) == "128 КБ"
    assert scr402_journal.bytes_text(None) == ""


def test_label_options_list_known_labels():
    """Фильтр по метке перечисляет метки обменов; обмен без метки в список не попадает."""
    records = [*_records(), _record(217, label="")]

    assert scr402_journal.label_options(records) == (scr402_journal.ANY, LABEL_A, LABEL_B)
    assert scr402_journal.label_options([]) == (scr402_journal.ANY,)


def test_filter_records_by_label_errors_and_needle():
    """Выборка журнала: метка, «только ошибки», подстрока; порядок — свежие сверху."""
    records = _records()

    all_rows = scr402_journal.filter_records(records)
    assert [item.seq for item in all_rows] == [216, 215, 214], "свежие обмены сверху"

    by_label = scr402_journal.filter_records(records, label=LABEL_A)
    assert [item.seq for item in by_label] == [216, 214]

    errors = scr402_journal.filter_records(records, only_errors=True)
    assert [item.seq for item in errors] == [216]

    found = scr402_journal.filter_records(records, needle="несущ.")
    assert [item.seq for item in found] == [216]
    assert scr402_journal.filter_records(records, needle="нет-такого-текста") == []
    assert len(scr402_journal.filter_records(records, limit=1)) == 1
    assert len(scr402_journal.filter_records(records, label=scr402_journal.ANY)) == 3


def test_summary_and_truncation_notes():
    """Сводка журнала считает обмены, ошибки, максимум и объёмы (`NFR-P-4`)."""
    records = _records()
    note = scr402_journal.summary_note(records)

    assert "Обменов 3" in note
    assert "ошибок 1" in note
    assert "макс 412 мс" in note
    assert "передано" in note and "получено" in note
    assert scr402_journal.truncation_note(records) == ""

    cut = scr402_journal.truncation_note([_record(1, truncated=True)])

    assert "усечены" in cut and "NFR-P-4" in cut


# ---------------------------------------------------------------------------
# Выгрузки: JSONL следа, артефакты сессии
# ---------------------------------------------------------------------------
def test_records_jsonl_is_one_line_per_exchange():
    """Выгрузка выборки — `JSONL`: по одному обмену на строку, без экранирования кириллицы."""
    text = scr402_journal.records_jsonl(_records()[:2])
    lines = [json.loads(line) for line in text.splitlines()]

    assert [item["seq"] for item in lines] == [214, 215]
    assert "тест" in text, "кириллица выгружается читаемой"
    assert lines[0]["path"] == "/api/data/files"


def test_trace_text_prefers_session_log_file(tmp_path: Path):
    """След сессии берётся из файла журнала, если он есть; иначе — из записей ленты."""
    session_log = tmp_path / "session_demo.jsonl"
    session_log.write_text('{"event": "pult_started"}\n', encoding="utf-8")

    assert "pult_started" in scr402_journal.trace_text(session_log, _records())
    assert "pult_started" not in scr402_journal.trace_text(tmp_path / "нет.jsonl", _records())
    assert '"seq": 214' in scr402_journal.trace_text(None, _records())


def test_trace_name_uses_session_id():
    """Имя файла следа называет сессию; без сессии — общее имя журнала."""
    session: SessionModel = new_session(base_url="http://test.local")

    assert scr402_journal.trace_name(session) == f"session_{session.session_id}.jsonl"
    assert scr402_journal.trace_name(None) == "journal.jsonl"


def test_save_selection_registers_artifact(tmp_path: Path, monkeypatch):
    """Сохранение выборки кладёт `JSONL` в артефакты и регистрирует его в сессии (`FR-P-28`)."""
    session: SessionModel = new_session(base_url="http://test.local")
    monkeypatch.setattr(scr402_journal, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(scr402_journal, "ensure_dirs", lambda: None)

    path = scr402_journal.save_selection(session, _records(), label=LABEL_A)

    assert path.parent == tmp_path
    assert LABEL_A in path.name, "метка выборки видна в имени файла"
    assert '"seq": 214' in path.read_text(encoding="utf-8")
    assert session.artifacts, "выгрузка не попала в артефакты сессии"
    assert session.artifacts[-1]["kind"] == "journal_jsonl"
    assert LABEL_A in session.artifacts[-1]["note"]


def test_detail_level_falls_back_to_known_level():
    """Уровень подробности берётся из ядра: незнакомый заменяется ближайшим доступным."""
    assert scr402_journal.detail_level(scr402_journal.REQUEST_LEVEL, fallback=1) == (
        scr402_journal.REQUEST_LEVEL
    )
    assert scr402_journal.detail_level("нет-такого", fallback=3) == "тело"
    assert scr402_journal.detail_level("нет-такого", fallback=99) == "полностью"


# ---------------------------------------------------------------------------
# Запреты журнала (`IR-P-4`, `DR-P-5`)
# ---------------------------------------------------------------------------
def test_journal_screen_has_no_run_commands():
    """Журнал не запускает проверки: повтор — только на «Прогоне» (`IR-P-4`)."""
    source = pathlib.Path(str(scr402_journal.__file__)).read_text(encoding="utf-8")

    for command in RUN_COMMANDS:
        assert command not in source, f"в журнале найдена команда запуска {command}"
    assert "queue.begin(" not in source
    assert "results_api.record(" not in source, "журнал не пишет результаты (`DR-P-5`)"
    assert "queue.finish(" not in source
