"""Time-based exits. The cases that matter most are the ones where it must
NOT fire — closing a position on bad evidence is worse than holding one."""
import json
import time

import pytest

import time_exit as te

MIN = 60.0


def write_log(tmp_path, records):
    p = tmp_path / "execution_log.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(p)


def buy(symbol, ts):
    return {"ts": ts, "symbol": symbol, "outcome": "submitted",
            "detail": "", "proposal": {"side": "buy", "notional": 5000.0, "reason": ""}}


def sell(symbol, ts):
    return {"ts": ts, "symbol": symbol, "outcome": "submitted",
            "detail": "", "proposal": {"side": "sell", "notional": None, "reason": ""}}


def stopped(symbol, ts):
    return {"ts": ts, "symbol": symbol, "outcome": "stop_loss_closed",
            "detail": "", "proposal": None}


# --- entry times ------------------------------------------------------------

def test_entry_time_read_from_the_log(tmp_path):
    path = write_log(tmp_path, [buy("AAPL", 1000.0)])
    assert te.entry_times(path) == {"AAPL": 1000.0}


def test_a_closed_position_is_forgotten(tmp_path):
    path = write_log(tmp_path, [buy("AAPL", 1000.0), sell("AAPL", 2000.0)])
    assert te.entry_times(path) == {}


def test_reentry_resets_the_clock(tmp_path):
    """Without this a re-bought symbol inherits the original position's age and
    is flattened the moment it opens."""
    path = write_log(tmp_path, [buy("AAPL", 1000.0), sell("AAPL", 2000.0), buy("AAPL", 9000.0)])
    assert te.entry_times(path) == {"AAPL": 9000.0}


def test_stop_loss_close_also_clears_the_entry(tmp_path):
    path = write_log(tmp_path, [buy("NVDA", 1000.0), stopped("NVDA", 1500.0)])
    assert te.entry_times(path) == {}


def test_unparseable_line_does_not_crash(tmp_path):
    """A half-written final line is normal if the process died mid-append."""
    p = tmp_path / "execution_log.jsonl"
    p.write_text(json.dumps(buy("AAPL", 1000.0)) + "\n{ broken", encoding="utf-8")
    assert te.entry_times(str(p)) == {"AAPL": 1000.0}


def test_missing_log_is_empty_not_an_error(tmp_path):
    assert te.entry_times(str(tmp_path / "nope.jsonl")) == {}


# --- max hold ---------------------------------------------------------------

def test_position_past_max_hold_is_returned():
    now = 10_000.0
    exits = te.past_max_hold(["AAPL"], {"AAPL": now - 90 * MIN}, 60, now=now)
    assert [e.symbol for e in exits] == ["AAPL"]
    assert "90 min" in exits[0].reason


def test_position_inside_max_hold_is_left_alone():
    now = 10_000.0
    assert te.past_max_hold(["AAPL"], {"AAPL": now - 30 * MIN}, 60, now=now) == []


def test_max_hold_of_zero_disables_it():
    now = 10_000.0
    assert te.past_max_hold(["AAPL"], {"AAPL": now - 999 * MIN}, 0, now=now) == []


def test_position_with_no_recorded_entry_is_not_closed():
    """The log may predate the position. Closing on no evidence is worse than
    holding — the stop loss still covers it."""
    now = 10_000.0
    assert te.past_max_hold(["AAPL"], {}, 60, now=now) == []


# --- close window -----------------------------------------------------------

def test_inside_the_window_flattens_everything_held():
    exits = te.flatten_for_close(["AAPL", "NVDA"], seconds_to_close=10 * MIN,
                                 flatten_before_close_minutes=15)
    assert {e.symbol for e in exits} == {"AAPL", "NVDA"}
    assert "overnight" in exits[0].reason


def test_outside_the_window_holds():
    assert te.flatten_for_close(["AAPL"], seconds_to_close=120 * MIN,
                                flatten_before_close_minutes=15) == []


def test_unknown_close_time_never_triggers_a_flatten():
    """None means the market is shut or the clock failed. Neither is a reason
    to liquidate a book."""
    assert te.flatten_for_close(["AAPL"], seconds_to_close=None,
                                flatten_before_close_minutes=15) == []
    assert te.within_close_window(None, 15) is False


def test_already_past_close_does_not_flatten():
    # Non-positive seconds means the session already ended; nothing to do.
    assert te.flatten_for_close(["AAPL"], seconds_to_close=0, flatten_before_close_minutes=15) == []
    assert te.flatten_for_close(["AAPL"], seconds_to_close=-60, flatten_before_close_minutes=15) == []


def test_flatten_of_zero_disables_it():
    assert te.flatten_for_close(["AAPL"], seconds_to_close=60, flatten_before_close_minutes=0) == []


# --- combined ---------------------------------------------------------------

def test_a_symbol_hitting_both_rules_is_returned_once():
    now = 10_000.0
    exits = te.due(["AAPL"], {"AAPL": now - 200 * MIN}, seconds_to_close=5 * MIN,
                   max_hold_minutes=60, flatten_before_close_minutes=15, now=now)
    assert len(exits) == 1
    assert "close" in exits[0].reason      # the imminent close is the better reason


def test_both_rules_off_means_nothing_is_closed():
    now = 10_000.0
    assert te.due(["AAPL"], {"AAPL": now - 999 * MIN}, seconds_to_close=1 * MIN,
                  max_hold_minutes=0, flatten_before_close_minutes=0, now=now) == []


def test_flat_book_produces_nothing():
    assert te.due([], {}, seconds_to_close=1 * MIN, max_hold_minutes=60,
                  flatten_before_close_minutes=15) == []


def test_shipped_defaults_flatten_but_do_not_cap_hold():
    import config
    assert config.FLATTEN_BEFORE_CLOSE_MINUTES > 0
    assert config.MAX_HOLD_MINUTES == 0, "max hold should be opt-in, not a default"
