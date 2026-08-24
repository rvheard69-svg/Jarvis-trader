"""
Futures stop loss — pure logic, no broker calls, unit-testable in isolation
(same philosophy as contract_specs.py/risk_budget.py/orb.py).

Mirrors risk_guardrail.py's stopped_out_positions check on the equity
side, but denominated in index points against config.FUTURES_STOP_POINTS
instead of a percent of position value. This is the same stop distance
risk_budget.py already uses to SIZE a new position (dollars risked =
micros x multiplier x stop_points) — this module is what actually watches
price and flags when that stop has been hit, closing the loop between
"how much did we intend to risk" and "did we actually lose that much".

Long-only, matching the rest of the futures pipeline (futures_strategy.py
never opens a short): a position is stopped out when the current price has
fallen at least stop_points below its average entry price. A negative
quantity (something opened outside this app, e.g. manually in TWS) is
left alone rather than guessed at — this pipeline has no basis for knowing
what a short's stop should be.

Each symbol's position is evaluated against its OWN entry price, even
though risk and grouping (contract_specs.py) happen at the underlying
level — ES and MES bought at different times can have different entries,
and the stop is a price-action fact about that specific fill.
"""
from __future__ import annotations

from dataclasses import dataclass

import contract_specs as cs
from ib_broker import PositionDetail


@dataclass(frozen=True)
class StoppedOut:
    symbol: str
    group: str
    entry_price: float
    current_price: float
    points_against: float


def find_stopped_out(
    positions: dict[str, PositionDetail],
    current_prices: dict[str, float],  # group -> current index price
    stop_points: dict[str, float],
) -> list[StoppedOut]:
    out: list[StoppedOut] = []
    for symbol, detail in positions.items():
        if detail.qty <= 0 or symbol not in cs.SPECS:
            continue

        group = cs.group_of(symbol)
        current = current_prices.get(group)
        if current is None:
            continue
        stop = stop_points.get(group)
        if stop is None:
            continue

        points_against = detail.avg_entry_price - current
        if points_against >= stop:
            out.append(StoppedOut(
                symbol=symbol, group=group,
                entry_price=detail.avg_entry_price, current_price=current,
                points_against=points_against,
            ))
    return out
