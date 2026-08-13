"""
The Execution agent: the only piece of this project that ever submits an
order. It's deliberately dumb and cautious —

  1. Only rsi_oversold / rsi_overbought signals reach `strategy.decide()`
     (everything else stays explanation-only, handled by the Analyst).
  2. Every proposal it gets back is checked against the Risk Guardrail's
     `evaluate_order()` gate before anything else happens.
  3. If it clears that, you get a Telegram message with the exact order and
     have to reply "yes" before anything is actually submitted. No reply
     (or a "no") within the timeout means nothing happens.
  4. Only ever talks to Alpaca's PAPER endpoint — same guarantee as the
     rest of this project.

One proposal at a time per symbol: if you're already being asked to
confirm something on NVDA, a second NVDA signal is dropped (logged, not
silently lost) rather than queued behind it or overlapping it.
"""
import asyncio
import json
import time

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

import config
import strategy
from risk_guardrail import RiskGuardrail
from watcher import Signal


class Executor:
    def __init__(self, guardrail: RiskGuardrail):
        self.guardrail = guardrail
        self.trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)
        self._pending: set[str] = set()          # symbols currently awaiting confirmation
        self._telegram_offset: int | None = None  # None until we've done one getUpdates call

    # --- position lookup -------------------------------------------------

    def _get_position_qty(self, symbol: str) -> float:
        try:
            position = self.trading_client.get_open_position(symbol)
            return float(position.qty)
        except Exception:
            return 0.0  # Alpaca raises if there's no open position for this symbol — that's the common case

    # --- Telegram confirmation --------------------------------------------

    def _telegram_configured(self) -> bool:
        return config.telegram_configured()

    def _telegram_send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        # Raise rather than swallow: if the proposal never reached you, there's
        # no point polling five minutes for a reply that can't come.
        resp.raise_for_status()

    def _telegram_get_updates(self, offset: int | None) -> list:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])

    async def _propose_and_confirm(self, proposal: strategy.TradeProposal) -> tuple[bool, str]:
        """
        Returns (confirmed, reason). The reason is what lands in
        execution_log.jsonl — the record you read back later to understand why
        a trade didn't happen — so it has to say what actually occurred rather
        than assume a timeout.
        """
        if not self._telegram_configured():
            print("[Executor] Telegram isn't configured — nothing to confirm through, so no order will be proposed. "
                  "Trade confirmation requires NOTIFY_TELEGRAM=true and valid credentials in .env.")
            return False, "Telegram not configured — you were never asked"

        amount = f"${proposal.notional:,.2f}" if proposal.notional else "full position"
        text = (
            f"\U0001F514 *Trade proposal (paper account)*\n"
            f"{proposal.side.upper()} {proposal.symbol} — {amount}\n"
            f"Reason: {proposal.reason}\n\n"
            f"Reply *yes* within {config.CONFIRMATION_TIMEOUT_SECONDS // 60} min to submit this paper order, "
            f"or *no* to cancel it now."
        )

        # Establishing the offset and sending the prompt both hit the network.
        # A failure here means you were never actually asked, so treat it as
        # "not confirmed" and log it that way — never let it escape as an
        # exception, which would skip the execution-log record entirely.
        try:
            # Establish our starting offset the first time, so we only ever look
            # at messages sent AFTER this proposal goes out, not old chat history.
            if self._telegram_offset is None:
                existing = await asyncio.to_thread(self._telegram_get_updates, None)
                self._telegram_offset = (existing[-1]["update_id"] + 1) if existing else 0

            await asyncio.to_thread(self._telegram_send, text)
        except Exception as exc:
            # redact_secrets: requests puts the full URL in HTTPError, and the
            # Telegram bot token lives in that URL path.
            print(config.redact_secrets(
                f"[Executor] couldn't reach Telegram to request confirmation, "
                f"treating {proposal.symbol} as unconfirmed: {exc!r}"))
            return False, f"couldn't reach Telegram to ask: {type(exc).__name__}"

        deadline = time.monotonic() + config.CONFIRMATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(config.CONFIRMATION_POLL_SECONDS)
            try:
                updates = await asyncio.to_thread(self._telegram_get_updates, self._telegram_offset)
            except Exception as exc:
                print(config.redact_secrets(f"[Executor] Telegram poll failed: {exc!r}"))
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

    # --- order submission --------------------------------------------------

    def _submit_order(self, proposal: strategy.TradeProposal):
        if proposal.side == "buy":
            return self.trading_client.submit_order(MarketOrderRequest(
                symbol=proposal.symbol,
                notional=proposal.notional,
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            ))
        # "sell" in this rule always means "close the whole position"
        return self.trading_client.close_position(proposal.symbol)

    # --- orchestration -------------------------------------------------------

    async def process(self, signal: Signal) -> None:
        if signal.kind not in ("rsi_oversold", "rsi_overbought"):
            return  # not a trade-rule signal — the Analyst handles narration for everything else

        if signal.symbol in self._pending:
            self._log(signal.symbol, "dropped", detail="a proposal for this symbol is already awaiting confirmation")
            return

        self._pending.add(signal.symbol)
        try:
            await self._process_locked(signal)
        finally:
            self._pending.discard(signal.symbol)

    async def _process_locked(self, signal: Signal) -> None:
        existing_qty = await asyncio.to_thread(self._get_position_qty, signal.symbol)
        status = self.guardrail._last_status or await asyncio.to_thread(self.guardrail.refresh)

        proposal = strategy.decide(signal, existing_qty, status.equity)
        if proposal is None:
            self._log(signal.symbol, "no_proposal", detail=f"kind={signal.kind}, existing_qty={existing_qty}")
            return

        allowed, reason = self.guardrail.evaluate_order(proposal.symbol, proposal.notional or 0.0)
        if not allowed:
            self._log(signal.symbol, "blocked_by_guardrail", detail=reason, proposal=proposal)
            self._notify(f"BLOCKED — {proposal.side.upper()} {proposal.symbol}", reason)
            return

        confirmed, why = await self._propose_and_confirm(proposal)
        if not confirmed:
            self._log(signal.symbol, "not_confirmed", detail=why, proposal=proposal)
            self._notify(
                f"NOT SUBMITTED — {proposal.side.upper()} {proposal.symbol}",
                f"Nothing was submitted: {why}.",
            )
            return

        try:
            order = await asyncio.to_thread(self._submit_order, proposal)
            self._log(signal.symbol, "submitted", detail=f"order_id={getattr(order, 'id', 'unknown')}", proposal=proposal)
            self._notify(
                f"SUBMITTED (paper) — {proposal.side.upper()} {proposal.symbol}",
                f"Order id {getattr(order, 'id', 'unknown')}. Reason: {proposal.reason}",
            )
        except Exception as exc:
            self._log(signal.symbol, "submit_failed", detail=str(exc), proposal=proposal)
            self._notify(f"SUBMIT FAILED — {proposal.side.upper()} {proposal.symbol}", str(exc))

    # --- logging / notification --------------------------------------------

    def _notify(self, title: str, body: str) -> None:
        from notifier import send as notify
        notify({
            "ts": time.time(), "symbol": "EXECUTION", "kind": title,
            "price": 0.0, "detail": {}, "headlines": [], "explanation": body,
        })

    def _log(self, symbol: str, outcome: str, detail: str = "", proposal: strategy.TradeProposal | None = None) -> None:
        record = {
            "ts": time.time(),
            "symbol": symbol,
            "outcome": outcome,
            "detail": detail,
            "proposal": (proposal.__dict__ if proposal else None),
        }
        print(f"[Executor] {symbol} -> {outcome}: {detail}")
        with open(config.EXECUTION_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
