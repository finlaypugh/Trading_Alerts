@echo off
REM run.bat - set up (once) and launch the signal bot for the configured
REM ticker, on Windows.
REM
REM Usage:
REM   1. Copy .env.example to .env and fill in DISCORD_WEBHOOK_URL (and any
REM      overrides you want, e.g. SIGNAL_INTERVAL).
REM   2. Double-click run.bat, or run it from Command Prompt / PowerShell.
REM
REM Re-running this script is safe: it reuses the existing venv and only
REM reinstalls deps if requirements.txt changed.

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

if "%DISCORD_WEBHOOK_URL%"=="" (
    echo DISCORD_WEBHOOK_URL is not set. Add it to .env before running.
    exit /b 1
)

REM No default. A silent fallback would run a strategy tuned for one
REM instrument against whatever the fallback happens to be.
if "%SIGNAL_TICKER%"=="" (
    echo SIGNAL_TICKER is not set. Add it to .env before running ^(e.g. GC=F^).
    exit /b 1
)

echo Starting signal bot for %SIGNAL_TICKER%...
python signal_bot.py

endlocal