@echo off
REM Supervisor loop for native Windows (no Git Bash/WSL needed).
REM Run this INSTEAD of "python main.py" directly for unattended operation.
REM Simpler than the .sh version (no healthy-run reset), but does the same
REM core job: restart main.py whenever it exits, with growing backoff.
REM
REM Usage:  scripts\run_forever.bat
REM For "starts on login too", point a Task Scheduler entry at this file —
REM see README.md.

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

set LOGFILE=jarvis.log
set DELAY=5
set MAXDELAY=300

REM Prefer the project's virtualenv — the dependencies live there, not in
REM whatever "python" happens to be on PATH. Without this the supervisor
REM crash-loops on ModuleNotFoundError instead of running.
set PYTHON=python
if exist ".venv\Scripts\python.exe" set PYTHON=.venv\Scripts\python.exe

:loop
echo %date% %time% starting main.py >> %LOGFILE%
REM -u keeps stdout unbuffered, so status lines reach the log immediately
REM instead of sitting in Python's buffer while you're trying to read it.
"%PYTHON%" -u main.py >> %LOGFILE% 2>&1
echo %date% %time% main.py exited, restarting in %DELAY%s >> %LOGFILE%

REM Start-Sleep rather than `timeout /t`: timeout reads the console input
REM handle and aborts with "input redirection is not supported" in some
REM non-interactive contexts (notably under Task Scheduler, which README.md
REM recommends). It does work under a plain hidden window, so this is
REM hardening for the documented deployment path, not a fix for an observed
REM failure. Start-Sleep has no console dependency either way.
powershell -NoProfile -Command "Start-Sleep -Seconds %DELAY%"
set /a DELAY=DELAY*2
if %DELAY% GTR %MAXDELAY% set DELAY=%MAXDELAY%

goto loop
