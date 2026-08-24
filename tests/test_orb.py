"""OrbTracker — pure logic, no broker calls, unit-testable in isolation
(same philosophy as contract_specs.py/risk_budget.py). Fires a fade signal
only on a genuine breach-then-revert: price has to actually poke outside
the opening range and then close back inside it, not merely approach the
edge."""
from datetime import datetime, timedelta

from orb import OrbTracker

DAY1 = datetime(2026, 8, 24, 9, 30, 0)
DAY2 = datetime(2026, 8, 25, 9, 30, 0)


def bar(t, high, low, close):
    return {"timestamp": t, "high": high, "low": low, "close": close}


def feed(tracker, bars):
    """Feed a list of bar dicts through update(); return the list of
    non-None results, in order."""
    return [r for r in (tracker.update(**b) for b in bars) if r is not None]


# --- range building --------------------------------------------------------

def test_first_bar_of_a_session_seeds_the_range_and_fires_nothing():
    t = OrbTracker(window_minutes=15)
    assert t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0) is None


def test_bars_within_the_window_extend_the_range_and_fire_nothing():
    t = OrbTracker(window_minutes=15)
    results = feed(t, [
        bar(DAY1, 101.0, 99.0, 100.0),
        bar(DAY1 + timedelta(minutes=5), 103.0, 98.0, 102.0),   # widens both sides
        bar(DAY1 + timedelta(minutes=10), 102.0, 100.0, 101.0),
    ])
    assert results == []
    assert t.range_high == 103.0
    assert t.range_low == 98.0


def test_a_bar_after_the_window_with_no_breach_fires_nothing():
    t = OrbTracker(window_minutes=15)
    t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0)
    result = t.update(timestamp=DAY1 + timedelta(minutes=20), high=100.5, low=99.5, close=100.0)
    assert result is None


# --- fade detection ----------------------------------------------------

def test_breach_above_then_revert_fires_fade_up():
    t = OrbTracker(window_minutes=15)
    t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0)  # range: [99, 101]
    # Breaks above the range high — no fade yet, still outside.
    assert t.update(timestamp=DAY1 + timedelta(minutes=20), high=103.0, low=101.5, close=102.5) is None
    # Closes back inside the range — the breakout failed.
    assert t.update(timestamp=DAY1 + timedelta(minutes=21), high=102.5, low=100.0, close=100.5) == "fade_up"


def test_breach_below_then_revert_fires_fade_down():
    t = OrbTracker(window_minutes=15)
    t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0)  # range: [99, 101]
    assert t.update(timestamp=DAY1 + timedelta(minutes=20), high=98.5, low=97.0, close=97.5) is None
    assert t.update(timestamp=DAY1 + timedelta(minutes=21), high=99.5, low=97.0, close=99.2) == "fade_down"


def test_staying_outside_the_range_never_fires():
    """A real breakout that holds is not a fade — the whole point is the
    reversal, not just crossing the line."""
    t = OrbTracker(window_minutes=15)
    t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0)
    results = feed(t, [
        bar(DAY1 + timedelta(minutes=20), 103.0, 101.5, 102.5),
        bar(DAY1 + timedelta(minutes=21), 104.0, 102.5, 103.5),
        bar(DAY1 + timedelta(minutes=22), 105.0, 103.5, 104.5),
    ])
    assert results == []


def test_a_fade_does_not_refire_without_a_fresh_breach():
    t = OrbTracker(window_minutes=15)
    t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0)
    t.update(timestamp=DAY1 + timedelta(minutes=20), high=103.0, low=101.5, close=102.5)
    assert t.update(timestamp=DAY1 + timedelta(minutes=21), high=102.5, low=100.0, close=100.5) == "fade_up"
    # Still inside the range, no new breach — must not fire again.
    result = t.update(timestamp=DAY1 + timedelta(minutes=22), high=100.6, low=100.0, close=100.3)
    assert result is None


def test_a_fresh_breach_can_fire_again_the_same_session():
    t = OrbTracker(window_minutes=15)
    t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0)
    t.update(timestamp=DAY1 + timedelta(minutes=20), high=103.0, low=101.5, close=102.5)
    assert t.update(timestamp=DAY1 + timedelta(minutes=21), high=102.5, low=100.0, close=100.5) == "fade_up"
    # Breaks out again, then fails again.
    t.update(timestamp=DAY1 + timedelta(minutes=22), high=104.0, low=101.0, close=103.0)
    assert t.update(timestamp=DAY1 + timedelta(minutes=23), high=103.0, low=100.5, close=100.8) == "fade_up"


def test_both_sides_can_fade_independently_the_same_session():
    t = OrbTracker(window_minutes=15)
    t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0)
    t.update(timestamp=DAY1 + timedelta(minutes=20), high=103.0, low=101.5, close=102.5)
    assert t.update(timestamp=DAY1 + timedelta(minutes=21), high=102.5, low=100.0, close=100.5) == "fade_up"
    t.update(timestamp=DAY1 + timedelta(minutes=22), high=100.5, low=97.0, close=97.5)
    assert t.update(timestamp=DAY1 + timedelta(minutes=23), high=99.5, low=97.0, close=99.2) == "fade_down"


# --- session reset -------------------------------------------------------

def test_a_new_calendar_day_resets_the_range_and_clears_breach_state():
    t = OrbTracker(window_minutes=15)
    t.update(timestamp=DAY1, high=101.0, low=99.0, close=100.0)
    t.update(timestamp=DAY1 + timedelta(minutes=20), high=103.0, low=101.5, close=102.5)  # breached, unresolved

    # New day: first bar just reseeds, even though yesterday's breach never fired.
    result = t.update(timestamp=DAY2, high=201.0, low=199.0, close=200.0)
    assert result is None
    assert t.range_high == 201.0 and t.range_low == 199.0

    # A value that would have fired yesterday's stale breach must not fire
    # today against today's fresh, unrelated range.
    result = t.update(timestamp=DAY2 + timedelta(minutes=20), high=200.5, low=199.5, close=100.5)
    assert result is None
