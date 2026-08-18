"""
Time-based exits: flatten before the close, and cap how long a position runs.

strategy.py only sells on RSI >= RSI_OVERBOUGHT. If overbought never fires, a
position is held indefinitely — which is why three of four positions went
overnight in the first live session. The stop loss does not cover that gap:
measured from real bars, average daily range is 4.5-4.7x the stop distance on
both indices, so a typical overnight move is several times the level the stop
defends. Widening the stop does not fix it either; that only sizes you smaller
while leaving exposure unbounded in time.

Two independent rules, either disabled by setting its config to 0:

  flatten before close   close everything N minutes before the session ends,
                         so overnight gap risk is opted into rather than
                         inherited by default
  max hold               close a position that has run longer than N minutes,
                         which bounds the "RSI never came back" case during
                         the session

Entry times come from execution_log.jsonl rather than memory, so a restart
does not reset every position's clock — the app restarted six times in the
first two sessions.

No broker calls here. Deciding what to close is separate from closing it, so
this stays testable and the closing path stays the one already proven by the
stop loss.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TimedExit:
    symbol: str
    reason: str


def entry_times(execution_log_path: str) -> dict[str, float]:
    """
    When each currently-held symbol was most recently opened, from the
    execution log.

    Reads forward and lets later records overwrite earlier ones, so a symbol
    bought, closed, and bought again reports the latest entry. A close of any
    kind clears the symbol — otherwise a re-entry would inherit the original
    position's age and be flattened immediately.
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
                if outcome == "submitted" and proposal.get("side") == "buy":
                    opened[symbol] = rec["ts"]
                elif outcome in ("submitted", "stop_loss_closed") and proposal.get("side") != "buy":
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
    """
    Positions that have run longer than `max_hold_minutes`.

    A symbol with no recorded entry is left alone rather than closed: the log
    may predate the position, and guessing wrong here means closing something
    on no evidence.
    """
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


def within_close_window(
    seconds_to_close: float | None,
    flatten_before_close_minutes: float,
) -> bool:
    """
    Whether we are inside the flatten window before the session close.

    `seconds_to_close` of None means the session close is unknown — the market
    is shut, or the clock could not be read. Returns False in that case: an
    unknown close is not a reason to liquidate, and the risk loop will ask
    again on its next pass.
    """
    if flatten_before_close_minutes <= 0 or seconds_to_close is None:
        return False
    return 0 < seconds_to_close <= flatten_before_close_minutes * 60


def flatten_for_close(
    held_symbols: list[str],
    seconds_to_close: float | None,
    flatten_before_close_minutes: float,
) -> list[TimedExit]:
    if not within_close_window(seconds_to_close, flatten_before_close_minutes):
        return []
    mins = seconds_to_close / 60  # type: ignore[operator]
    return [
        TimedExit(s, f"{mins:.0f} min to the close, flattening rather than carrying overnight")
        for s in held_symbols
    ]


def due(
    held_symbols: list[str],
    entries: dict[str, float],
    seconds_to_close: float | None,
    max_hold_minutes: float,
    flatten_before_close_minutes: float,
    now: float | None = None,
) -> list[TimedExit]:
    """
    Every position due to be closed on time grounds, deduplicated by symbol.

    Close-window exits are listed first: when both rules fire for the same
    position, the imminent close is the more useful reason to record.
    """
    out: dict[str, TimedExit] = {}
    for exit_ in flatten_for_close(held_symbols, seconds_to_close, flatten_before_close_minutes):
        out[exit_.symbol] = exit_
    for exit_ in past_max_hold(held_symbols, entries, max_hold_minutes, now):
        out.setdefault(exit_.symbol, exit_)
    return list(out.values())
