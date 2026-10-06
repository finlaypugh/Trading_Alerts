@echo off
REM run_resolver.bat - resolve the bot's alert outcomes once, then exit, on Windows.
REM
REM Usage:
REM   run_resolver.bat             ingest + resolve
REM   run_resolver.bat --dry-run   report what would change, write nothing
REM
REM To run it every 15 minutes, from Command Prompt:
REM   schtasks /Create /TN "Trading Alerts resolver" /SC MINUTE /MO 15 ^
REM            /TR "\"%CD%\run_resolver.bat\""
REM Task Scheduler's default "do not start a new instance" keeps runs from
REM overlapping.
REM
REM Uses the venv run.bat created; it never installs anything itself.

setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo No .venv found. Run run.bat once first to set it up.
    exit /b 1
)
call ".venv\Scripts\activate.bat"

if not exist ".env" (
    echo No .env file found. Copy .env.example to .env and fill it in first.
    exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "line=%%A"
    REM skip blank lines and comments
    if not "!line!"=="" if not "!line:~0,1!"=="#" (
        set "%%A=%%B"
    )
)

python resolve_alerts.py %*
exit /b %errorlevel%
