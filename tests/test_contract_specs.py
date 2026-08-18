"""Sizing and grouping have to be right before anything touches IB — a wrong
multiplier mis-sizes every order, and per-symbol grouping is the flaw that lets
a fully correlated book pass every risk check."""
import pytest

import contract_specs as cs
from contract_specs import NASDAQ100, SP500, SPECS


# --- specs -----------------------------------------------------------------

def test_multipliers_match_cme_specs():
    # MES/MNQ verified against CME; ES/NQ are their E-minis at 10:1.
    assert SPECS["MES"].multiplier == 5
    assert SPECS["ES"].multiplier == 50
    assert SPECS["MNQ"].multiplier == 2
    assert SPECS["NQ"].multiplier == 20


def test_minis_are_exactly_ten_micros():
    """The whole micro-equivalent scheme rests on this ratio holding."""
    for mini, micro in (("ES", "MES"), ("NQ", "MNQ")):
        assert SPECS[mini].multiplier == SPECS[micro].multiplier * 10
        assert SPECS[mini].micro_equivalents == 10
        assert SPECS[micro].micro_equivalents == 1


def test_tick_values():
    assert SPECS["MES"].tick_value == 1.25
    assert SPECS["ES"].tick_value == 12.50
    assert SPECS["MNQ"].tick_value == 0.50
    assert SPECS["NQ"].tick_value == 5.00


def test_notional_at_measured_index_levels():
    # Levels measured during the spike session.
    assert SPECS["MES"].notional(7764) == 38820
    assert SPECS["ES"].notional(7764) == 388200
    assert SPECS["MNQ"].notional(29742) == 59484
    assert SPECS["NQ"].notional(29742) == 594840


def test_groups():
    assert cs.group_of("ES") == cs.group_of("MES") == SP500
    assert cs.group_of("NQ") == cs.group_of("MNQ") == NASDAQ100


# --- fill ------------------------------------------------------------------

def test_fill_uses_the_fewest_contracts():
    assert cs.fill(SP500, 12) == {"ES": 1, "MES": 2}
    assert cs.fill(NASDAQ100, 23) == {"NQ": 2, "MNQ": 3}


def test_fill_below_mini_granularity_uses_micros_only():
    assert cs.fill(SP500, 7) == {"MES": 7}
    assert cs.fill(SP500, 9) == {"MES": 9}


def test_fill_exact_mini_multiples_omit_micros():
    # Zero-count contracts are omitted so the caller can iterate the result
    # directly as orders to place.
    assert cs.fill(SP500, 10) == {"ES": 1}
    assert cs.fill(SP500, 30) == {"ES": 3}


def test_fill_zero_is_empty():
    assert cs.fill(SP500, 0) == {}


def test_fill_rejects_negative():
    with pytest.raises(ValueError, match="must be >= 0"):
        cs.fill(SP500, -5)


@pytest.mark.parametrize("target", range(0, 47))
def test_fill_always_totals_the_target(target):
    """Property: whatever the fill, the micro-equivalents must add back up."""
    filled = cs.fill(SP500, target)
    total = sum(SPECS[s].micro_equivalents * n for s, n in filled.items())
    assert total == target


# --- grouping --------------------------------------------------------------

def test_mini_and_micro_aggregate_into_one_underlying():
    """The core of Hazard 04: these are one bet, not two."""
    assert cs.micros_held({"ES": 1, "MES": 2}) == {SP500: 12}


def test_both_groups_tracked_separately():
    held = cs.micros_held({"ES": 1, "MNQ": 4})
    assert held == {SP500: 10, NASDAQ100: 4}


def test_shorts_net_against_longs():
    assert cs.micros_held({"ES": 1, "MES": -3}) == {SP500: 7}


def test_non_futures_symbols_are_ignored():
    # An equity leg is not futures exposure.
    assert cs.micros_held({"AAPL": 100, "MES": 2}) == {SP500: 2}


def test_flat_positions_are_omitted():
    assert cs.micros_held({"MES": 0}) == {}


def test_group_notional_shares_one_index_level_across_the_pair():
    notional = cs.group_notional({"ES": 1, "MES": 2}, {SP500: 7764})
    assert notional == {SP500: 388200 + 2 * 38820}


def test_group_notional_demands_a_level_for_every_held_group():
    with pytest.raises(KeyError, match="NASDAQ100"):
        cs.group_notional({"MNQ": 1}, {SP500: 7764})


# --- dollar-risk sizing ----------------------------------------------------

def test_dollar_risk_sizing_uses_the_micro_as_the_unit():
    # $500 at risk, 20-point stop on the S&P. One MES risks 5 * 20 = $100.
    assert cs.micros_for_dollar_risk("MES", 500, 20) == 5
    # Asking via the mini must size in the same micro unit, not ES units.
    assert cs.micros_for_dollar_risk("ES", 500, 20) == 5


def test_dollar_risk_sizing_rounds_down():
    # $550 / $100 per micro = 5.5 -> 5. Never exceed the stated risk.
    assert cs.micros_for_dollar_risk("MES", 550, 20) == 5


def test_wider_stop_gives_a_smaller_position():
    """Position size and stop stay consistent — this is why both are denominated
    in dollars rather than percent."""
    tight = cs.micros_for_dollar_risk("MES", 1000, 10)
    wide = cs.micros_for_dollar_risk("MES", 1000, 40)
    assert tight == 20 and wide == 5


def test_nasdaq_risk_uses_the_mnq_multiplier():
    # One MNQ risks 2 * 25 = $50.
    assert cs.micros_for_dollar_risk("MNQ", 500, 25) == 10


def test_zero_stop_is_rejected():
    with pytest.raises(ValueError, match="stop_points must be > 0"):
        cs.micros_for_dollar_risk("MES", 500, 0)


# --- verification against IB ------------------------------------------------

def test_matching_spec_reports_no_problems():
    # What IB actually returned for MESU6 during the spike.
    assert cs.verify_against_ib("MES", ib_multiplier=5, ib_tick_size=0.25) == []


def test_multiplier_mismatch_is_reported():
    problems = cs.verify_against_ib("MES", ib_multiplier=50)
    assert len(problems) == 1
    assert "declared 5, IB reports 50" in problems[0]


def test_tick_mismatch_is_reported():
    problems = cs.verify_against_ib("ES", ib_multiplier=50, ib_tick_size=0.5)
    assert any("tick size" in p for p in problems)
