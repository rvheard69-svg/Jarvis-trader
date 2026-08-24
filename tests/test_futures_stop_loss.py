"""futures_stop_loss.py — pure logic, no broker calls. Mirrors
risk_guardrail.py's stopped_out_positions check, but in index points
against config.FUTURES_STOP_POINTS instead of a percent."""
import pytest

import futures_stop_loss as fsl
from contract_specs import NASDAQ100, SP500
from ib_broker import PositionDetail

STOPS = {SP500: 20.0, NASDAQ100: 120.0}


def positions(**kwargs):
    """kwargs: symbol=(qty, entry_price)"""
    return {symbol: PositionDetail(qty=qty, avg_entry_price=entry) for symbol, (qty, entry) in kwargs.items()}


# --- the basic trigger -----------------------------------------------------

def test_price_within_the_stop_distance_is_not_stopped_out():
    pos = positions(MES=(2, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={SP500: 7750.0}, stop_points=STOPS)  # 14pts against, stop is 20
    assert result == []


def test_price_exactly_at_the_stop_distance_is_stopped_out():
    pos = positions(MES=(2, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={SP500: 7744.0}, stop_points=STOPS)  # exactly 20pts against
    assert len(result) == 1
    hit = result[0]
    assert hit.symbol == "MES"
    assert hit.group == SP500
    assert hit.points_against == 20.0


def test_price_beyond_the_stop_distance_is_stopped_out():
    pos = positions(MES=(2, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={SP500: 7700.0}, stop_points=STOPS)  # 64pts against
    assert len(result) == 1
    assert result[0].points_against == 64.0


def test_price_above_entry_is_never_stopped_out():
    """A winning position must never trip the stop."""
    pos = positions(MES=(2, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={SP500: 7900.0}, stop_points=STOPS)
    assert result == []


# --- long-only guard -------------------------------------------------------

def test_a_short_position_is_never_flagged():
    """This pipeline never opens a short; a negative qty found in the
    account (something opened outside this app) is left for a human, not
    guessed at here."""
    pos = positions(MES=(-2, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={SP500: 7700.0}, stop_points=STOPS)
    assert result == []


def test_a_flat_position_is_never_flagged():
    pos = positions(MES=(0, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={SP500: 7700.0}, stop_points=STOPS)
    assert result == []


# --- missing data is skipped, not guessed -----------------------------------

def test_no_current_price_for_the_group_is_skipped():
    pos = positions(MES=(2, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={}, stop_points=STOPS)
    assert result == []


def test_no_stop_distance_configured_never_fires():
    pos = positions(MES=(2, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={SP500: 1.0}, stop_points={})
    assert result == []


def test_unknown_symbol_is_ignored():
    pos = {"CL": PositionDetail(qty=5, avg_entry_price=70.0)}
    result = fsl.find_stopped_out(pos, current_prices={}, stop_points=STOPS)
    assert result == []


# --- multiple positions / groups --------------------------------------------

def test_only_the_positions_actually_past_their_stop_fire():
    pos = positions(MES=(2, 7764.0), MNQ=(3, 29742.0))
    result = fsl.find_stopped_out(
        pos,
        current_prices={SP500: 7760.0, NASDAQ100: 29500.0},  # SP500: 4pts (fine); NASDAQ100: 242pts (past 120)
        stop_points=STOPS,
    )
    assert {r.symbol for r in result} == {"MNQ"}


def test_both_groups_can_be_stopped_out_at_once():
    pos = positions(MES=(2, 7764.0), MNQ=(3, 29742.0))
    result = fsl.find_stopped_out(
        pos,
        current_prices={SP500: 7700.0, NASDAQ100: 29500.0},
        stop_points=STOPS,
    )
    assert {r.symbol for r in result} == {"MES", "MNQ"}


def test_mini_and_micro_in_the_same_group_are_each_evaluated_on_their_own_entry():
    """ES and MES may have been entered at different prices (different
    signals, different times) even though they share a group."""
    pos = positions(ES=(1, 7800.0), MES=(2, 7764.0))
    result = fsl.find_stopped_out(pos, current_prices={SP500: 7770.0}, stop_points=STOPS)
    # ES: 30pts against (past 20-pt stop) -> stopped out. MES: 6pts against -> fine.
    assert {r.symbol for r in result} == {"ES"}
