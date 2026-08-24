#!/usr/bin/env bash
# Auto-login IB Gateway for macOS/Linux via IBC (https://github.com/IbcAlpha/IBC),
# so it comes up logged into your PAPER account without you clicking through
# the login screen (and its daily 24h auto-restart) by hand every session.
#
# One-time setup:
#   1. Download IBC from https://github.com/IbcAlpha/IBC/releases and unzip
#      it somewhere OUTSIDE this repo (e.g. ~/ibc) — the rendered config
#      below will hold your real IB password and must never sit in a git
#      working tree.
#   2. Install IB Gateway itself (see spike/ib_connect.py's CHECKLIST).
#   3. In this project's .env, set:
#        IB_USERNAME, IB_PASSWORD   — your PAPER account login
#        IBC_PATH                   — path to the IBC folder from step 1
#        IBC_GATEWAY_MAJOR_VERSION  — the Gateway version IBC should launch
#                                      (check Gateway's own splash screen /
#                                      Help > About; e.g. the folder name
#                                      under C:\Jts\ibgateway\<version>\ on
#                                      Windows is the same number on other
#                                      platforms' install layout)
#
# Usage:
#   chmod +x scripts/ibc/start_gateway.sh   # first time only
#   ./scripts/ibc/start_gateway.sh
#
# Then point this project at it as usual (.env: IB_HOST=127.0.0.1,
# IB_PORT=4002) and run python main.py / scripts/run_forever.sh.
#
# This only starts Gateway — it does not start main.py. Chain the two in
# your boot/login automation (see README.md) if you want both unattended.

set -euo pipefail
cd "$(dirname "$0")/../.."

if [ ! -f .env ]; then
    echo "No .env found — copy .env.example to .env and fill in IB_USERNAME/IB_PASSWORD/IBC_PATH first." >&2
    exit 1
fi

set -a
source .env
set +a

: "${IB_USERNAME:?Set IB_USERNAME in .env (your PAPER account login)}"
: "${IB_PASSWORD:?Set IB_PASSWORD in .env (your PAPER account password)}"
: "${IBC_PATH:?Set IBC_PATH in .env to where you unzipped IBC (outside this repo)}"
: "${IBC_GATEWAY_MAJOR_VERSION:?Set IBC_GATEWAY_MAJOR_VERSION in .env to your installed Gateway's version number}"
: "${IB_PORT:=4002}"

if ! command -v envsubst >/dev/null 2>&1; then
    echo "envsubst not found (part of gettext) — install it: apt/brew/etc. 'gettext'." >&2
    exit 1
fi

RENDERED_CONFIG="$IBC_PATH/config.ini"
echo "Rendering scripts/ibc/config.ini.template -> $RENDERED_CONFIG (real credentials, outside this repo)"
IB_USERNAME="$IB_USERNAME" IB_PASSWORD="$IB_PASSWORD" IB_PORT="$IB_PORT" \
    envsubst '${IB_USERNAME} ${IB_PASSWORD} ${IB_PORT}' \
    < scripts/ibc/config.ini.template > "$RENDERED_CONFIG"
chmod 600 "$RENDERED_CONFIG"

GATEWAY_SCRIPT="$IBC_PATH/gatewaystart.sh"
if [ ! -x "$GATEWAY_SCRIPT" ]; then
    echo "Expected IBC's launcher at $GATEWAY_SCRIPT (chmod +x it if it exists but isn't executable)." >&2
    exit 1
fi

echo "Starting IB Gateway $IBC_GATEWAY_MAJOR_VERSION via IBC (paper trading mode)..."
exec "$GATEWAY_SCRIPT" "$IBC_GATEWAY_MAJOR_VERSION" --gateway --tws-path="$IBC_PATH" --tws-settings-path="$IBC_PATH" --ibc-ini="$RENDERED_CONFIG"
