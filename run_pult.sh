#!/usr/bin/env bash
# Запуск пульта испытаний (Linux/macOS): создаёт .venv, ставит зависимости, стартует Streamlit.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PYTHON="$VENV/bin/python"
MARKER="$VENV/.deps_installed"
PORT=8502

if [ ! -x "$PYTHON" ]; then
    echo "[setup] Creating virtual environment \"$VENV\"..."
    python3 -m venv "$VENV"
fi

if [ ! -f "$MARKER" ]; then
    echo "[setup] Installing dependencies from requirements.txt..."
    "$PYTHON" -m pip install --upgrade pip
    "$PYTHON" -m pip install -r requirements.txt
    touch "$MARKER"
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[setup] Created .env from .env.example - check TRAINING_SERVER_BASE_URL."
fi

echo "[run] Starting acceptance console: http://localhost:$PORT"
exec "$PYTHON" -m streamlit run acceptance/app.py --server.port "$PORT" "$@"
