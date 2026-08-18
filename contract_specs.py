"""
Contract specs, micro-equivalent sizing, and underlying grouping.

Two problems this solves, both from the port plan:

Sizing (Hazard 01). Percent-of-equity does not survive the move to futures.
Alpaca took a dollar notional and computed fractional shares; futures are
integer contracts with a fixed multiplier, so the smallest S&P position you
can hold is one MES — 16% of a $250k account. But one ES is 155% of the same
account. No single percentage constant can serve both.

The fix is that a mini is exactly ten micros. Express a target in
micro-equivalents and fill it with the fewest contracts, and each mini/micro
pair collapses into one number instead of two instruments competing for the
same signal.

Grouping (Hazard 04). ES and MES are the same underlying; so are NQ and MNQ.
Per-symbol limits count them as independent risk, which they are not — a
broad selloff is what drives everything oversold at once, and that is exactly
when the limits should bind hardest. Exposure is therefore aggregated by
underlying before any cap is applied.

No broker calls here on purpose: this is the layer that has to be right before
anything is wired to IB, so it stays unit-testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass

# Underlying groups. Risk limits apply at this level, never per symbol.
SP500 = "SP500"
NASDAQ100 = "NASDAQ100"


@dataclass(frozen=True)
class ContractSpec:
    """
    One tradeable futures contract.

    `micro_equivalents` is the unit that makes mini and micro commensurable:
    CME states micros are offset-eligible against their E-mini counterparts at
    10:1, so one ES is ten MES of the same exposure.

    `multiplier` and `tick_size` are declared here and verified against what
    IB reports for the qualified contract — see verify_against_ib(). Declaring
    them lets the sizing layer be tested without a broker; verifying them stops
    a stale constant from silently mis-sizing every order.
    """
    symbol: str
    exchange: str
    multiplier: int          # dollars of notional per 1.0 index point
    tick_size: float         # minimum price increment, in index points
    group: str
    micro_equivalents: int   # 1 for a micro, 10 for its E-mini

    @property
    def tick_value(self) -> float:
        """Dollars gained or lost per one-tick move, per contract."""
        return self.multiplier * self.tick_size

    def notional(self, index_level: float) -> float:
        return self.multiplier * index_level


# MES and MNQ multipliers and tick sizes are from CME contract specs, and the
# MES multiplier was confirmed against IB's own qualified contract during the
# phase 1 spike. ES and NQ are their E-minis at the documented 10:1 ratio.
# verify_against_ib() re-checks all four once market data is live.
SPECS: dict[str, ContractSpec] = {
    "MES": ContractSpec("MES", "CME", 5, 0.25, SP500, 1),
    "ES": ContractSpec("ES", "CME", 50, 0.25, SP500, 10),
    "MNQ": ContractSpec("MNQ", "CME", 2, 0.25, NASDAQ100, 1),
    "NQ": ContractSpec("NQ", "CME", 20, 0.25, NASDAQ100, 10),
}


def group_of(symbol: str) -> str:
    return SPECS[symbol].group


def symbols_in(group: str) -> list[str]:
    """Contracts in a group, largest first — the order fill() consumes them."""
    return sorted(
        (s for s, spec in SPECS.items() if spec.group == group),
        key=lambda s: SPECS[s].micro_equivalents,
        reverse=True,
    )


def fill(group: str, target_micros: int) -> dict[str, int]:
    """
    Express `target_micros` of exposure as the fewest contracts in `group`.

        fill(SP500, 12)  ->  {"ES": 1, "MES": 2}
        fill(SP500, 7)   ->  {"MES": 7}
        fill(SP500, 0)   ->  {}

    Greedy from the largest contract down. That is optimal here because each
    size divides the next exactly (10:1), so there is no case where taking a
    smaller contract first yields fewer total contracts.

    Contracts that would need zero are omitted rather than returned as 0, so
    the caller can iterate the result directly as orders to place.
    """
    if target_micros < 0:
        raise ValueError(
            f"target_micros must be >= 0, got {target_micros}. "
            f"This sizes new exposure; closing a position is a separate path."
        )

    remaining = target_micros
    out: dict[str, int] = {}
    for symbol in symbols_in(group):
        unit = SPECS[symbol].micro_equivalents
        count, remaining = divmod(remaining, unit)
        if count:
            out[symbol] = count
    assert remaining == 0, f"unfilled remainder {remaining} — a group lacks a 1-micro contract"
    return out


def micros_held(positions: dict[str, float]) -> dict[str, int]:
    """
    Current exposure per underlying group, in micro-equivalents.

        {"ES": 1, "MES": 2}  ->  {"SP500": 12}

    This is what risk limits read. A position held as ES and a position held as
    MES are the same bet, and counting them separately is the flaw that lets a
    correlated book pass every per-symbol check.

    Signed: shorts subtract, so an ES short against an MES long nets correctly.
    Symbols not in SPECS are ignored — an equity leg is not futures exposure.
    """
    out: dict[str, int] = {}
    for symbol, qty in positions.items():
        spec = SPECS.get(symbol)
        if spec is None or not qty:
            continue
        out[spec.group] = out.get(spec.group, 0) + int(qty) * spec.micro_equivalents
    return out


def group_notional(positions: dict[str, float], index_levels: dict[str, float]) -> dict[str, float]:
    """
    Dollar notional per group. `index_levels` is keyed by group, not symbol —
    ES and MES price off the same index, so one level covers both.
    """
    out: dict[str, float] = {}
    for symbol, qty in positions.items():
        spec = SPECS.get(symbol)
        if spec is None or not qty:
            continue
        level = index_levels.get(spec.group)
        if level is None:
            raise KeyError(f"no index level supplied for group {spec.group!r} (needed for {symbol})")
        out[spec.group] = out.get(spec.group, 0.0) + qty * spec.notional(level)
    return out


def micros_for_dollar_risk(symbol: str, dollars_at_risk: float, stop_points: float) -> int:
    """
    How many micro-equivalents put `dollars_at_risk` behind a stop `stop_points`
    index points away.

    This is the sizing basis that keeps position size and stop loss consistent:
    both are denominated in the same currency, so widening the stop shrinks the
    position automatically instead of silently increasing what a stop-out costs.

    Rounds down — never take more risk than asked for.
    """
    if stop_points <= 0:
        raise ValueError(f"stop_points must be > 0, got {stop_points}")
    micro = SPECS[symbol] if SPECS[symbol].micro_equivalents == 1 else _micro_of(SPECS[symbol].group)
    risk_per_micro = micro.multiplier * stop_points
    return int(dollars_at_risk // risk_per_micro)


def _micro_of(group: str) -> ContractSpec:
    for symbol in symbols_in(group):
        if SPECS[symbol].micro_equivalents == 1:
            return SPECS[symbol]
    raise KeyError(f"group {group!r} has no 1-micro contract")


def verify_against_ib(symbol: str, ib_multiplier: int, ib_tick_size: float | None = None) -> list[str]:
    """
    Compare a declared spec against what IB reports for the qualified contract.

    Returns a list of mismatches, empty when everything agrees. A wrong
    multiplier here would mis-size every order in that contract and mis-state
    every exposure figure, so this runs at startup rather than being trusted.
    """
    spec = SPECS[symbol]
    problems = []
    if ib_multiplier != spec.multiplier:
        problems.append(
            f"{symbol} multiplier: declared {spec.multiplier}, IB reports {ib_multiplier}"
        )
    if ib_tick_size is not None and abs(ib_tick_size - spec.tick_size) > 1e-9:
        problems.append(
            f"{symbol} tick size: declared {spec.tick_size}, IB reports {ib_tick_size}"
        )
    return problems
