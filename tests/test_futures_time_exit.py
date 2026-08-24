"""Futures time-based exits — the max-hold half ports directly from
time_exit.py; the close-window half uses CME's weekly close (Friday
~17:00 America/New_York) instead of a daily one. The cases that matter
most are the ones where it must NOT fire — closing a position on bad
evidence, or on the wrong day, is worse than holding one."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import futures_time_exit as fte

MIN = 60.0
ET = ZoneInfo("America/New_York")

# A real week: 2026-08-24 is a Monday, 2026-08-28 is a Friday.
WED_NOON = datetime(2026, 8, 26, 12, 0, tzinfo=ET)
FRI_16_00 = datetime(2026, 8, 28, 16, 0, tzinfo=ET)
FRI_17_00 = datetime(2026, 8, 28, 17, 0, tzinfo=ET)
FRI_18_00 = datetime(2026, 8, 28, 18, 0, tzinfo=ET)
SAT_NOON = datetime(2026, 8, 29, 12, 0, tzinfo=ET)
MON_01_00 = datetime(2026, 8, 24, 1, 0, tzinfo=ET)


def write_log(tmp_path, records):
    p = tmp_path / "futures_execution_log.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(p)


def opened(symbol, ts):
    return {"ts": ts, "symbol": symbol, "outcome": "submitted",
            "detail": "", "proposal": {"side": "open", "group": "", "contracts": {}, "add_micros": 1, "reason": ""}}


def closed(symbol, ts):
    return {"ts": ts, "symbol": symbol, "outcome": "submitted",
            "detail": "", "proposal": {"side": "close", "group": "", "contracts": {}, "add_micros": 0, "reason": ""}}


def stopped(symbol, ts):
    return {"ts": ts, "symbol": symbol, "outcome": "stop_loss_closed", "detail": "", "proposal": None}


# --- entry times -------------------------------------------------------

def test_entry_time_read_from_the_log(tmp_path):
    path = write_log(tmp_path, [opened("MES", 1000.0)])
    assert fte.entry_times(path) == {"MES": 1000.0}


def test_a_closed_position_is_forgotten(tmp_path):
    path = write_log(tmp_path, [opened("MES", 1000.0), closed("MES", 2000.0)])
    assert fte.entry_times(path) == {}


def test_reentry_resets_the_clock(tmp_path):
    path = write_log(tmp_path, [opened("MES", 1000.0), closed("MES", 2000.0), opened("MES", 9000.0)])
    assert fte.entry_times(path) == {"MES": 9000.0}


def test_stop_loss_close_also_clears_the_entry(tmp_path):
    path = write_log(tmp_path, [opened("MES", 1000.0), stopped("MES", 1500.0)])
    assert fte.entry_times(path) == {}


def test_unparseable_line_does_not_crash(tmp_path):
    p = tmp_path / "futures_execution_log.jsonl"
    p.write_text(json.dumps(opened("MES", 1000.0)) + "\n{ broken", encoding="utf-8")
    assert fte.entry_times(str(p)) == {"MES": 1000.0}


def test_missing_log_is_empty_not_an_error(tmp_path):
    assert fte.entry_times(str(tmp_path / "nope.jsonl")) == {}


def test_legs_of_a_multi_contract_open_are_tracked_independently(tmp_path):
    """A 1 ES + 2 MES open logs one 'submitted' line per leg."""
    path = write_log(tmp_path, [opened("ES", 1000.0), opened("MES", 1000.0)])
    assert fte.entry_times(path) == {"ES": 1000.0, "MES": 1000.0}


# --- max hold ------------------------------------------------------------

def test_position_past_max_hold_is_returned():
    now = 10_000.0
    exits = fte.past_max_hold(["MES"], {"MES": now - 90 * MIN}, 60, now=now)
    assert [e.symbol for e in exits] == ["MES"]
    assert "90 min" in exits[0].reason


def test_position_inside_max_hold_is_left_alone():
    now = 10_000.0
    assert fte.past_max_hold(["MES"], {"MES": now - 30 * MIN}, 60, now=now) == []


def test_max_hold_of_zero_disables_it():
    now = 10_000.0
    assert fte.past_max_hold(["MES"], {"MES": now - 999 * MIN}, 0, now=now) == []


def test_position_with_no_recorded_entry_is_not_closed():
    now = 10_000.0
    assert fte.past_max_hold(["MES"], {}, 60, now=now) == []


# --- weekly close boundary ---------------------------------------------

def test_wednesday_counts_down_to_friday_1700():
    seconds = fte.seconds_to_weekly_close(WED_NOON)
    expected = (FRI_17_00 - WED_NOON).total_seconds()
    assert seconds == expected
    assert 2 * 24 * 3600 < seconds < 3 * 24 * 3600  # roughly 2.2 days out


def test_one_hour_before_friday_close():
    assert fte.seconds_to_weekly_close(FRI_16_00) == 3600.0


def test_exactly_at_friday_close_rolls_to_next_week():
    """At the boundary itself, the close has arrived, not "0 seconds until"
    — the next relevant close is next Friday."""
    seconds = fte.seconds_to_weekly_close(FRI_17_00)
    assert seconds == 7 * 24 * 3600


def test_after_friday_close_counts_to_next_friday():
    seconds = fte.seconds_to_weekly_close(FRI_18_00)
    expected = (FRI_17_00 + timedelta(days=7) - FRI_18_00).total_seconds()
    assert seconds == expected


def test_saturday_counts_to_next_friday():
    seconds = fte.seconds_to_weekly_close(SAT_NOON)
    expected = (FRI_17_00 + timedelta(days=7) - SAT_NOON).total_seconds()
    assert seconds == expected
    assert 6 * 24 * 3600 < seconds < 7 * 24 * 3600


def test_monday_counts_to_this_weeks_friday():
    seconds = fte.seconds_to_weekly_close(MON_01_00)
    expected = (FRI_17_00 - MON_01_00).total_seconds()
    assert seconds == expected


def test_naive_datetime_is_rejected():
    import pytest
    with pytest.raises(ValueError, match="timezone-aware"):
        fte.seconds_to_weekly_close(datetime(2026, 8, 26, 12, 0))


# --- flatten window ------------------------------------------------------

def test_inside_the_window_flattens_everything_held():
    exits = fte.flatten_for_weekly_close(["MES", "MNQ"], seconds_to_close=10 * MIN,
                                          flatten_before_close_minutes=15)
    assert {e.symbol for e in exits} == {"MES", "MNQ"}
    assert "weekend" in exits[0].reason


def test_outside_the_window_holds():
    assert fte.flatten_for_weekly_close(["MES"], seconds_to_close=120 * MIN,
                                         flatten_before_close_minutes=15) == []


def test_unknown_close_time_never_triggers_a_flatten():
    assert fte.flatten_for_weekly_close(["MES"], seconds_to_close=None,
                                         flatten_before_close_minutes=15) == []
    assert fte.within_weekly_close_window(None, 15) is False


def test_already_past_close_does_not_flatten():
    assert fte.flatten_for_weekly_close(["MES"], seconds_to_close=0, flatten_before_close_minutes=15) == []
    assert fte.flatten_for_weekly_close(["MES"], seconds_to_close=-60, flatten_before_close_minutes=15) == []


def test_flatten_of_zero_disables_it():
    assert fte.flatten_for_weekly_close(["MES"], seconds_to_close=60, flatten_before_close_minutes=0) == []


# --- combined --------------------------------------------------------------

def test_a_symbol_hitting_both_rules_is_returned_once():
    now = 10_000.0
    exits = fte.due(["MES"], {"MES": now - 200 * MIN}, seconds_to_weekly_close=5 * MIN,
                     max_hold_minutes=60, flatten_before_close_minutes=15, now=now)
    assert len(exits) == 1
    assert "weekend" in exits[0].reason  # the imminent close is the better reason


def test_both_rules_off_means_nothing_is_closed():
    now = 10_000.0
    assert fte.due(["MES"], {"MES": now - 999 * MIN}, seconds_to_weekly_close=1 * MIN,
                    max_hold_minutes=0, flatten_before_close_minutes=0, now=now) == []


def test_flat_book_produces_nothing():
    assert fte.due([], {}, seconds_to_weekly_close=1 * MIN, max_hold_minutes=60,
                    flatten_before_close_minutes=15) == []


def test_shipped_defaults_flatten_but_do_not_cap_hold():
    import config
    assert config.FUTURES_FLATTEN_BEFORE_WEEKLY_CLOSE_MINUTES > 0
    assert config.FUTURES_MAX_HOLD_MINUTES == 0, "max hold should be opt-in, not a default"
