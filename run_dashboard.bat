@echo off
REM run_dashboard.bat - set up (once) and launch the LAN dashboard, on Windows.
REM
REM Usage:
REM   1. Set DASHBOARD_TOKEN in .env (without it the dashboard is read-only).
REM   2. Double-click run_dashboard.bat, then open http://localhost:8080
REM
REM Runs alongside run.bat, not instead of it: the dashboard only reads the
REM bot's files, so stopping it never stops alerts. Start/stop/restart need
REM systemd and are refused here.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set VENV_DIR=.venv
set ENV_FILE=.env
set HASH_FILE=%VENV_DIR%\.requirements.hash

REM --- venv setup ---
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create venv. Make sure Python is installed and on PATH.
        exit /b 1
    )
)

call "%VENV_DIR%\Scripts\activate.bat"

REM --- compute a simple hash of requirements.txt to skip reinstalls ---
for /f "delims=" %%H in ('certutil -hashfile requirements.txt SHA256 ^| find /v "hash" ^| find /v "CertUtil"') do set CURRENT_HASH=%%H

set OLD_HASH=
if exist "%HASH_FILE%" set /p OLD_HASH=<"%HASH_FILE%"

if not "%CURRENT_HASH%"=="%OLD_HASH%" (
    echo Installing/updating dependencies...
    python -m pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed.
        exit /b 1
    )
    >"%HASH_FILE%" echo %CURRENT_HASH%
)

REM --- load .env if present ---
if not exist "%ENV_FILE%" (
    echo No .env file found. Copy .env.example to .env and fill it in first.
    exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    set "line=%%A"
    REM skip blank lines and comments
    if not "!line!"=="" if not "!line:~0,1!"=="#" (
        set "%%A=%%B"
    )
)

if "%SIGNAL_TICKER%"=="" (
    echo SIGNAL_TICKER is not set. Add it to .env so the dashboard finds the bot's files.
    exit /b 1
)

if "%DASHBOARD_TOKEN%"=="" (
    echo DASHBOARD_TOKEN is not set: starting read-only, all actions will be refused.
)

python -m dashboard

endlocal
