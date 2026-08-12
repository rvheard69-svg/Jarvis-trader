#!/usr/bin/env bash
# Supervisor loop for macOS/Linux (and Windows via Git Bash/WSL).
#
# Run this INSTEAD of `python main.py` directly for unattended operation:
# it restarts main.py whenever it exits, for any reason (crash, an unhandled
# API error, the machine waking from sleep and the websocket dying, etc.).
# Backoff grows on repeated fast failures and resets after a healthy run,
# so a bad patch doesn't leave you waiting minutes once things recover.
#
# Usage:
#   chmod +x scripts/run_forever.sh   # first time only
#   ./scripts/run_forever.sh
#
# For true "starts on login/boot too" behavior, wrap this with systemd
# (Linux), launchd (macOS), or Task Scheduler (Windows) — see README.md.

set -uo pipefail
cd "$(dirname "$0")/.."

LOG_FILE="${LOG_FILE:-jarvis.log}"
BASE_DELAY=5
MAX_DELAY=300
HEALTHY_RUN_SECONDS=120
delay=$BASE_DELAY

echo "Starting jarvis-trader supervisor. Logs -> $LOG_FILE"

while true; do
    start_ts=$(date +%s)
    echo "$(date '+%F %T') starting main.py" | tee -a "$LOG_FILE"

    # -u keeps stdout unbuffered: without it Python block-buffers when writing
    # to a file, so status lines sit invisible in the buffer for ages and the
    # log is useless for seeing what the app is actually doing right now.
    python -u main.py >> "$LOG_FILE" 2>&1
    exit_code=$?

    end_ts=$(date +%s)
    ran_for=$((end_ts - start_ts))
    echo "$(date '+%F %T') main.py exited (code $exit_code) after ${ran_for}s" | tee -a "$LOG_FILE"

    if [ "$ran_for" -ge "$HEALTHY_RUN_SECONDS" ]; then
        delay=$BASE_DELAY
    else
        delay=$((delay * 2))
        if [ "$delay" -gt "$MAX_DELAY" ]; then
            delay=$MAX_DELAY
        fi
    fi

    echo "$(date '+%F %T') restarting in ${delay}s..." | tee -a "$LOG_FILE"
    sleep "$delay"
done
