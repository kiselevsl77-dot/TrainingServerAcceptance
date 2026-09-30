"""Тесты фонового слоя пульта: задания прогонов и строки панели «Задачи» (шаг 4).

Фоновые задания (`pult/jobs.py`) запускают проверку в отдельном потоке, поэтому
проверяется то, что видит оператор: статус, длительность, вердикт, диапазон журнала
и отказ на второй одновременный прогон. Представления панели (`pult/view.py`)
собирают строки из тех же данных, что приходят в API (`GET /api/jobs`).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from acceptance import session as session_api
from acceptance.tasks_monitor import TaskMonitor
from pult import jobs as jobs_api
from pult import view

TASK_ID = "11111111-2222-4333-8444-555555555555"


def _runner(values: list[int | None] | None = None) -> jobs_api.JobRunner:
    """Раннер с предсказуемым источником номера записи журнала."""
    queue = list(values or [None])
    return jobs_api.JobRunner(journal_seq=lambda: queue.pop(0) if len(queue) > 1 else queue[0])


def test_start_records_result_and_journal_range() -> None:
    """Задание выполняет работу, фиксирует вердикт, строки и диапазон журнала."""
    runner = _runner([10, 42])
    job = runner.start(
        check_id="TC-TR-02",
        action="next",
        label="TC-TR-02: Запуск обучения",
        work=lambda: {
            "rows": [{"check_id": "TC-TR-02", "status": "успех", "verdict": "обучение запущено"}],
            "verdict": "обучение запущено",
            "journal_to": 42,
        },
    )

    finished = runner.wait(job.job_id, timeout=5.0)

    assert finished is not None
    assert finished.status == jobs_api.JOB_DONE
    assert finished.is_active is False
    assert finished.icon == "✔"
    assert finished.verdict == "обучение запущено"
    assert finished.rows == [
        {"check_id": "TC-TR-02", "status": "успех", "verdict": "обучение запущено"}
    ]
    assert finished.journal_from == 10
    assert finished.journal_to == 42
    assert finished.duration_ms >= 0
    assert finished.started_at and finished.ended_at
    assert runner.active() is None


def test_second_run_is_refused_while_first_is_active() -> None:
    """Пока задание идёт, второй прогон не запускается (`NFR-P-5`)."""
    runner = jobs_api.JobRunner()
    release = threading.Event()

    def slow() -> dict[str, Any]:
        release.wait(5.0)
        return {"rows": [], "verdict": ""}

    first = runner.start(check_id="TC-TR-02", action="next", work=slow)
    try:
        assert runner.active() is not None
        try:
            runner.start(check_id="TC-TR-03", action="next", work=lambda: {})
        except RuntimeError as exc:
            assert "Прогон уже идёт" in str(exc)
            assert first.check_id in str(exc)
        else:  # pragma: no cover - ветка «второй запуск прошёл» недопустима
            raise AssertionError("второй прогон запустился одновременно с первым")
    finally:
        release.set()
        runner.wait(first.job_id, timeout=5.0)


def test_failure_is_recorded_and_does_not_break_runner() -> None:
    """Ошибка задания попадает в `error` и не роняет пульт: следующий прогон работает."""
    runner = jobs_api.JobRunner()

    def broken() -> dict[str, Any]:
        raise ValueError("стенд ответил 500")

    failed = runner.wait(
        runner.start(check_id="TC-TR-02", action="next", work=broken).job_id, timeout=5.0
    )

    assert failed is not None
    assert failed.status == jobs_api.JOB_FAILED
    assert failed.icon == "✖"
    assert failed.error == "стенд ответил 500"
    assert "задание не выполнено" in failed.verdict
    assert failed.is_active is False

    ok = runner.wait(
        runner.start(check_id="TC-TR-01", action="next", work=lambda: {"verdict": "ок"}).job_id,
        timeout=5.0,
    )
    assert ok is not None and ok.status == jobs_api.JOB_DONE


def test_history_keeps_last_jobs_newest_first() -> None:
    """История заданий ограничена: старые вытесняются, первым идёт новое."""
    runner = jobs_api.JobRunner(limit=2)
    for check_id in ("TC-TR-01", "TC-TR-02", "TC-TR-03"):
        runner.wait(
            runner.start(check_id=check_id, action="next", work=lambda: {"verdict": "ок"}).job_id,
            timeout=5.0,
        )

    jobs = runner.jobs()

    assert [job.check_id for job in jobs] == ["TC-TR-03", "TC-TR-02"]
    assert runner.get("нет-такого") is None


def test_job_as_dict_is_ready_for_panel() -> None:
    """Задание отдаёт интерфейсу все поля панели, включая подпись длительности."""
    runner = jobs_api.JobRunner()
    job = runner.wait(
        runner.start(
            check_id="TC-TR-02",
            action="next",
            work=lambda: {"verdict": "ок"},
            label="TC-TR-02: Запуск обучения",
            params={"epochs": 3, "U_A_signal": "<base64: 8192 симв.>"},
        ).job_id,
        timeout=5.0,
    )

    assert job is not None
    data = job.as_dict()

    assert data["check_id"] == "TC-TR-02"
    assert data["label"] == "TC-TR-02: Запуск обучения"
    assert data["status"] == jobs_api.JOB_DONE
    assert data["active"] is False
    assert data["icon"] == "✔"
    # длительность: «12,3 с», «2 мин 05 с» или «—» у мгновенного задания
    assert data["duration"] and isinstance(data["duration_ms"], float)
    assert data["params"]["epochs"] == 3
    assert data["rows"] == []
    assert set(data) >= {"job_id", "created_at", "started_at", "ended_at", "journal_to"}


def test_verdict_of_rows_gives_short_summary() -> None:
    """Итог задания: вердикт одной проверки или сводка пачки."""
    assert jobs_api.verdict_of([]) == "нет результатов"
    assert jobs_api.verdict_of([{"verdict": "успех"}]) == "успех"
    assert jobs_api.verdict_of([{"status": "успех"}, {"status": "пропущена"}]) == (
        "пунктов: 2 (успех, пропущена)"
    )


def test_mask_params_hides_long_values() -> None:
    """Длинные значения параметров (base64-сигналы) в интерфейс не печатаются."""
    masked = view.mask_params(
        {
            "dataset_id": "5f0a5b7e-0000-4000-8000-000000000001",
            "U_A_signal": "A" * 8192,
            "note": "B" * 100,
            "threshold": 0.5,
        }
    )

    assert masked["U_A_signal"] == "<base64: 8192 симв.>"
    assert masked["note"] == "<длинное значение: 100 симв.>"
    assert masked["threshold"] == 0.5
    assert str(masked["dataset_id"]).startswith("5f0a5b7e")
    assert view.mask_params(None) == {}


def test_jobs_view_names_active_run() -> None:
    """Панель прогонов: активное задание называется оператору, история остаётся."""
    active = jobs_api.Job(
        job_id="abc",
        check_id="TC-TR-02",
        action="next",
        label="TC-TR-02: Запуск обучения",
        status=jobs_api.JOB_RUNNING,
    )
    done = jobs_api.Job(job_id="def", check_id="TC-TR-01", action="next", status=jobs_api.JOB_DONE)

    data = view.jobs_view([active, done])

    assert [row["job_id"] for row in data["rows"]] == ["abc", "def"]
    assert data["active"]["job_id"] == "abc"
    assert "Прогон идёт" in data["note"]
    assert "TC-TR-02: Запуск обучения" in data["note"]
    assert view.jobs_view([])["note"] == "Фоновых прогонов ещё не было."
    assert "завершено" in view.jobs_view([done])["note"]


def _task_row(task_id: str, task_type: str, name: str, created_at: str) -> Any:
    """Запись списка задач стенда (`CeleryTask`): без статуса — как отдаёт сервер."""
    return type(
        "Task",
        (),
        {
            "id": task_id,
            "type": task_type,
            "name": name,
            "description": name,
            "created_at": created_at,
        },
    )()


def test_task_rows_show_status_commands_and_polling() -> None:
    """Строка наблюдения: статус, команды FSM-1, поллинг и привязка к проверке."""
    session = session_api.new_session(base_url="http://test.local")
    session_api.add_task(
        session,
        task_id=TASK_ID,
        task_type="celery-test",
        name="обучение stft_mlp",
        check_id="TC-TR-02",
        status="running",
        interval=2.0,
    )
    monitor = TaskMonitor(None)  # type: ignore[arg-type]  # стенд не нужен: читаем сессию

    rows = view.task_rows(monitor, session)

    assert len(rows) == 1
    row = rows[0]
    assert row["short_id"] == TASK_ID[:8]
    assert row["status"] == "running"
    assert row["status_label"]
    assert row["terminal"] is False
    assert row["actions"] == ["pause", "interrupt"], "команды берутся из FSM-1"
    assert row["check_id"] == "TC-TR-02"
    assert row["interval"] == 2.0

    listed = view.task_list_rows(
        monitor,
        session,
        [
            _task_row(TASK_ID, "celery-test", "обучение", "2026-09-30T12:00:00"),
            _task_row("other-task", "dataset-fill", "наполнение", "2026-09-30T12:05:00"),
        ],
    )

    assert listed[0]["status"] == "running"
    assert listed[0]["source"] == "опрос"
    assert listed[0]["observed"] is True
    assert listed[1]["status_label"] == "нет данных", "статуса в списке нет — честно «нет данных»"
    assert listed[1]["source"] == "нет данных"
    assert listed[1]["observed"] is False


def test_task_list_rows_without_session_are_unknown() -> None:
    """Без сессии статус списка неизвестен: источник «нет данных», не выдумка."""
    monitor = TaskMonitor(None)  # type: ignore[arg-type]
    rows = view.task_list_rows(monitor, None, [_task_row(TASK_ID, "celery-test", "x", "")])

    assert rows[0]["status"] == ""
    assert rows[0]["glyph"] == "?"
    assert rows[0]["observed"] is False
    assert rows[0]["actions"] == []


def test_wait_returns_none_for_unknown_job() -> None:
    """Ожидание неизвестного задания не висит: возвращается None."""
    runner = jobs_api.JobRunner()
    started = time.monotonic()

    assert runner.wait("нет-такого", timeout=0.2) is None
    assert time.monotonic() - started < 2.0
