"""
The futures Execution agent — same shape and same caution as executor.py,
one level up (a group, not a symbol; see futures_strategy.py):

  1. Only rsi_oversold / rsi_overbought signals reach `futures_strategy.decide()`.
  2. Every proposal is checked against risk_budget.evaluate() — the
     dollars-at-risk gate, not the equity system's percent-of-notional one —
     before anything else happens.
  3. If it clears that, you get a Telegram message with the exact order(s)
     and have to reply "yes" before anything is submitted, via the same
     TelegramConfirmer executor.py uses (telegram_confirm.py) — one
     implementation, so the two paths can't quietly disagree about what
     "confirmed" means.
  4. Orders go out through ib_broker.py, which refuses to even connect to a
     LIVE IB port.

force_close() is the exception to all of that: the one path that trades
without your explicit yes, for a position already past its stop
(futures_stop_loss.py). Same shape as executor.py's force_close — it takes
a symbol and quantity you already hold, so it can only ever close, never
open or increase exposure.

Wired into main.py behind config.FUTURES_ENABLED. See ib_broker.py's module
docstring for what has and hasn't been verified against real IB.

Dedup is per GROUP, not per symbol: a pending MES proposal has to block a
fresh ES signal too, because they're the same bet (Hazard 04) and confirming
one while another is in flight for the same underlying would double up
exposure the guardrail never got to see together.

A multi-leg proposal (e.g. 1 ES + 2 MES) submits each leg independently —
IB gives no cross-leg atomicity for two separate contracts, so a failure on
one leg does not roll back or skip the other; each leg's outcome is logged
on its own.
"""
from __future__ import annotations

import json
import time

import config
import contract_specs as cs
import futures_strategy
import risk_budget as rb
from ib_broker import IBBroker
from telegram_confirm import TelegramConfirmer


class FuturesExecutor:
    def __init__(self, broker: IBBroker):
        self.broker = broker
        self._confirmer = TelegramConfirmer()
        self._pending: set[str] = set()  # groups (SP500/NASDAQ100) currently awaiting confirmation

    # --- orchestration ---------------------------------------------------

    async def process_signal(self, symbol: str, kind: str, rsi: float) -> None:
        if kind not in ("rsi_oversold", "rsi_overbought", "orb_fade_buy", "orb_fade_sell"):
            return  # not a trade-rule signal

        group = cs.group_of(symbol)
        if group in self._pending:
            self._log(symbol, "dropped", detail=f"a proposal for {group} is already awaiting confirmation")
            return

        self._pending.add(group)
        try:
            await self._process_locked(symbol, kind, rsi)
        finally:
            self._pending.discard(group)

    async def _process_locked(self, symbol: str, kind: str, rsi: float) -> None:
        positions = await self.broker.get_positions()
        equity = await self.broker.get_equity()

        proposal = futures_strategy.decide(symbol, kind, positions, equity, rsi)
        if proposal is None:
            self._log(symbol, "no_proposal", detail=f"kind={kind}, group={cs.group_of(symbol)}")
            return

        limits, stop_points = rb.from_config()
        allowed, reason = rb.evaluate(symbol, proposal.add_micros, positions, stop_points, equity, limits)
        if not allowed:
            self._log(symbol, "blocked_by_guardrail", detail=reason, proposal=proposal)
            self._notify(f"BLOCKED — {proposal.side.upper()} {proposal.group}", reason)
            return

        confirmed, why = await self._confirmer.propose_and_confirm(
            self._format_proposal(proposal), log_prefix="[FuturesExecutor]"
        )
        if not confirmed:
            self._log(symbol, "not_confirmed", detail=why, proposal=proposal)
            self._notify(
                f"NOT SUBMITTED — {proposal.side.upper()} {proposal.group}",
                f"Nothing was submitted: {why}.",
            )
            return

        await self._submit(proposal)

    # --- Telegram message ------------------------------------------------

    def _format_proposal(self, proposal: futures_strategy.FuturesProposal) -> str:
        legs = ", ".join(f"{qty:+d} {sym}" for sym, qty in proposal.contracts.items())
        return (
            f"\U0001F514 *Futures trade proposal (paper account)*\n"
            f"{proposal.side.upper()} {proposal.group} — {legs}\n"
            f"Reason: {proposal.reason}\n\n"
            f"Reply *yes* within {config.CONFIRMATION_TIMEOUT_SECONDS // 60} min to submit "
            f"{'this paper order' if len(proposal.contracts) == 1 else 'these paper orders'}, "
            f"or *no* to cancel."
        )

    # --- order submission --------------------------------------------------

    async def _submit(self, proposal: futures_strategy.FuturesProposal) -> None:
        for leg_symbol, qty in proposal.contracts.items():
            if proposal.side == "open":
                action, quantity = "BUY", qty
            else:  # "close": flatten whatever sign is actually held
                action, quantity = ("SELL", qty) if qty > 0 else ("BUY", -qty)

            try:
                trade = await self.broker.submit_market_order(leg_symbol, action, quantity)
                order_id = getattr(getattr(trade, "order", None), "orderId", "unknown")
                self._log(leg_symbol, "submitted", detail=f"order_id={order_id}", proposal=proposal)
                self._notify(
                    f"SUBMITTED (paper) — {action} {quantity} {leg_symbol}",
                    f"Order id {order_id}. Reason: {proposal.reason}",
                )
            except Exception as exc:
                self._log(leg_symbol, "submit_failed", detail=str(exc), proposal=proposal)
                self._notify(f"SUBMIT FAILED — {action} {quantity} {leg_symbol}", str(exc))

    # --- stop loss (no confirmation) ----------------------------------------

    async def force_close(self, symbol: str, quantity: int, reason: str) -> bool:
        """
        Close `quantity` contracts of `symbol` immediately, no confirmation
        step — the one path in this executor that trades without your
        explicit yes. Always SELLs: futures_strategy.py never opens a
        short, so a position this stop loss ever finds is a long, and the
        only thing this can do is reduce or flatten it — never open a
        position or add exposure.

        Deferred (not closed) if a proposal for the same GROUP is already
        awaiting confirmation: submitting now could double-close if you
        reply "yes" a moment later. The caller (futures_risk_monitor_loop)
        retries on its next poll.
        """
        group = cs.group_of(symbol)
        if group in self._pending:
            self._log(symbol, "stop_loss_deferred", detail=f"{reason}; a proposal for {group} is awaiting confirmation")
            return False

        self._pending.add(group)
        try:
            trade = await self.broker.submit_market_order(symbol, "SELL", quantity)
            order_id = getattr(getattr(trade, "order", None), "orderId", "unknown")
            self._log(symbol, "stop_loss_closed", detail=f"{reason}; order_id={order_id}")
            self._notify(f"STOP LOSS — CLOSED {symbol}", f"{reason}. Position closed automatically, no confirmation required.")
            return True
        except Exception as exc:
            self._log(symbol, "stop_loss_failed", detail=f"{reason}; {exc}")
            self._notify(f"STOP LOSS FAILED — {symbol}", f"{reason}. Could not close: {type(exc).__name__}. Position is still open.")
            return False
        finally:
            self._pending.discard(group)

    # --- logging / notification --------------------------------------------

    def _notify(self, title: str, body: str) -> None:
        from notifier import send as notify
        notify({
            "ts": time.time(), "symbol": "FUTURES_EXECUTION", "kind": title,
            "price": 0.0, "detail": {}, "headlines": [], "explanation": body,
        })

    def _log(self, symbol: str, outcome: str, detail: str = "", proposal: futures_strategy.FuturesProposal | None = None) -> None:
        record = {
            "ts": time.time(),
            "symbol": symbol,
            "outcome": outcome,
            "detail": detail,
            "proposal": (proposal.__dict__ if proposal else None),
        }
        print(f"[FuturesExecutor] {symbol} -> {outcome}: {detail}")
        with open(config.FUTURES_EXECUTION_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
