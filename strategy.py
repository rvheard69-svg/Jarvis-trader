"""
The trade rule — the ONLY thing in this entire project that decides to
place a trade. Deliberately simple and fully deterministic. This is not a
claim that RSI mean-reversion is a good strategy; it's a concrete,
inspectable rule to prove out the full pipeline (signal -> proposal -> risk
gate -> your confirmation -> order) before you ever swap in something more
sophisticated. Replace this file, not the plumbing around it, when you want
a different rule.

Rule:
  - RSI oversold   -> propose BUYing a new position, ONLY if you don't
                      already hold one in that symbol (no averaging down).
  - RSI overbought -> propose SELLING (closing) the position, ONLY if you
                      already hold one. No shorting.
  - orb_fade_buy / orb_fade_sell (see orb.py + watcher.py) follow the exact
    same two rules as rsi_oversold/rsi_overbought — an ORB fade is already
    RSI-confirmed by the time the Watcher emits it (see watcher.py), so
    there is nothing left for this file to check beyond the existing
    position rule. Only the reason string differs, to say which trigger fired.
  - Every other signal kind (volume spike, VWAP cross) never proposes a
    trade — those stay explanation-only, exactly as before this file existed.
"""
from dataclasses import dataclass

import config
from watcher import Signal


@dataclass
class TradeProposal:
    symbol: str
    side: str            # "buy" or "sell"
    notional: float | None   # dollar amount for a buy; None for a sell (closes the whole position)
    reason: str


def decide(signal: Signal, existing_qty: float, equity: float) -> TradeProposal | None:
    if signal.kind in ("rsi_oversold", "orb_fade_buy"):
        if existing_qty != 0:
            return None  # already holding this symbol — rule doesn't average down
        notional = round(equity * config.TRADE_SIZE_PCT / 100, 2)
        rsi = signal.detail.get("rsi")
        reason = (
            f"RSI oversold ({rsi}) with no existing position"
            if signal.kind == "rsi_oversold"
            else f"ORB fade: reclaimed the opening range low, RSI oversold ({rsi}), no existing position"
        )
        return TradeProposal(symbol=signal.symbol, side="buy", notional=notional, reason=reason)

    if signal.kind in ("rsi_overbought", "orb_fade_sell"):
        if existing_qty == 0:
            return None  # nothing to sell
        rsi = signal.detail.get("rsi")
        reason = (
            f"RSI overbought ({rsi}) — closing existing {existing_qty}-share position"
            if signal.kind == "rsi_overbought"
            else f"ORB fade: rejected the opening range high, RSI overbought ({rsi}) — "
                 f"closing existing {existing_qty}-share position"
        )
        return TradeProposal(symbol=signal.symbol, side="sell", notional=None, reason=reason)

    return None
