"""Тесты снимка статусов и архива задач (FR-T11, этап T4).

`GET /api/tasks/` статуса задачи не возвращает (в спецификации у `CeleryTask` только
`type`, `name`, `description`, `id`, `created_at`), поэтому пульт запоминает последний
известный серверный статус в `acceptance_data/task_snapshot.json` и ведёт там архив
задач. Тесты фиксируют правила снимка:

    * приоритет и сохранность данных (пустое значение не затирает известное);
    * авто-архив завершённых старше суток и возврат из архива, если задача снова
      в работе (иначе в архиве видно то, что исполняется сейчас);
    * вид архива по умолчанию — «завершена ≥ суток назад», «только что перенесённые»
      показываются по флажку;
    * устойчивость чтения файла: повреждённый файл не роняет экран «Задачи».
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from acceptance import task_snapshot as snapshot_api
from acceptance.task_snapshot import (
    ARCHIVE_AFTER,
    ARCHIVED_BY_AUTO,
    ARCHIVED_BY_OPERATOR,
    SNAPSHOT_FILENAME,
    TaskSnapshot,
    archive_summary,
    load_snapshot,
    save_snapshot,
)

TASK = "9f0a5b7e-0000-4000-8000-000000000001"
TASK2 = "9f0a5b7e-0000-4000-8000-000000000002"
NOW = datetime(2026, 9, 16, 12, 0, 0)


def stamp(moment: datetime) -> str:
    """Отметка времени в том виде, в каком её отдаёт API (`isoformat`)."""
    return moment.isoformat()


def finished(moment: datetime, status: str = "completed") -> TaskSnapshot:
    """Снимок с одной завершённой задачей (`end_time` — заданный момент)."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status=status, end_time=stamp(moment), start_time=stamp(moment))
    return snapshot


def test_observe_keeps_last_known_values():
    """Пустое значение не затирает известное: ошибка карточки не стирает статус."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status="running", type="celery-test", name="probe")
    first = snapshot.entries[TASK].fetched_at

    snapshot.observe(TASK, error="сервер недоступен")

    record = snapshot.entries[TASK]
    assert record.status == "running"
    assert record.type == "celery-test"
    assert record.name == "probe"
    assert record.error == "сервер недоступен"
    assert record.fetched_at >= first
    assert snapshot.updated_at == record.fetched_at


def test_mark_present_tracks_tasks_missing_from_the_list():
    """`served` показывает, есть ли задача в текущей выдаче сервера."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status="running")
    snapshot.observe(TASK2, status="completed")

    snapshot.mark_present({TASK: ("celery-test", "probe")})

    assert snapshot.entries[TASK].served is True
    assert snapshot.entries[TASK2].served is False
    assert snapshot.entries[TASK].type == "celery-test"


def test_refresh_auto_archives_only_finished_older_than_a_day():
    """Авто-архив: завершённые старше суток — в архив, активные — нет."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status="completed", end_time=stamp(NOW - ARCHIVE_AFTER * 2))
    snapshot.observe(TASK2, status="running", end_time=stamp(NOW - ARCHIVE_AFTER * 2))

    archived, unarchived = snapshot.refresh_auto(now=NOW)

    assert archived == [TASK]
    assert unarchived == []
    assert snapshot.entries[TASK].archived is True
    assert snapshot.entries[TASK].archived_by == ARCHIVED_BY_AUTO
    assert snapshot.entries[TASK2].archived is False


def test_refresh_auto_keeps_recently_finished_tasks_active():
    """Завершённая только что задача остаётся в активных до истечения суток."""
    snapshot = finished(NOW - timedelta(hours=1))

    archived, unarchived = snapshot.refresh_auto(now=NOW)

    assert (archived, unarchived) == ([], [])
    assert snapshot.entries[TASK].archived is False


def test_refresh_auto_returns_task_to_work():
    """Задача, вернувшаяся в работу, выходит из архива — иначе архив врёт оператору."""
    snapshot = finished(NOW - ARCHIVE_AFTER * 3)
    snapshot.refresh_auto(now=NOW)
    assert snapshot.entries[TASK].archived is True

    snapshot.observe(TASK, status="running")

    archived, unarchived = snapshot.refresh_auto(now=NOW)

    assert (archived, unarchived) == ([], [TASK])
    assert snapshot.entries[TASK].archived is False
    assert snapshot.entries[TASK].archived_at == ""


def test_refresh_auto_needs_completion_time():
    """Терминальный статус без времени завершения не архивируется: сравнивать нечего."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status="failed")

    archived, _ = snapshot.refresh_auto(now=NOW)

    assert archived == []
    assert snapshot.entries[TASK].archived is False


def test_archive_completed_moves_terminal_tasks_once():
    """Кнопка оператора переносит завершённые задачи и не повторяет перенос."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status="completed", end_time=stamp(NOW))
    snapshot.observe(TASK2, status="running", end_time=stamp(NOW))

    moved = snapshot.archive_completed(by=ARCHIVED_BY_OPERATOR)

    assert moved == [TASK]
    assert snapshot.entries[TASK].archived_by == ARCHIVED_BY_OPERATOR
    assert snapshot.entries[TASK2].archived is False
    assert snapshot.archive_completed() == []
    assert snapshot.archive_candidates() == []


def test_archived_entries_hide_fresh_ones_by_default():
    """Вид архива: по умолчанию «завершена ≥ суток назад», флажок добавляет свежие."""
    old = finished(NOW - ARCHIVE_AFTER * 3)
    old.archive(TASK, by=ARCHIVED_BY_AUTO)
    old.observe(TASK2, status="completed", end_time=stamp(NOW - timedelta(minutes=5)))
    old.archive(TASK2, by=ARCHIVED_BY_OPERATOR)

    assert [record.task_id for record in old.archived_entries(now=NOW)] == [TASK]
    assert {record.task_id for record in old.archived_entries(now=NOW, include_fresh=True)} == {
        TASK,
        TASK2,
    }


def test_is_aged_separates_finished_older_than_a_day():
    """Правило вида архива: «завершена ≥ суток назад», без времени — только ручной перенос.

    Экран «Задачи» показывает свежеперенесённые записи по флажку, поэтому правило
    вынесено в один метод: по нему же решается, видна ли задача в виде «📦 Архив».
    """
    snapshot = TaskSnapshot()
    fresh = snapshot.observe(TASK, status="completed", end_time=stamp(NOW - timedelta(hours=2)))
    aged = snapshot.observe(TASK2, status="completed", end_time=stamp(NOW - ARCHIVE_AFTER * 2))
    manual = snapshot.observe(
        "9f0a5b7e-0000-4000-8000-000000000003", status="completed", end_time=""
    )
    manual.archived_at = stamp(NOW)
    manual.archived_by = ARCHIVED_BY_OPERATOR
    manual.end_time = ""

    assert snapshot.is_aged(fresh, now=NOW) is False
    assert snapshot.is_aged(aged, now=NOW) is True
    assert snapshot.is_aged(manual, now=NOW) is True


def test_archived_entries_are_sorted_by_completion():
    """Свежие по завершению записи архива идут первыми."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status="completed", end_time=stamp(NOW - ARCHIVE_AFTER * 5))
    snapshot.observe(TASK2, status="failed", end_time=stamp(NOW - ARCHIVE_AFTER * 2))
    snapshot.archive_completed()

    assert [record.task_id for record in snapshot.archived_entries(now=NOW)] == [TASK2, TASK]


def test_kpi_counts_states():
    """Показатели списка: активные, завершённые за сутки, архив и «только что»."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status="running")
    snapshot.observe(TASK2, status="completed", end_time=stamp(NOW - timedelta(hours=2)))
    snapshot.observe("9f0a5b7e-0000-4000-8000-000000000003")
    snapshot.archive(TASK2, by=ARCHIVED_BY_OPERATOR)

    kpi = snapshot.kpi(now=NOW)

    assert kpi == {
        "known": 2,
        "unknown": 1,
        "active": 1,
        "finished_today": 1,
        "archived": 1,
        "archived_fresh": 1,
    }


def test_snapshot_round_trip_on_disk(tmp_path: Path):
    """Снимок сохраняется и читается: архив и статусы переживают перезапуск пульта."""
    snapshot = finished(NOW - ARCHIVE_AFTER * 3)
    snapshot.archive_completed()

    path = save_snapshot(snapshot, tmp_path)
    restored = load_snapshot(tmp_path)

    assert path.name == SNAPSHOT_FILENAME
    assert restored.updated_at == snapshot.updated_at
    assert restored.entries[TASK].status == "completed"
    assert restored.entries[TASK].archived is True
    assert restored.entries[TASK].archived_by == ARCHIVED_BY_OPERATOR


def test_load_snapshot_tolerates_missing_and_broken_files(tmp_path: Path):
    """Отсутствующий или повреждённый файл — пустой снимок, а не падение экрана."""
    assert load_snapshot(tmp_path).entries == {}

    path = tmp_path / SNAPSHOT_FILENAME
    path.write_text("{ не json", encoding="utf-8")

    assert load_snapshot(tmp_path).entries == {}

    path.write_text('{"entries": {"' + TASK + '": {"status": "running"}}}', encoding="utf-8")

    restored = load_snapshot(tmp_path)
    assert restored.entries[TASK].status == "running"


def test_archive_summary_reports_volume_and_fresh_tasks():
    """Подпись переноса: сколько всего и сколько завершилось за последние сутки."""
    snapshot = TaskSnapshot()
    snapshot.observe(TASK, status="completed", end_time=stamp(NOW - timedelta(minutes=10)))
    snapshot.observe(TASK2, status="completed", end_time=stamp(datetime.now() - timedelta(days=3)))

    text = archive_summary([TASK, TASK2], snapshot)

    assert "перенесено в архив: 2" in text
    assert "за последние сутки: 1" in text
    assert archive_summary([], snapshot).startswith("переносить нечего")


def test_snapshot_module_exposes_archive_rule():
    """Правило архива объявлено в одном месте и видно документации и тестам.

    В `TERMINAL_STATUSES` входят все терминальные состояния FSM-1, включая
    `not_found`: карточка подтвердила, что задачи нет, и держать её в активных
    незачем (в архив она уйдёт только при известном времени завершения).
    """
    assert ARCHIVE_AFTER == timedelta(days=1)
    assert snapshot_api.ARCHIVE_AFTER.total_seconds() == 24 * 3600
    assert set(snapshot_api.TERMINAL_STATUSES) == {
        "completed",
        "failed",
        "interrupted",
        "not_found",
    }
