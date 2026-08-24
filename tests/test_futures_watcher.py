"""futures_watcher.py aggregates IB's 5-second real-time bars into 1-minute
bars per symbol (the phase-1 spike found 5s bars and noted "aggregation to
1m is required" — spike/ib_connect.py check 3) and fires the same
rsi_oversold/rsi_overbought signals futures_strategy.py already understands.

Bar aggregation and trigger logic are synchronous (on_bar callbacks from
ib_async are not awaited — see futures_watcher.py's module docstring for
why put_nowait is used instead of an async queue.put), so they're tested
directly with no event loop needed. The IB connection itself is mocked,
same as ib_broker.py's tests — nothing here has been exercised against a
real IB connection or real market data."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import futures_watcher as fw_module
from futures_watcher import FuturesWatcher


class FakeEvent:
    """Stands in for eventkit's Event, which real ib_async bars use for
    ``updateEvent``. Only the ``+=`` (connect) behavior this code relies on."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def rt_bar(t, o, h, l, c, v):
    return SimpleNamespace(time=t, open_=o, high=h, low=l, close=c, volume=v)


def minute(base, n):
    return base + timedelta(minutes=n)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "IB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "IB_PORT", 4002)
    monkeypatch.setattr(config, "IB_WATCHER_CLIENT_ID", 8)
    monkeypatch.setattr(config, "FUTURES_WATCHLIST", ["MES", "MNQ"])
    monkeypatch.setattr(config, "RSI_PERIOD", 14)
    monkeypatch.setattr(config, "RSI_OVERBOUGHT", 70.0)
    monkeypatch.setattr(config, "RSI_OVERSOLD", 30.0)
    monkeypatch.setattr(config, "SIGNAL_COOLDOWN_SECONDS", 600)


def make_watcher():
    return FuturesWatcher(asyncio.Queue())


def drain(w):
    signals = []
    while not w.signal_queue.empty():
        signals.append(w.signal_queue.get_nowait())
    return signals


def feed_closes(w, symbol, closes, base):
    """One finalized 1-minute bar per close, by rolling the minute forward
    for each — plus one trailing bar to finalize the last close."""
    for i, close in enumerate(closes):
        w._on_realtime_bar(symbol, rt_bar(minute(base, i), close, close, close, close, 1))
    w._on_realtime_bar(symbol, rt_bar(minute(base, len(closes)), closes[-1], closes[-1], closes[-1], closes[-1], 1))


# --- construction: paper-only guarantee -------------------------------------

def test_refuses_to_construct_against_a_live_port(monkeypatch):
    monkeypatch.setattr(config, "IB_PORT", 7496)  # TWS LIVE
    with pytest.raises(RuntimeError, match="LIVE"):
        make_watcher()


def test_constructs_fine_against_a_paper_port():
    make_watcher()  # must not raise


# --- bar aggregation ----------------------------------------------------

def test_five_second_bars_aggregate_into_one_minute_bar():
    w = make_watcher()
    base = datetime(2026, 8, 24, 14, 30, 0)
    w._on_realtime_bar("MES", rt_bar(base, 100.0, 101.0, 99.5, 100.5, 10))
    w._on_realtime_bar("MES", rt_bar(base + timedelta(seconds=5), 100.5, 102.0, 100.0, 101.5, 15))
    w._on_realtime_bar("MES", rt_bar(base + timedelta(seconds=10), 101.5, 101.8, 101.0, 101.2, 5))
    # The next minute's first bar is what finalizes the previous one.
    w._on_realtime_bar("MES", rt_bar(minute(base, 1), 101.2, 101.3, 101.0, 101.1, 3))

    assert len(w.bars["MES"]) == 1
    row = w.bars["MES"].iloc[0]
    assert row["open"] == 100.0
    assert row["high"] == 102.0
    assert row["low"] == 99.5
    assert row["close"] == 101.2
    assert row["volume"] == 30


def test_symbols_aggregate_independently():
    w = make_watcher()
    base = datetime(2026, 8, 24, 14, 30, 0)
    w._on_realtime_bar("MES", rt_bar(base, 100, 100, 100, 100, 1))
    w._on_realtime_bar("MNQ", rt_bar(base, 200, 200, 200, 200, 2))
    w._on_realtime_bar("MES", rt_bar(minute(base, 1), 101, 101, 101, 101, 1))

    assert len(w.bars["MES"]) == 1
    assert len(w.bars["MNQ"]) == 0  # MNQ's minute hasn't rolled over — nothing to finalize yet


def test_history_is_capped_at_500_bars():
    w = make_watcher()
    base = datetime(2026, 8, 24, 9, 0, 0)
    for i in range(505):
        w._on_realtime_bar("MES", rt_bar(minute(base, i), 100, 100, 100, 100, 1))
    assert len(w.bars["MES"]) <= 500


# --- triggers ----------------------------------------------------------

def test_oversold_rsi_emits_a_signal():
    w = make_watcher()
    base = datetime(2026, 8, 24, 9, 0, 0)
    feed_closes(w, "MES", [100 - i for i in range(20)], base)  # steadily falling

    kinds = {s.kind for s in drain(w) if s.symbol == "MES"}
    assert "rsi_oversold" in kinds


def test_overbought_rsi_emits_a_signal():
    w = make_watcher()
    base = datetime(2026, 8, 24, 9, 0, 0)
    feed_closes(w, "MES", [100 + i for i in range(20)], base)  # steadily rising

    kinds = {s.kind for s in drain(w) if s.symbol == "MES"}
    assert "rsi_overbought" in kinds


def test_perfectly_flat_closes_is_rsi_100_and_does_trigger_overbought():
    """compute_rsi's own documented edge case (indicators.py: zero average
    loss -> RSI 100.0) — the same math the equity Watcher already relies
    on. Flat prices are "no losses at all," which Wilder's RSI treats as
    maximally overbought, not neutral."""
    w = make_watcher()
    base = datetime(2026, 8, 24, 9, 0, 0)
    feed_closes(w, "MES", [100] * 20, base)

    signals = drain(w)
    assert any(s.kind == "rsi_overbought" and s.detail["rsi"] == 100.0 for s in signals)


def test_mild_chop_within_both_thresholds_never_triggers():
    base = datetime(2026, 8, 24, 9, 0, 0)
    w = make_watcher()
    closes = [100, 100.5, 100, 99.5, 100, 100.5, 100, 99.5] * 3  # small back-and-forth, no clear trend
    feed_closes(w, "MES", closes, base)
    assert w.signal_queue.empty()


def test_cooldown_suppresses_a_repeat_signal():
    w = make_watcher()
    base = datetime(2026, 8, 24, 9, 0, 0)
    feed_closes(w, "MES", [100 - i for i in range(20)], base)
    first_count = len(drain(w))
    assert first_count > 0

    # Still falling, still oversold — the cooldown should suppress a repeat.
    w._on_realtime_bar("MES", rt_bar(minute(base, 21), 79, 79, 79, 79, 1))
    w._on_realtime_bar("MES", rt_bar(minute(base, 22), 78, 78, 78, 78, 1))

    assert w.signal_queue.empty()


def test_a_different_symbol_is_not_held_back_by_anothers_cooldown():
    w = make_watcher()
    base = datetime(2026, 8, 24, 9, 0, 0)
    feed_closes(w, "MES", [100 - i for i in range(20)], base)
    drain(w)

    feed_closes(w, "MNQ", [200 - i for i in range(20)], base)
    kinds = {s.kind for s in drain(w) if s.symbol == "MNQ"}
    assert "rsi_oversold" in kinds


# --- subscription: ContFuture, not the tradable Future ------------------

async def test_subscribe_uses_contfuture_for_market_data(monkeypatch):
    w = make_watcher()
    fib = MagicMock()
    fib.isConnected = MagicMock(return_value=True)
    qualified = SimpleNamespace(conId=1, symbol="MES")
    fib.qualifyContractsAsync = AsyncMock(return_value=[qualified])

    events = []

    def fake_req_realtime_bars(contract, bar_size, what_to_show, use_rth):
        bars_obj = SimpleNamespace(updateEvent=FakeEvent(), contract=contract)
        events.append(bars_obj)
        return bars_obj

    fib.reqRealTimeBars = MagicMock(side_effect=fake_req_realtime_bars)
    w.ib = fib

    await w._subscribe_all()

    assert fib.qualifyContractsAsync.await_count == 2  # MES, MNQ
    contract_arg = fib.qualifyContractsAsync.await_args_list[0].args[0]
    assert type(contract_arg).__name__ == "ContFuture"
    assert len(events) == 2
    for bars_obj in events:
        assert len(bars_obj.updateEvent.handlers) == 1  # subscribed exactly once


async def test_subscribe_raises_when_qualification_fails(monkeypatch):
    w = make_watcher()
    fib = MagicMock()
    fib.qualifyContractsAsync = AsyncMock(return_value=[None])
    w.ib = fib
    with pytest.raises(RuntimeError, match="MES"):
        await w._subscribe_all()


# --- ORB fade wiring -----------------------------------------------------

def test_finalize_minute_feeds_the_orb_tracker():
    w = make_watcher()
    base = datetime(2026, 8, 24, 14, 30, 0)
    w._on_realtime_bar("MES", rt_bar(base, 100.0, 101.0, 99.5, 100.5, 10))
    w._on_realtime_bar("MES", rt_bar(minute(base, 1), 100.5, 100.6, 100.4, 100.5, 1))  # rolls the minute

    assert w._orb["MES"].range_high == 101.0
    assert w._orb["MES"].range_low == 99.5


def seed_closes(w, symbol, closes, base):
    """Directly seeds w.bars[symbol] with a synthetic closes history, so RSI
    is deterministic without also having to drive real ORB breach state."""
    df = w.bars[symbol]
    for i, c in enumerate(closes):
        df.loc[len(df)] = [minute(base, i), c, c, c, c, 1]
    w.bars[symbol] = df


def test_orb_fade_down_with_rsi_oversold_emits_orb_fade_buy():
    w = make_watcher()
    seed_closes(w, "MES", [100 - i for i in range(20)], datetime(2026, 8, 24, 9, 0, 0))  # RSI oversold
    w._check_triggers("MES", price=80.0, fade="fade_down")
    assert "orb_fade_buy" in {s.kind for s in drain(w)}


def test_orb_fade_up_with_rsi_overbought_emits_orb_fade_sell():
    w = make_watcher()
    seed_closes(w, "MES", [100 + i for i in range(20)], datetime(2026, 8, 24, 9, 0, 0))  # RSI overbought
    w._check_triggers("MES", price=120.0, fade="fade_up")
    assert "orb_fade_sell" in {s.kind for s in drain(w)}


def test_fade_without_matching_rsi_never_emits_the_orb_kind():
    """A failed breakdown (fade_down, wants oversold) with RSI actually
    overbought must not fire the ORB signal — the plain RSI kind can still
    fire on its own, but the fade needs its own matching confirmation."""
    w = make_watcher()
    seed_closes(w, "MES", [100 + i for i in range(20)], datetime(2026, 8, 24, 9, 0, 0))  # overbought, not oversold
    w._check_triggers("MES", price=120.0, fade="fade_down")
    kinds = {s.kind for s in drain(w)}
    assert "orb_fade_buy" not in kinds


def test_no_fade_never_emits_an_orb_kind_even_with_matching_rsi():
    w = make_watcher()
    seed_closes(w, "MES", [100 - i for i in range(20)], datetime(2026, 8, 24, 9, 0, 0))  # oversold
    w._check_triggers("MES", price=80.0, fade=None)
    kinds = {s.kind for s in drain(w)}
    assert "orb_fade_buy" not in kinds
    assert "rsi_oversold" in kinds  # the plain RSI trigger is unaffected


async def test_bar_handler_routes_the_latest_bar_only():
    """The event callback receives (bar_list, has_new_bar) — a growing list
    of every bar ever received, per ib_async's RealTimeBarList — so the
    handler must read only the newest entry, and skip entirely when
    has_new_bar is False."""
    w = make_watcher()
    handler = w._bar_handler("MES")
    base = datetime(2026, 8, 24, 9, 0, 0)
    bar_list = [rt_bar(base, 1, 1, 1, 1, 1)]

    handler(bar_list, False)  # no new bar — must be ignored
    assert len(w.bars["MES"]) == 0 and w._minute_buffer["MES"] == []

    bar_list.append(rt_bar(base + timedelta(seconds=5), 2, 2, 2, 2, 1))
    handler(bar_list, True)
    assert len(w._minute_buffer["MES"]) == 1
    assert w._minute_buffer["MES"][0].close == 2
