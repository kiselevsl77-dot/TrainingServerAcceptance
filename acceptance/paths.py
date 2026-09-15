"""Каталоги данных пульта испытаний.

Все данные испытаний лежат в `acceptance_data/` (каталог в `.gitignore`):
    sessions/   — файлы сессий испытаний (JSON), по одному на сессию;
    logs/       — журналы приложения (`app.log`) и структурные логи сессий (`session_<id>.jsonl`);
    reports/    — сформированные отчёты (md/json/csv);
    artifacts/  — выгруженные доказательства (запрошенные выгрузки, манифесты).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "acceptance_data"
SESSION_DIR = DATA_DIR / "sessions"
LOG_DIR = DATA_DIR / "logs"
REPORT_DIR = DATA_DIR / "reports"
ARTIFACT_DIR = DATA_DIR / "artifacts"

DEFAULT_APP_LOG = LOG_DIR / "app.log"


def ensure_dirs() -> None:
    """Создаёт каталоги данных, если их нет (идемпотентно)."""
    for directory in (DATA_DIR, SESSION_DIR, LOG_DIR, REPORT_DIR, ARTIFACT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def session_log_path(session_id: str) -> Path:
    """Путь к структурному журналу сессии (`logs/session_<id>.jsonl`)."""
    return LOG_DIR / f"session_{session_id}.jsonl"
