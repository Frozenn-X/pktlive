@echo off
setlocal
REM Launcher (Windows CMD) — delegates to run.py
REM Usage: run [--interval 1] [--no-dashboard] [--verbose]
REM Exit: propagates run.py exit code

cd /d "%~dp0"

REM ── Resolve Python ──
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] No python found in PATH. >&2
        echo         Install Python ^>= 3.9 or create a venv: python -m venv .venv >&2
        exit /b 1
    )
    set "PY=python"
)

REM ── Python version gate ──
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>nul
if errorlevel 1 (
    echo [ERROR] Python ^>= 3.9 required. >&2
    "%PY%" --version
    exit /b 1
)

"%PY%" run.py %*
exit /b %ERRORLEVEL%
