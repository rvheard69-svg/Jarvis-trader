"""
Opening Range Breakout fade — pure logic, no broker calls, unit-testable in
isolation (same philosophy as contract_specs.py/risk_budget.py: this has to
be right before either Watcher wires it to a signal).

The rule: the first `window_minutes` of a session set a high/low range.
Once that window closes, if price pokes outside the range and then a later
bar closes back inside it, the breakout failed — that's the fade. Merely
touching or holding beyond the range is NOT a fade; a real breakout that
holds is the opposite of what this detects on purpose (see
test_staying_outside_the_range_never_fires).

Session boundary: a new calendar day (by `timestamp.date()`) resets the
range and clears breach state. This is a real simplification worth naming:
for a 24-hour futures session, "calendar day" is not the same thing as the
RTH day-session open a professional futures ORB trader would anchor to —
it's whatever time bars happen to start arriving that UTC date. Treat this
as the smallest rule that exercises the pipeline (same caveat strategy.py
gives its own RSI rule), not a claim about the "correct" futures session
boundary — revisit if the anchor time matters for how you actually trade
this.

One fade per breach: after "fade_up" fires, price sitting back inside the
range does not refire it — a fresh breach above the range is required
before it can fire "fade_up" again. The two sides (fade_up/fade_down)
track independently.
"""
from __future__ import annotations

from datetime import datetime, timedelta


class OrbTracker:
    def __init__(self, window_minutes: int):
        self.window_minutes = window_minutes
        self.range_high: float | None = None
        self.range_low: float | None = None
        self._session_date = None
        self._range_start: datetime | None = None
        self._breached_above = False
        self._breached_below = False

    def update(self, timestamp: datetime, high: float, low: float, close: float) -> str | None:
        """Feed one finalized bar. Returns "fade_up" (failed breakout
        above — bearish), "fade_down" (failed breakdown below — bullish),
        or None."""
        date = timestamp.date()
        if date != self._session_date:
            self._start_session(date, timestamp, high, low)
            return None

        if timestamp - self._range_start < timedelta(minutes=self.window_minutes):
            self.range_high = max(self.range_high, high)
            self.range_low = min(self.range_low, low)
            return None

        if high > self.range_high:
            self._breached_above = True
        if low < self.range_low:
            self._breached_below = True

        if self._breached_above and close <= self.range_high:
            self._breached_above = False
            return "fade_up"
        if self._breached_below and close >= self.range_low:
            self._breached_below = False
            return "fade_down"
        return None

    def _start_session(self, date, timestamp: datetime, high: float, low: float) -> None:
        self._session_date = date
        self._range_start = timestamp
        self.range_high = high
        self.range_low = low
        self._breached_above = False
        self._breached_below = False
