@echo off
REM Windows equivalent of update_all.sh — pulls code updates (if this is a
REM git repo with a remote) and upgrades Python dependencies.
REM
REM Usage:  scripts\update_all.bat

cd /d "%~dp0\.."

echo == Code updates ==
if exist .git (
    git fetch
    git pull --ff-only
) else (
    echo Not a git repo - skipping code update. See README.md to turn this into one.
)

echo.
echo == Dependency updates ==
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)
pip install --upgrade -r requirements.txt

echo.
echo == Done ==
echo Restart run_forever.bat if it's currently running, to pick up changes.
