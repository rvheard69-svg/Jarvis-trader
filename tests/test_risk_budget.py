"""Risk caps in dollars-at-risk. The properties that matter most are the ones
about what must NOT be blocked, and about mini/micro counting as one bet."""
import pytest

import risk_budget as rb
from contract_specs import NASDAQ100, SP500
from risk_budget import RiskLimits

EQUITY = 250_258.24                      # the actual paper account
STOPS = {SP500: 20.0, NASDAQ100: 50.0}   # index points per underlying
LIMITS = RiskLimits()                    # 0.5 / 1.5 / 2.0 — the coherent defaults

# At this equity:  per_trade $1,251   per_group $3,754   total $5,005

# 1 MES micro risks 5 * 20 = $100.  1 MNQ micro risks 2 * 50 = $100.
MES_MICRO_RISK = 100.0
MNQ_MICRO_RISK = 100.0


# --- risk arithmetic --------------------------------------------------------

def test_risk_of_a_micro():
    assert rb.risk_of(1, SP500, STOPS) == MES_MICRO_RISK
    assert rb.risk_of(1, NASDAQ100, STOPS) == MNQ_MICRO_RISK


def test_risk_scales_with_size():
    assert rb.risk_of(12, SP500, STOPS) == 1200.0


def test_shorts_risk_the_same_as_longs():
    assert rb.risk_of(-5, SP500, STOPS) == rb.risk_of(5, SP500, STOPS)


def test_mini_and_micro_risk_is_counted_once_per_underlying():
    """1 ES + 2 MES is 12 micro-equivalents of one bet, not two positions."""
    assert rb.risk_by_group({"ES": 1, "MES": 2}, STOPS) == {SP500: 1200.0}


def test_missing_stop_distance_is_an_error_not_a_zero():
    with pytest.raises(KeyError, match="NASDAQ100"):
        rb.risk_of(1, NASDAQ100, {SP500: 20.0})


# --- per-trade cap ----------------------------------------------------------

def test_trade_within_per_trade_cap_is_allowed():
    # cap = 0.5% of 250,258 = $1,251. 12 micros risks $1,200.
    ok, why = rb.evaluate("MES", 12, {}, STOPS, EQUITY, LIMITS)
    assert ok, why


def test_trade_over_per_trade_cap_is_blocked():
    ok, why = rb.evaluate("MES", 13, {}, STOPS, EQUITY, LIMITS)
    assert not ok
    assert "per-trade cap" in why
    assert "$1,300" in why      # names the projected figure


# --- per-group cap: the Hazard 04 case --------------------------------------

def test_second_position_in_the_same_underlying_counts_against_the_first():
    """The flaw this exists to fix: MES and ES are not independent risk.
    30 micros held ($3,000) plus 12 more ($1,200) exceeds the $3,754 group cap,
    even though the 12 on its own is inside the per-trade cap."""
    held = {"ES": 3}                       # 30 micros = $3,000 at risk
    ok, why = rb.evaluate("MES", 12, held, STOPS, EQUITY, LIMITS)
    assert not ok
    assert "per-underlying cap" in why
    assert "SP500" in why


def test_the_same_add_is_fine_when_the_underlying_is_empty():
    """Confirms the block above came from the group total, not the trade size."""
    ok, why = rb.evaluate("MES", 12, {}, STOPS, EQUITY, LIMITS)
    assert ok, why


def test_a_different_underlying_is_not_blocked_by_the_first():
    held = {"ES": 1}                       # S&P risk, not Nasdaq
    ok, why = rb.evaluate("MNQ", 10, held, STOPS, EQUITY, LIMITS)
    assert ok, why


# --- total cap: correlated groups -------------------------------------------

def test_total_cap_binds_when_both_groups_are_loaded():
    """Neither group is individually over its 1.5% cap, and the trade is inside
    the per-trade cap — but together they exceed the 2% portfolio budget. This
    is the case that exists because the two underlyings are correlated."""
    held = {"ES": 2, "NQ": 2}              # $2,000 S&P + $2,000 Nasdaq
    ok, why = rb.evaluate("MNQ", 11, held, STOPS, EQUITY, LIMITS)
    assert not ok
    assert "portfolio cap" in why
    assert "$5,100" in why                 # projected, not just current


def test_one_less_micro_fits_under_the_total():
    ok, why = rb.evaluate("MNQ", 10, {"ES": 2, "NQ": 2}, STOPS, EQUITY, LIMITS)
    assert ok, why


# --- limits that cannot fire ------------------------------------------------

def test_default_limits_are_coherent():
    assert RiskLimits().unreachable_caps() == []


def test_group_caps_summing_to_the_total_makes_the_total_inert():
    """The obvious-looking config: 1% per group, 2% total, two groups. The
    group cap always fires first, so the total never can."""
    problems = RiskLimits(per_trade_pct=0.5, per_group_pct=1.0, total_pct=2.0).unreachable_caps()
    assert len(problems) == 1
    assert "total cap can never fire" in problems[0]


def test_shipped_config_is_coherent_and_complete():
    """The defaults in config.py must survive their own validation — an
    incoherent shipped default would fail only at runtime, on a real account."""
    limits, stops = rb.from_config()
    assert limits.unreachable_caps(n_groups=len(stops)) == []
    assert set(stops) == {SP500, NASDAQ100}
    assert all(v > 0 for v in stops.values())


def test_configured_stops_reflect_the_measured_atr_ratio():
    """Both underlyings get the same ATR multiple. If someone re-tunes one
    without the other, the tighter side gets stopped out disproportionately —
    the exact flaw in the original hand-picked values."""
    _, stops = rb.from_config()
    # 2x median 15-min ATR: S&P 19.8pts, Nasdaq 121.1pts at the measured levels.
    assert 15 <= stops[SP500] <= 25
    assert 100 <= stops[NASDAQ100] <= 140


def test_incoherent_config_refuses_to_load(monkeypatch):
    import config
    monkeypatch.setattr(config, "RISK_PER_GROUP_PCT", 1.0)   # 1.0 x 2 <= 2.0
    monkeypatch.setattr(config, "RISK_TOTAL_PCT", 2.0)
    with pytest.raises(RuntimeError, match="never fire"):
        rb.from_config()


def test_missing_stop_distance_refuses_to_load(monkeypatch):
    import config
    monkeypatch.setattr(config, "FUTURES_STOP_POINTS", {SP500: 20.0})
    with pytest.raises(RuntimeError, match="NASDAQ100"):
        rb.from_config()


def test_per_trade_above_per_group_is_flagged():
    problems = RiskLimits(per_trade_pct=2.0, per_group_pct=1.0, total_pct=5.0).unreachable_caps()
    assert any("per_trade can never bind" in p for p in problems)


# --- what must never be blocked ---------------------------------------------

def test_closing_is_always_allowed_even_when_every_cap_is_breached():
    """Refusing to let a book de-risk because it is already too risky would
    trap you in an oversized position exactly when exiting matters most."""
    wildly_over = {"ES": 20, "NQ": 20}
    for add in (0, -1, -50):
        ok, why = rb.evaluate("ES", add, wildly_over, STOPS, EQUITY, LIMITS)
        assert ok, f"a closing/reducing order was blocked: {why}"


# --- headroom ---------------------------------------------------------------

def test_headroom_on_an_empty_book_is_the_per_trade_cap():
    # $1,251 per-trade budget / $100 per micro = 12
    assert rb.headroom(SP500, {}, STOPS, EQUITY, LIMITS) == 12


def test_headroom_stays_at_the_per_trade_cap_until_the_group_fills():
    # 20 micros held = $2,000; group has $1,754 left, so per-trade still binds
    assert rb.headroom(SP500, {"ES": 2}, STOPS, EQUITY, LIMITS) == 12


def test_headroom_shrinks_once_the_group_cap_bites():
    # 30 micros held = $3,000; group cap $3,754 leaves $754 -> 7 micros
    assert rb.headroom(SP500, {"ES": 3}, STOPS, EQUITY, LIMITS) == 7


def test_headroom_is_zero_when_a_cap_already_binds():
    # 40 micros held = $4,000, past the $3,754 group cap
    assert rb.headroom(SP500, {"ES": 4}, STOPS, EQUITY, LIMITS) == 0


def test_headroom_never_proposes_something_evaluate_would_reject():
    """The two must agree, or sizing proposes trades the Guardrail rejects."""
    for held in ({}, {"ES": 1}, {"ES": 2}, {"ES": 3}, {"NQ": 2},
                 {"ES": 1, "NQ": 1}, {"ES": 2, "NQ": 2}, {"ES": 3, "MES": 5}):
        room = rb.headroom(SP500, held, STOPS, EQUITY, LIMITS)
        if room:
            ok, why = rb.evaluate("MES", room, held, STOPS, EQUITY, LIMITS)
            assert ok, f"headroom said {room} but evaluate rejected it: {why}"
        ok, _ = rb.evaluate("MES", room + 1, held, STOPS, EQUITY, LIMITS)
        assert not ok, f"headroom said {room} but {room + 1} was also allowed"


# --- the contradiction this design resolves ---------------------------------

def test_a_risk_sized_trade_passes_despite_enormous_notional():
    """0.5% risk produces 186% notional. A notional cap would reject this
    trade even though its risk is exactly what was asked for — which is why
    the cap is denominated in risk instead."""
    import contract_specs as cs
    micros = cs.micros_for_dollar_risk("MES", EQUITY * 0.005, STOPS[SP500])
    order = cs.fill(SP500, micros)
    notional = cs.group_notional(order, {SP500: 7764})[SP500]

    assert notional / EQUITY > 1.8                      # 186% of equity
    ok, _ = rb.evaluate("MES", micros, {}, STOPS, EQUITY, LIMITS)
    assert ok                                            # yet within risk budget
    assert rb.risk_of(micros, SP500, STOPS) / EQUITY < 0.005
