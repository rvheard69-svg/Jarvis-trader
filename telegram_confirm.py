"""
Shared Telegram yes/no confirmation gate.

Extracted out of executor.py so a second execution path (futures) doesn't
duplicate this logic behind its back and drift from it — the two paths agree
on what "confirmed" means because they call the same code, not two copies of
it. Semantics are unchanged from the original: send a message, then poll
getUpdates for a reply from the configured chat id until it sees yes/no or
the timeout expires.

Each TelegramConfirmer instance tracks its own update offset, established
once (on its first proposal) and reused after that — re-establishing it on
every call would silently re-read old chat history as if it were a fresh
reply. One consequence worth knowing: two TelegramConfirmer instances polling
the same chat concurrently (e.g. a stock proposal and a futures proposal both
awaiting confirmation at once) would each see the same replies and could both
act on a single "yes". Out of scope today — nothing wires two executors to
run at the same time — but worth revisiting before that changes.
"""
import asyncio
import time

import requests

import config


class TelegramConfirmer:
    def __init__(self):
        self._telegram_offset: int | None = None  # None until we've done one getUpdates call

    def configured(self) -> bool:
        return config.telegram_configured()

    def _send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        # Raise rather than swallow: if the proposal never reached you, there's
        # no point polling for a reply that can't come.
        resp.raise_for_status()

    def _get_updates(self, offset: int | None) -> list:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])

    async def propose_and_confirm(self, text: str, log_prefix: str = "[TelegramConfirmer]") -> tuple[bool, str]:
        """
        Sends `text`, then waits for your yes/no. Returns (confirmed, reason);
        the reason is what callers log — it has to say what actually
        happened rather than assume a timeout.
        """
        if not self.configured():
            print(f"{log_prefix} Telegram isn't configured — nothing to confirm through, so no order will be "
                  f"proposed. Trade confirmation requires NOTIFY_TELEGRAM=true and valid credentials in .env.")
            return False, "Telegram not configured — you were never asked"

        # Establishing the offset and sending the prompt both hit the network.
        # A failure here means you were never actually asked, so treat it as
        # "not confirmed" and log it that way — never let it escape as an
        # exception, which would skip the caller's own logging entirely.
        try:
            if self._telegram_offset is None:
                existing = await asyncio.to_thread(self._get_updates, None)
                self._telegram_offset = (existing[-1]["update_id"] + 1) if existing else 0

            await asyncio.to_thread(self._send, text)
        except Exception as exc:
            # redact_secrets: requests puts the full URL in HTTPError, and the
            # Telegram bot token lives in that URL path.
            print(config.redact_secrets(
                f"{log_prefix} couldn't reach Telegram to request confirmation: {exc!r}"))
            return False, f"couldn't reach Telegram to ask: {type(exc).__name__}"

        deadline = time.monotonic() + config.CONFIRMATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(config.CONFIRMATION_POLL_SECONDS)
            try:
                updates = await asyncio.to_thread(self._get_updates, self._telegram_offset)
            except Exception as exc:
                print(config.redact_secrets(f"{log_prefix} Telegram poll failed: {exc!r}"))
                continue
            for update in updates:
                self._telegram_offset = update["update_id"] + 1
                message = update.get("message", {})
                if str(message.get("chat", {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
                    continue  # ignore anyone else who might message the bot
                reply = (message.get("text") or "").strip().lower()
                if reply in ("yes", "y", "confirm"):
                    return True, f"you replied '{reply}'"
                if reply in ("no", "n", "cancel"):
                    return False, f"you replied '{reply}'"
                # anything else (e.g. a stray "hi") is ignored, keep waiting
        return False, f"no reply within {config.CONFIRMATION_TIMEOUT_SECONDS}s"
