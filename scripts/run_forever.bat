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

:loop
echo %date% %time% starting main.py >> %LOGFILE%
REM -u keeps stdout unbuffered, so status lines reach the log immediately
REM instead of sitting in Python's buffer while you're trying to read it.
python -u main.py >> %LOGFILE% 2>&1
echo %date% %time% main.py exited, restarting in %DELAY%s >> %LOGFILE%

timeout /t %DELAY% /nobreak > NUL
set /a DELAY=DELAY*2
if %DELAY% GTR %MAXDELAY% set DELAY=%MAXDELAY%

goto loop
