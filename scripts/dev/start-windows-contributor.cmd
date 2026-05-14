@echo off
REM Daily launcher for the Potluck contributor sidecar on Windows.
REM Double-click to start; close the window (or Ctrl-C) to stop.
REM
REM Reads env vars from apps\desktop\python\.env, which setup-windows-contributor.ps1
REM writes during bootstrap.

setlocal

REM Resolve repo root from this script's location (scripts\ is one below root).
set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."
set "SIDECAR_DIR=%REPO_ROOT%\apps\desktop\python"
set "VENV_PY=%SIDECAR_DIR%\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo.
    echo ERROR: venv python not found at %VENV_PY%
    echo Run scripts\setup-windows-contributor.ps1 first.
    echo.
    pause
    exit /b 1
)

if not exist "%SIDECAR_DIR%\.env" (
    echo.
    echo ERROR: %SIDECAR_DIR%\.env is missing.
    echo Run scripts\setup-windows-contributor.ps1 first to write it.
    echo.
    pause
    exit /b 1
)

cd /d "%SIDECAR_DIR%"
echo Starting Potluck contributor (Ctrl-C or close window to stop)...
echo.

"%VENV_PY%" -m potluck_local.contributor --verbose

echo.
echo Sidecar exited. Press any key to close this window.
pause >nul
