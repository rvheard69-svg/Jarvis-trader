"""
Futures time-based exits — max-hold ported directly from time_exit.py, plus
a weekly (not daily) close-window flatten.

Why weekly, not daily: equities have one close a day, so "flatten before
close" bounds overnight gap risk every session. CME's ES/MES/NQ/MNQ trade
nearly 24 hours (Sunday ~18:00 through Friday ~17:00 America/New_York),
with a ~17:00-18:00 ET daily maintenance halt Monday-Thursday. That halt is
deliberately NOT treated as a close here — trading resumes an hour later,
which isn't a comparable gap-risk event. The one real multi-day gap-risk
window for these contracts is the weekend, so this only counts down to
Friday's close. This does not account for the CME holiday calendar (an
early Thanksgiving/Christmas close, a holiday Monday) — unverified against
a real calendar; revisit if that matters for how this gets traded.

Two independent rules, either disabled by setting its config to 0 (same
shape as time_exit.py):

  flatten before the weekly close   close everything N minutes before
                                     Friday's close, so weekend gap risk is
                                     opted into rather than inherited
  max hold                          close a position that has run longer
                                     than N minutes — bounds the "RSI/ORB
                                     never came back" case during the week

Entry times come from futures_execution_log.jsonl rather than memory, for
the same restart-safety reason as time_exit.py. Each leg of a multi-contract
open/close (e.g. 1 ES + 2 MES) logs its own line with its own symbol, so
entries are tracked per symbol, same granularity the stop loss uses.

No broker calls here — deciding what to close is separate from closing it,
so this stays testable and the closing path stays the one already proven
by futures_stop_loss.py / FuturesExecutor.force_close().
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_WEEKLY_CLOSE_WEEKDAY = 4  # Friday (Monday=0 .. Sunday=6)
_WEEKLY_CLOSE_HOUR = 17    # 17:00 America/New_York


@dataclass(frozen=True)
class TimedExit:
    symbol: str
    reason: str


def entry_times(execution_log_path: str) -> dict[str, float]:
    """
    When each currently-held symbol was most recently opened, from
    futures_execution_log.jsonl. Reads forward and lets later records
    overwrite earlier ones, so a symbol opened, closed, and reopened
    reports the latest entry — a stale entry would flatten a fresh
    position immediately.
    """
    opened: dict[str, float] = {}
    try:
        with open(execution_log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a partially-written final line is not worth crashing over
                symbol, outcome = rec.get("symbol"), rec.get("outcome")
                proposal = rec.get("proposal") or {}
                if outcome == "submitted" and proposal.get("side") == "open":
                    opened[symbol] = rec["ts"]
                elif outcome == "submitted" and proposal.get("side") == "close":
                    opened.pop(symbol, None)
                elif outcome == "stop_loss_closed":
                    opened.pop(symbol, None)
    except FileNotFoundError:
        return {}
    return opened


def past_max_hold(
    held_symbols: list[str],
    entries: dict[str, float],
    max_hold_minutes: float,
    now: float | None = None,
) -> list[TimedExit]:
    """Positions that have run longer than `max_hold_minutes`. A symbol
    with no recorded entry is left alone — the log may predate the
    position, and guessing wrong means closing something on no evidence."""
    if max_hold_minutes <= 0:
        return []
    now = time.time() if now is None else now
    out = []
    for symbol in held_symbols:
        opened = entries.get(symbol)
        if opened is None:
            continue
        age_min = (now - opened) / 60
        if age_min >= max_hold_minutes:
            out.append(TimedExit(
                symbol,
                f"held {age_min:.0f} min, past the {max_hold_minutes:.0f} min max hold",
            ))
    return out


def seconds_to_weekly_close(now: datetime) -> float:
    """
    Seconds until the next Friday 17:00 America/New_York. `now` must be
    timezone-aware — a naive datetime is rejected rather than silently
    assumed to be UTC or local, since that assumption would be wrong on
    at least one platform.

    Exactly at the boundary, the close has just arrived, not "0 seconds
    left" — the next relevant close is a week out.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    et_now = now.astimezone(_ET)
    days_ahead = (_WEEKLY_CLOSE_WEEKDAY - et_now.weekday()) % 7
    candidate = (et_now + timedelta(days=days_ahead)).replace(
        hour=_WEEKLY_CLOSE_HOUR, minute=0, second=0, microsecond=0
    )
    if candidate <= et_now:
        candidate += timedelta(days=7)
    return (candidate - et_now).total_seconds()


def within_weekly_close_window(seconds_to_close: float | None, flatten_before_close_minutes: float) -> bool:
    """`seconds_to_close` of None means unknown — not computed this cycle,
    or the caller couldn't determine it. Returns False: an unknown close is
    not a reason to liquidate, and the risk loop will ask again next pass."""
    if flatten_before_close_minutes <= 0 or seconds_to_close is None:
        return False
    return 0 < seconds_to_close <= flatten_before_close_minutes * 60


def flatten_for_weekly_close(
    held_symbols: list[str],
    seconds_to_close: float | None,
    flatten_before_close_minutes: float,
) -> list[TimedExit]:
    if not within_weekly_close_window(seconds_to_close, flatten_before_close_minutes):
        return []
    mins = seconds_to_close / 60  # type: ignore[operator]
    return [
        TimedExit(s, f"{mins:.0f} min to the weekly close, flattening rather than carrying over the weekend")
        for s in held_symbols
    ]


def due(
    held_symbols: list[str],
    entries: dict[str, float],
    seconds_to_weekly_close: float | None,
    max_hold_minutes: float,
    flatten_before_close_minutes: float,
    now: float | None = None,
) -> list[TimedExit]:
    """Every futures position due to be closed on time grounds, deduplicated
    by symbol. Close-window exits are listed first: when both rules fire
    for the same position, the imminent close is the more useful reason."""
    out: dict[str, TimedExit] = {}
    for exit_ in flatten_for_weekly_close(held_symbols, seconds_to_weekly_close, flatten_before_close_minutes):
        out[exit_.symbol] = exit_
    for exit_ in past_max_hold(held_symbols, entries, max_hold_minutes, now):
        out.setdefault(exit_.symbol, exit_)
    return list(out.values())
