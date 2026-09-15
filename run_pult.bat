@echo off
setlocal

rem Change to the script directory so it works from anywhere.
cd /d "%~dp0"

set "VENV=.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "MARKER=%VENV%\.deps_installed"
set "PORT=8502"

rem Detect the system Python launcher.
set "SYS_PYTHON="
where python >nul 2>nul && set "SYS_PYTHON=python"
if not defined SYS_PYTHON (
    where py >nul 2>nul && set "SYS_PYTHON=py -3"
)
if not defined SYS_PYTHON (
    echo [error] Python 3.11+ not found. Install it and add it to PATH.
    exit /b 1
)

rem 1) Create the virtual environment if missing.
if not exist "%PYTHON%" (
    echo [setup] Creating virtual environment "%VENV%"...
    %SYS_PYTHON% -m venv "%VENV%"
    if errorlevel 1 (
        echo [error] Failed to create virtual environment.
        exit /b 1
    )
)

rem 2) Install runtime dependencies on first run.
if not exist "%MARKER%" (
    echo [setup] Installing dependencies from requirements.txt...
    "%PYTHON%" -m pip install --upgrade pip
    if errorlevel 1 exit /b 1
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 exit /b 1
    type nul > "%MARKER%"
)

rem 3) Bootstrap .env from the example if missing.
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo [setup] Created .env from .env.example - check TRAINING_SERVER_BASE_URL.
    )
)

rem 4) Run the acceptance console.
echo [run] Starting acceptance console: http://localhost:%PORT%
"%PYTHON%" -m streamlit run acceptance\app.py --server.port %PORT% %*
exit /b %errorlevel%
