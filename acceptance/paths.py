"""Каталоги данных пульта испытаний.

Все данные испытаний лежат в `acceptance_data/` (каталог в `.gitignore`):
    sessions/   — файлы сессий испытаний (JSON), по одному на сессию;
    logs/       — структурные логи сессий (`session_<id>.jsonl`) и подкаталог
                  `applog/` с текстовыми журналами приложения **по одному на запуск**
                  пульта (`app_log_path`);
    reports/    — сформированные отчёты (md/json/csv);
    artifacts/  — выгруженные доказательства (запрошенные выгрузки, манифесты).

Текстовый журнал приложения пишется отдельным файлом на каждый запуск сервера
пульта: одна длинная лента `app.log` неудобна для поиска фрагмента, а файл
запуска (`applog_<ГГГГММДД-ЧЧММСС>.log`) сразу ограничивает поиск своим запуском.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "acceptance_data"
SESSION_DIR = DATA_DIR / "sessions"
LOG_DIR = DATA_DIR / "logs"
REPORT_DIR = DATA_DIR / "reports"
ARTIFACT_DIR = DATA_DIR / "artifacts"

#: Каталог текстовых журналов запусков пульта (`logs/applog/`).
APP_LOG_DIR = LOG_DIR / "applog"

#: Маска имён журналов запусков (`applog_<ГГГГММДД-ЧЧММСС>.log`).
APP_LOG_PATTERN = "applog_*.log"


def app_log_path(run_id: str) -> Path:
    """Путь к текстовому журналу запуска пульта: `logs/applog/applog_<run_id>.log`."""
    return APP_LOG_DIR / f"applog_{run_id}.log"


def ensure_dirs() -> None:
    """Создаёт каталоги данных, если их нет (идемпотентно)."""
    for directory in (DATA_DIR, SESSION_DIR, LOG_DIR, APP_LOG_DIR, REPORT_DIR, ARTIFACT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def session_log_path(session_id: str) -> Path:
    """Путь к структурному журналу сессии (`logs/session_<id>.jsonl`)."""
    return LOG_DIR / f"session_{session_id}.jsonl"
