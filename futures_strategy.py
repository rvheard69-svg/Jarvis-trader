"""
The futures trade rule — same restraint as strategy.py, one level up.

strategy.py decides per symbol because on Alpaca a symbol IS the position.
On IB futures that's not true: ES and MES are the same underlying bet
(contract_specs.py / risk_budget.py, Hazard 04), so every check here happens
at the GROUP level (SP500/NASDAQ100) instead — "already holding this
symbol" becomes "already holding this underlying, in any contract size".

Rule, deliberately as narrow as the equity one:
  - RSI oversold   -> propose opening a NEW long position, ONLY if the whole
                      underlying group is currently flat (no averaging down,
                      no shorting). Sized as a percent of equity's worth of
                      DOLLAR RISK (config.RISK_PER_TRADE_PCT), not percent of
                      notional — risk_budget.py explains why notional can't
                      be the basis once sizing has to work for both ES and
                      MES from the same account.
  - RSI overbought -> propose closing EVERY contract currently held in that
                      group, not just whichever symbol's RSI triggered — a
                      position split across ES and MES has to close both.
  - Every other signal kind proposes nothing, same as strategy.py.

This is the proposal layer only. Nothing here checks risk_budget's caps —
that is futures_executor.py's job, via risk_budget.evaluate(), exactly like
strategy.py's proposals are separately gated by risk_guardrail.evaluate_order().
Keeping sizing and gating apart means a change to the caps never has to touch
this file.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
import contract_specs as cs
import risk_budget as rb


@dataclass
class FuturesProposal:
    group: str
    side: str                  # "open" or "close"
    contracts: dict[str, int]  # symbol -> signed quantity (open: always positive/BUY; close: the exact signed position being flattened)
    add_micros: int            # micro-equivalents this proposal adds; 0 for a close
    reason: str


def decide(symbol: str, kind: str, positions: dict[str, float], equity: float, rsi: float) -> FuturesProposal | None:
    if symbol not in cs.SPECS:
        raise ValueError(f"unknown futures symbol {symbol!r}. Known symbols: {sorted(cs.SPECS)}")

    group = cs.group_of(symbol)

    if kind == "rsi_oversold":
        if cs.micros_held(positions).get(group, 0) != 0:
            return None  # already holding this underlying — rule doesn't average down

        _, stop_points = rb.from_config()  # raises if the shipped risk config is incoherent
        target_risk = equity * config.RISK_PER_TRADE_PCT / 100
        micros = cs.micros_for_dollar_risk(symbol, target_risk, stop_points[group])
        if micros <= 0:
            return None  # equity too small (or stop too wide) to size even one micro

        return FuturesProposal(
            group=group,
            side="open",
            contracts=cs.fill(group, micros),
            add_micros=micros,
            reason=f"RSI oversold ({rsi}) on {symbol} with {group} flat — risking ${target_risk:,.0f}",
        )

    if kind == "rsi_overbought":
        held = {
            held_symbol: int(qty)
            for held_symbol, qty in positions.items()
            if held_symbol in cs.SPECS and cs.group_of(held_symbol) == group and qty
        }
        if not held:
            return None  # nothing in this group to close

        return FuturesProposal(
            group=group,
            side="close",
            contracts=held,
            add_micros=0,
            reason=f"RSI overbought ({rsi}) on {symbol} — closing entire {group} position ({held})",
        )

    return None
