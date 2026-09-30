"""Фоновые прогоны проверок и состояние панели «Задачи» (шаг 4 плана M2).

Тяжёлые проверки (`train`, проверка модели, инференс по датасету) идут минутами:
синхронный HTTP-ответ держал бы браузер оператора и мешал работе. Поэтому пульт
запускает прогон **в фоне** и возвращает идентификатор задания
(`POST /api/run` с `background: true`), а панель «Задачи» опрашивает `GET /api/jobs`
и `GET /api/jobs/{job_id}`.

Решения, заложенные в модуль:

    * **одно активное задание**: запуск второго прогона отклоняется сообщением, а не
      встаёт в очередь — иначе оператор не понимает, что происходит на стенде
      (`NFR-P-5`: случайный двойной клик не должен приводить к двум прогонам);
    * задания живут в процессе пульта и **не переживают перезапуск**: доказательства
      прогона (строки результатов, вердикты, диапазон журнала) записываются в сессию
      на диске, а само задание после перезапуска уже не нужно;
    * ошибка задания не роняет пульт: текст попадает в `Job.error` и виден оператору.

Модуль чистый: он не знает ни о стенде, ни об интерфейсе — ему передают функцию
работы (`work`), возвращающую строки результатов и диапазон журнала.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

#: Статусы задания: ожидает → выполняется → выполнено | ошибка.
JOB_QUEUED = "ожидает"
JOB_RUNNING = "выполняется"
JOB_DONE = "выполнено"
JOB_FAILED = "ошибка"

#: Все статусы задания (для тестов и подписей).
JOB_STATUSES: tuple[str, ...] = (JOB_QUEUED, JOB_RUNNING, JOB_DONE, JOB_FAILED)

#: Сколько заданий держать в истории процесса.
JOB_LIMIT = 20


def _now_iso() -> str:
    """Отметка времени задания (секундная точность, как в сессии испытаний)."""
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Job:
    """Фоновый прогон пункта программы: что запускали, чем закончилось, сколько шло."""

    job_id: str
    check_id: str
    action: str
    label: str = ""
    status: str = JOB_QUEUED
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_ms: float = 0.0
    verdict: str = ""
    error: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    journal_from: int | None = None
    journal_to: int | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """Идёт ли задание прямо сейчас (ожидает запуска или выполняется)."""
        return self.status in (JOB_QUEUED, JOB_RUNNING)

    @property
    def icon(self) -> str:
        """Пиктограмма статуса для панели «Задачи»."""
        return {
            JOB_QUEUED: "⏳",
            JOB_RUNNING: "▶",
            JOB_DONE: "✔",
            JOB_FAILED: "✖",
        }.get(self.status, "?")

    def as_dict(self) -> dict[str, Any]:
        """Задание для API и панели (все поля, значения простые)."""
        return {
            "job_id": self.job_id,
            "check_id": self.check_id,
            "action": self.action,
            "label": self.label,
            "status": self.status,
            "icon": self.icon,
            "active": self.is_active,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": round(self.duration_ms, 1),
            "duration": _duration_text(self.duration_ms),
            "verdict": self.verdict,
            "error": self.error,
            "rows": list(self.rows),
            "journal_from": self.journal_from,
            "journal_to": self.journal_to,
            "params": dict(self.params),
        }


def _duration_text(duration_ms: float) -> str:
    """Длительность задания словами: «12,3 с» или «2 мин 05 с»."""
    if duration_ms <= 0:
        return "—"
    seconds = duration_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f} с".replace(".", ",")
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} мин {rest:02d} с"


class JobRunner:
    """Фоновые прогоны: одно активное задание, история последних `limit` заданий.

    Args:
        limit: сколько заданий держать в памяти (старые вытесняются).
        journal_seq: функция «номер последней записи журнала» — по ней фиксируется
            диапазон обмена, относящийся к заданию (доказательство прогона).
    """

    def __init__(
        self,
        *,
        limit: int = JOB_LIMIT,
        journal_seq: Callable[[], int | None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._jobs: list[Job] = []
        self._threads: dict[str, threading.Thread] = {}
        self._limit = max(1, int(limit))
        self._journal_seq = journal_seq

    # -- управление ----------------------------------------------------------
    def set_journal_seq(self, provider: Callable[[], int | None] | None) -> None:
        """Задаёт источник номера последней записи журнала (диапазон в доказательствах)."""
        self._journal_seq = provider

    def start(
        self,
        *,
        check_id: str,
        action: str,
        work: Callable[[], dict[str, Any]],
        label: str = "",
        params: dict[str, Any] | None = None,
    ) -> Job:
        """Запускает работу в фоне и сразу возвращает задание.

        Raises:
            RuntimeError: активное задание уже есть — второй прогон не запускается.
        """
        with self._lock:
            active = self._active_locked()
            if active is not None:
                raise RuntimeError(
                    f"Прогон уже идёт: {active.check_id or active.job_id} "
                    f"({active.status}). Дождитесь окончания или прервите задачу."
                )
            job = Job(
                job_id=uuid4().hex[:12],
                check_id=str(check_id),
                action=str(action),
                label=str(label),
                created_at=_now_iso(),
                params=dict(params or {}),
            )
            self._jobs.append(job)
            del self._jobs[: max(0, len(self._jobs) - self._limit)]
            thread = threading.Thread(
                target=self._run, args=(job, work), name=f"pult-job-{job.job_id}", daemon=True
            )
            self._threads[job.job_id] = thread
            thread.start()
            return job

    def get(self, job_id: str) -> Job | None:
        """Задание по идентификатору (None — такого задания не было)."""
        with self._lock:
            return next((job for job in self._jobs if job.job_id == job_id), None)

    def jobs(self) -> list[Job]:
        """Задания в порядке запуска (сначала новые)."""
        with self._lock:
            return list(reversed(self._jobs))

    def active(self) -> Job | None:
        """Активное задание или None, если пульт свободен."""
        with self._lock:
            return self._active_locked()

    def wait(self, job_id: str, timeout: float = 30.0) -> Job | None:
        """Ждёт завершения задания (скрипты и тесты): задание или None по таймауту."""
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(job_id)

    def _active_locked(self) -> Job | None:
        """Активное задание (вызывается под замком)."""
        return next((job for job in self._jobs if job.is_active), None)

    # -- выполнение ----------------------------------------------------------
    def _seq(self) -> int | None:
        """Номер последней записи журнала (None — журнала нет)."""
        if self._journal_seq is None:
            return None
        try:
            return self._journal_seq()
        except Exception:  # noqa: BLE001 — журнал не должен ломать задание
            return None

    def _run(self, job: Job, work: Callable[[], dict[str, Any]]) -> None:
        """Выполняет работу задания и записывает итог (ошибки не поднимаются наружу)."""
        job.status = JOB_RUNNING
        job.started_at = _now_iso()
        job.journal_from = self._seq()
        started = time.monotonic()
        try:
            result = work() or {}
            job.rows = [dict(row) for row in (result.get("rows") or [])]
            job.verdict = str(result.get("verdict") or "")
            if result.get("journal_from") is not None:
                job.journal_from = int(result["journal_from"])
            job.journal_to = (
                result.get("journal_to") if result.get("journal_to") is not None else self._seq()
            )
            job.status = JOB_DONE
        except Exception as exc:  # noqa: BLE001 — пульт продолжает работу
            job.status = JOB_FAILED
            job.error = str(exc).strip() or exc.__class__.__name__
            job.verdict = f"задание не выполнено: {job.error}"
        finally:
            job.ended_at = _now_iso()
            job.duration_ms = (time.monotonic() - started) * 1000.0


def verdict_of(rows: list[dict[str, Any]]) -> str:
    """Короткий итог задания по строкам результатов (для панели «Задачи»)."""
    if not rows:
        return "нет результатов"
    if len(rows) == 1:
        return str(rows[0].get("verdict") or rows[0].get("status") or "")
    done = [str(row.get("status") or "") for row in rows]
    return f"пунктов: {len(rows)} ({', '.join(done)})"
