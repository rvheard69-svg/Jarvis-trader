"""The futures trade rule — same shape and same restraint as strategy.py:
deterministic, long-only, no averaging down. The one thing that has to
change from the equity version is the unit everything checks at: a group
(SP500/NASDAQ100), never a single symbol, because ES and MES are one bet
(risk_budget.py's Hazard 04)."""
import pytest

import config
import contract_specs as cs
import futures_strategy as fs
from contract_specs import NASDAQ100, SP500

EQUITY = 250_258.24  # matches tests/test_risk_budget.py's reference account


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.5)
    monkeypatch.setattr(config, "RISK_PER_GROUP_PCT", 1.5)
    monkeypatch.setattr(config, "RISK_TOTAL_PCT", 2.0)
    monkeypatch.setattr(config, "FUTURES_STOP_POINTS", {SP500: 20.0, NASDAQ100: 50.0})


# --- opening ------------------------------------------------------------

def test_oversold_with_flat_group_proposes_a_risk_sized_open():
    proposal = fs.decide("MES", "rsi_oversold", positions={}, equity=EQUITY, rsi=25)
    assert proposal is not None
    assert proposal.side == "open"
    assert proposal.group == SP500
    # 0.5% of EQUITY = $1,251.29 risk budget; $100/micro (5 * 20) -> 12 micros
    assert proposal.add_micros == 12
    assert proposal.contracts == cs.fill(SP500, 12)
    assert "MES" in proposal.reason and "flat" in proposal.reason


def test_oversold_via_the_mini_sizes_the_same_group_budget():
    """Whichever symbol in the group triggered the signal, sizing is the
    same group-level risk budget — not a per-symbol one."""
    via_micro = fs.decide("MES", "rsi_oversold", positions={}, equity=EQUITY, rsi=25)
    via_mini = fs.decide("ES", "rsi_oversold", positions={}, equity=EQUITY, rsi=25)
    assert via_micro.add_micros == via_mini.add_micros
    assert via_micro.group == via_mini.group == SP500


def test_oversold_does_not_average_down_when_group_already_holds_the_mini():
    """The Hazard 04 case: already holding ES, a fresh MES oversold signal
    must not propose adding — same underlying, not a fresh entry."""
    assert fs.decide("MES", "rsi_oversold", positions={"ES": 1}, equity=EQUITY, rsi=25) is None


def test_oversold_is_independent_per_group():
    """Holding the S&P doesn't block a fresh Nasdaq entry."""
    proposal = fs.decide("MNQ", "rsi_oversold", positions={"ES": 1}, equity=EQUITY, rsi=25)
    assert proposal is not None
    assert proposal.group == NASDAQ100


def test_oversold_proposes_nothing_when_sizing_rounds_to_zero(monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.0001)  # rounds to 0 micros
    assert fs.decide("MES", "rsi_oversold", positions={}, equity=EQUITY, rsi=25) is None


# --- closing --------------------------------------------------------------

def test_overbought_with_a_position_proposes_closing_the_whole_group():
    """1 ES + 2 MES is one bet — closing has to flatten both, not just
    whichever symbol's RSI happened to trigger."""
    proposal = fs.decide("MES", "rsi_overbought", positions={"ES": 1, "MES": 2}, equity=EQUITY, rsi=80)
    assert proposal is not None
    assert proposal.side == "close"
    assert proposal.group == SP500
    assert proposal.contracts == {"ES": 1, "MES": 2}
    assert proposal.add_micros == 0


def test_overbought_ignores_positions_in_a_different_group():
    proposal = fs.decide("MES", "rsi_overbought", positions={"ES": 1, "MNQ": 3}, equity=EQUITY, rsi=80)
    assert proposal.contracts == {"ES": 1}


def test_overbought_with_flat_group_proposes_nothing():
    assert fs.decide("MES", "rsi_overbought", positions={}, equity=EQUITY, rsi=80) is None
    assert fs.decide("MES", "rsi_overbought", positions={"MNQ": 3}, equity=EQUITY, rsi=80) is None


def test_overbought_closes_a_short_too():
    """This rule never opens a short, but positions is real broker state —
    something else (a manual IB order) could have. Flattening on overbought
    must not skip it just because the sign is unexpected."""
    proposal = fs.decide("MES", "rsi_overbought", positions={"MES": -4}, equity=EQUITY, rsi=80)
    assert proposal.contracts == {"MES": -4}


# --- everything else --------------------------------------------------------

def test_other_signal_kinds_never_propose_a_trade():
    for kind in ("volume_spike", "vwap_cross_up", "vwap_cross_down"):
        assert fs.decide("MES", kind, positions={}, equity=EQUITY, rsi=50) is None


def test_unknown_symbol_is_rejected():
    with pytest.raises(ValueError, match="CL"):
        fs.decide("CL", "rsi_oversold", positions={}, equity=EQUITY, rsi=25)
