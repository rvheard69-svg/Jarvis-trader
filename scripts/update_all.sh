#!/usr/bin/env bash
# Keeps the project current: pulls any new code (if this folder is a git
# repo with a remote configured — see README.md) and upgrades the Python
# dependencies to their latest compatible versions.
#
# Safe to run anytime. Put it on a weekly cron job (or Windows Task
# Scheduler pointed at update_all.bat) so this happens without you
# remembering to do it.
#
# Usage:
#   chmod +x scripts/update_all.sh   # first time only
#   ./scripts/update_all.sh

set -uo pipefail
cd "$(dirname "$0")/.."

echo "== Code updates =="
if [ -d .git ]; then
    git fetch --quiet
    LOCAL=$(git rev-parse @ 2>/dev/null)
    REMOTE=$(git rev-parse '@{u}' 2>/dev/null)
    if [ -z "$REMOTE" ]; then
        echo "No remote tracking branch configured — skipping. See README.md to set one up."
    elif [ "$LOCAL" != "$REMOTE" ]; then
        echo "Updates found, pulling..."
        git pull --ff-only
    else
        echo "Already up to date."
    fi
else
    echo "Not a git repo — skipping code update. See README.md to turn this into one."
fi

echo ""
echo "== Dependency updates =="
if [ -d .venv ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi
pip install --upgrade -r requirements.txt

echo ""
echo "== Done =="
echo "If main.py is currently running via run_forever.sh, restart it now to"
echo "pick up any code changes (dependency upgrades apply on next launch too)."
