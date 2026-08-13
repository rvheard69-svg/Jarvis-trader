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
    title = f"{result['symbol']} — {result['kind']}"
    # Execution and account alerts carry no meaningful price; rendering them
    # as "@ $0.00" makes a working alert look like a bug.
    if result.get("price"):
        title += f" @ ${result['price']:.2f}"
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
    if not config.telegram_configured():
        # Placeholder credentials from .env.example land here. Skip quietly —
        # main.py already warns once at startup; warning on every single alert
        # would bury the alerts themselves.
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": f"*{title}*\n{body}", "parse_mode": "Markdown"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(config.redact_secrets(f"[Notifier] Telegram send failed: {resp.status_code} {resp.text}"))
    except Exception as exc:
        # The bot token is in the request URL, which requests embeds in its
        # exception text — redact before it reaches the log.
        print(config.redact_secrets(f"[Notifier] Telegram request failed: {exc!r}"))


def _safe_print(text: str) -> None:
    """
    print() that can't raise on an encoding-limited stdout. Redirected stdout
    on Windows encodes with cp1252, which has no arrows or math symbols —
    exactly what Claude reaches for in technical explanations. Losing the
    characters is fine; losing the alert is not.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def _send_console(title: str, body: str) -> None:
    _safe_print(f"\n{title}\n{'-' * len(title)}\n{body}\n")


def send(result: dict) -> None:
    """
    Fan out to every enabled channel. Each is independent and best-effort: a
    failure in one must never prevent the others from firing, which is why
    each call is isolated rather than run in sequence in one try block.
    """
    title, body = _format(result)
    for name, channel in (
        ("console", _send_console),
        ("desktop", _send_desktop),
        ("telegram", _send_telegram),
    ):
        try:
            channel(title, body)
        except Exception as exc:
            # _safe_print, not print: a UnicodeEncodeError's repr contains the
            # very character that couldn't be encoded, so a plain print here
            # would raise the same error again and escape send() entirely.
            # Deliberately not channel.__name__ either — the handler for a
            # failing channel must not itself be able to fail.
            _safe_print(f"[Notifier] {name} channel failed: {exc!r}")
