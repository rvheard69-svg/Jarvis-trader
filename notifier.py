"""
The Notifier: gets the Analyst's explanation in front of you, wherever you are.

Two channels, each toggled independently via .env:
  - Desktop notification (native OS popup) via `plyer` — for when you're at the machine
  - Telegram message via the Bot API — for when you're not

Both are best-effort. A failure in one channel is logged and never blocks the
other or crashes the pipeline — a missed notification is annoying, a crashed
Watcher is worse.
"""
from datetime import datetime

import requests

import config

try:
    from plyer import notification as _desktop_notification
    _DESKTOP_LIB_AVAILABLE = True
except ImportError:
    _DESKTOP_LIB_AVAILABLE = False


def _format(result: dict) -> tuple[str, str]:
    when = datetime.fromtimestamp(result["ts"]).strftime("%H:%M:%S")
    title = f"{result['symbol']} — {result['kind']} @ ${result['price']:.2f}"
    body = f"[{when}] {result['explanation']}"
    if result["headlines"]:
        body += "\n\nHeadlines:\n" + "\n".join(f"- {h}" for h in result["headlines"])
    return title, body


def _send_desktop(title: str, body: str) -> None:
    if not config.NOTIFY_DESKTOP:
        return
    if not _DESKTOP_LIB_AVAILABLE:
        print("[Notifier] plyer isn't installed — skipping desktop popup (pip install plyer)")
        return
    try:
        # Most OS notification centers truncate long text anyway; keep it readable.
        _desktop_notification.notify(title=title, message=body[:250], timeout=15)
    except Exception as exc:
        # Common on a headless/remote Linux box with no notification daemon running —
        # not fatal, Telegram (if enabled) still gets it.
        print(f"[Notifier] desktop notification failed: {exc!r}")


def _send_telegram(title: str, body: str) -> None:
    if not config.NOTIFY_TELEGRAM:
        return
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print("[Notifier] Telegram is enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are missing — skipping")
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": f"*{title}*\n{body}", "parse_mode": "Markdown"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Notifier] Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as exc:
        print(f"[Notifier] Telegram request failed: {exc!r}")


def send(result: dict) -> None:
    title, body = _format(result)
    print(f"\n{title}\n{'-' * len(title)}\n{body}\n")  # console always gets it — cheap, and useful in logs
    _send_desktop(title, body)
    _send_telegram(title, body)
