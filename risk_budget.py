"""
Risk limits denominated in dollars-at-risk.

The equity system capped notional: MAX_POSITION_PCT and MAX_PORTFOLIO_PCT both
measured market value against equity. That basis breaks on futures, and not
subtly — sizing 0.5% of a $250k account behind a 20-point S&P stop produces
1 ES + 2 MES, which risks $1,200 (0.48%, exactly as asked) while carrying
$465,840 of notional (186% of equity). A notional cap rejects that trade
despite its risk being tiny; loosen the cap enough to admit it and it no longer
constrains anything.

So the cap uses the same currency as the sizing: dollars at risk, meaning what
a position loses if it goes to its stop.

    risk = micro_equivalents x micro_multiplier x stop_points

That requires a stop distance per underlying, which is configuration rather
than per-position state — one rule per group, the same shape as the single
global stop the equity system used.

Three limits, checked in order of how much they should bind:

    per_trade   what a single new position may risk
    per_group   what all positions in one underlying may risk together
    total       what everything may risk at once

per_group exists because ES and MES are the same bet; total exists because
S&P and Nasdaq are not independent either — measured at 0.927 daily-return
correlation, they fall together 88% of the time.

Pure logic, no broker calls.
"""
from __future__ import annotations

from dataclasses import dataclass

import contract_specs as cs


@dataclass(frozen=True)
class RiskLimits:
    """
    Each limit is a percent of account equity.

    per_group_pct defaults above total_pct / number-of-groups on purpose. If
    the group caps summed to exactly the total, the group cap would always
    bind first and the total cap could never fire — see unreachable_caps(),
    which exists because that configuration looks perfectly reasonable and is
    silently inert.

    Allowing one underlying up to 1.5% while capping everything at 2% is also
    the right shape for the correlation: S&P and Nasdaq move together (0.927
    daily-return correlation), so concentrating in one is not meaningfully
    worse than splitting across both — but the combined total still needs a
    ceiling.
    """
    per_trade_pct: float = 0.5
    per_group_pct: float = 1.5
    total_pct: float = 2.0

    def unreachable_caps(self, n_groups: int = 2) -> list[str]:
        """
        Limits that can never fire given the others. Empty when the
        configuration is coherent.

        A cap that cannot bind is worse than no cap: it reads as protection
        during review and provides none at runtime.
        """
        problems = []
        if self.per_trade_pct > self.per_group_pct:
            problems.append(
                f"per_trade ({self.per_trade_pct}%) exceeds per_group "
                f"({self.per_group_pct}%) — the group cap fires first, so "
                f"per_trade can never bind"
            )
        if self.per_group_pct * n_groups <= self.total_pct:
            problems.append(
                f"per_group ({self.per_group_pct}%) x {n_groups} groups "
                f"<= total ({self.total_pct}%) — group caps always bind first, "
                f"so the total cap can never fire"
            )
        return problems

    def per_trade(self, equity: float) -> float:
        return equity * self.per_trade_pct / 100

    def per_group(self, equity: float) -> float:
        return equity * self.per_group_pct / 100

    def total(self, equity: float) -> float:
        return equity * self.total_pct / 100


def from_config() -> tuple[RiskLimits, dict[str, float]]:
    """
    The configured limits and stop distances, as (limits, stop_points).

    Raises if the configured limits are incoherent — a cap that can never fire
    reads as protection during review and provides none at runtime, so it
    should stop the app rather than be discovered after a loss.
    """
    import config

    limits = RiskLimits(
        per_trade_pct=config.RISK_PER_TRADE_PCT,
        per_group_pct=config.RISK_PER_GROUP_PCT,
        total_pct=config.RISK_TOTAL_PCT,
    )
    stop_points = dict(config.FUTURES_STOP_POINTS)

    # Completeness first. The coherence check below depends on how many
    # underlyings there are, so an incomplete config checked in the other
    # order reports a misleading "incoherent limits" error for what is
    # really a missing stop distance.
    groups = {spec.group for spec in cs.SPECS.values()}
    missing = groups - set(stop_points)
    if missing:
        raise RuntimeError(
            f"no stop distance configured for {sorted(missing)} — sizing and the "
            f"stop loss both need one per underlying"
        )

    # Group count comes from the contracts, not from the config: it is a fact
    # about what is tradeable, and reading it from stop_points would let a
    # truncated config silently relax the coherence rule.
    problems = limits.unreachable_caps(n_groups=len(groups))
    if problems:
        raise RuntimeError(
            "risk limits are incoherent — one cap is masked by another and can "
            "never fire:\n  " + "\n  ".join(problems)
        )
    return limits, stop_points


def risk_of(micros: int, group: str, stop_points: dict[str, float]) -> float:
    """
    Dollars lost if `micros` of exposure in `group` goes to its stop.

    Uses absolute value: a short risks the same dollars as a long of equal
    size, just in the other direction.
    """
    if group not in stop_points:
        raise KeyError(f"no stop distance configured for group {group!r}")
    micro = cs._micro_of(group)
    return abs(micros) * micro.multiplier * stop_points[group]


def risk_by_group(positions: dict[str, float], stop_points: dict[str, float]) -> dict[str, float]:
    """Dollars at risk per underlying, from current positions."""
    return {
        group: risk_of(micros, group, stop_points)
        for group, micros in cs.micros_held(positions).items()
        if micros
    }


def total_risk(positions: dict[str, float], stop_points: dict[str, float]) -> float:
    return sum(risk_by_group(positions, stop_points).values())


def evaluate(
    symbol: str,
    add_micros: int,
    positions: dict[str, float],
    stop_points: dict[str, float],
    equity: float,
    limits: RiskLimits,
) -> tuple[bool, str]:
    """
    May we add `add_micros` of exposure in `symbol`? Returns (allowed, reason).

    Only orders that ADD risk are checked. An order that reduces or closes
    exposure is always allowed — refusing to let a book de-risk because it is
    already too risky is exactly backwards, and would trap you in an oversized
    position precisely when getting out matters most. Same principle the
    equity system's portfolio cap already applies to sells.

    Reasons name the projected figure, not just the current one, so a refusal
    explains itself.
    """
    if add_micros <= 0:
        return True, "allowed: reduces or closes exposure"

    group = cs.group_of(symbol)
    added = risk_of(add_micros, group, stop_points)

    # 1. what this one position risks
    cap = limits.per_trade(equity)
    if added > cap:
        return False, (
            f"blocked: {add_micros} micro-equivalents of {symbol} risks ${added:,.0f}, "
            f"over the ${cap:,.0f} per-trade cap ({limits.per_trade_pct}% of equity)"
        )

    # 2. what this underlying risks in total — ES and MES are one bet
    current = risk_by_group(positions, stop_points)
    projected_group = current.get(group, 0.0) + added
    cap = limits.per_group(equity)
    if projected_group > cap:
        return False, (
            f"blocked: would take {group} risk to ${projected_group:,.0f}, over the "
            f"${cap:,.0f} per-underlying cap ({limits.per_group_pct}% of equity); "
            f"currently ${current.get(group, 0.0):,.0f}"
        )

    # 3. what everything risks at once — the groups are correlated too
    projected_total = sum(current.values()) + added
    cap = limits.total(equity)
    if projected_total > cap:
        return False, (
            f"blocked: would take total risk to ${projected_total:,.0f}, over the "
            f"${cap:,.0f} portfolio cap ({limits.total_pct}% of equity); "
            f"currently ${sum(current.values()):,.0f}"
        )

    return True, (
        f"allowed: risks ${added:,.0f}, taking {group} to ${projected_group:,.0f} "
        f"and total to ${projected_total:,.0f}"
    )


def headroom(
    group: str,
    positions: dict[str, float],
    stop_points: dict[str, float],
    equity: float,
    limits: RiskLimits,
) -> int:
    """
    Largest number of micro-equivalents that could still be added to `group`
    without breaching any limit. Zero when a cap already binds.

    Lets the sizing layer ask for what fits rather than proposing something
    the Guardrail will only reject.
    """
    current = risk_by_group(positions, stop_points)
    micro = cs._micro_of(group)
    risk_per_micro = micro.multiplier * stop_points[group]
    if risk_per_micro <= 0:
        return 0

    budgets = (
        limits.per_trade(equity),
        limits.per_group(equity) - current.get(group, 0.0),
        limits.total(equity) - sum(current.values()),
    )
    return max(0, int(min(budgets) // risk_per_micro))
