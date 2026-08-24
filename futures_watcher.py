"""
The futures Watcher: streams IB real-time bars, aggregates them into
1-minute bars per symbol, computes RSI, and fires the same
rsi_oversold/rsi_overbought signals futures_strategy.py understands. Same
role as watcher.py, one layer of market-data reality removed.

Why aggregation is needed at all: IB's reqRealTimeBars only offers 5-second
bars — the phase-1 spike confirmed this (spike/ib_connect.py check 3) and
noted "aggregation to 1m is required." RSI_PERIOD (config.py) is tuned
against 1-minute bars the same way the equity side's is, so this buffers
5s bars per symbol and finalizes one aggregated OHLCV bar every time the
wall-clock minute rolls over.

ContFuture vs. Future: unlike ib_broker.py (which trades the concrete
front-month Future — IB refuses orders against a ContFuture), this
subscribes to ContFuture, exactly like the spike's own market-data check.
ContFuture is explicitly for market data, not for trading.

on_bar callbacks from ib_async (eventkit) are synchronous, not awaited —
that's why signals go onto the queue with put_nowait() instead of an
`await queue.put(...)` the way watcher.py's async Alpaca callback can.

IMPORTANT — unverified against real IB, for the same reason as
ib_broker.py: the phase-1 spike's real-time bars check (3) never actually
received bars in the account's tested session — no CME futures market data
subscription. Bar aggregation and the trigger logic are unit-tested here
against synthetic bars (tests/test_futures_watcher.py); the ContFuture
subscription path has not been exercised against a live IB connection or
real market data. Re-run spike/ib_connect.py and confirm bars actually
arrive before trusting this to generate real signals.

NOT wired into main.py.
"""
from __future__ import annotations

import asyncio
import time

import pandas as pd
from ib_async import IB, ContFuture

import config
import contract_specs as cs
from indicators import compute_rsi
from watcher import Signal

RECONNECT_BASE_DELAY = 5     # seconds
RECONNECT_MAX_DELAY = 300    # cap backoff at 5 minutes
HEALTHY_RUN_SECONDS = 120    # a connection that lasted this long resets the backoff
CONNECTION_POLL_SECONDS = 5  # how often start() checks the IB connection is still alive


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


class FuturesWatcher:
    def __init__(self, signal_queue: asyncio.Queue):
        if config.IB_PORT in config.IB_LIVE_PORTS:
            raise RuntimeError(
                f"IB_PORT={config.IB_PORT} is a LIVE IB port ({sorted(config.IB_LIVE_PORTS)}). "
                f"This project is paper-only — see config.validate()."
            )
        self.signal_queue = signal_queue
        self.bars: dict[str, pd.DataFrame] = {symbol: _empty_bars() for symbol in config.FUTURES_WATCHLIST}
        self._minute_buffer: dict[str, list] = {symbol: [] for symbol in config.FUTURES_WATCHLIST}
        self._current_minute: dict[str, object] = {}  # symbol -> the minute currently buffering
        self._last_fired: dict[tuple[str, str], float] = {}  # (symbol, kind) -> timestamp
        self.ib = IB()

    # --- bar aggregation ---------------------------------------------------

    def _bar_handler(self, symbol: str):
        """Returns the (bar_list, has_new_bar) callback ib_async's
        RealTimeBarList.updateEvent expects. bar_list grows with every bar
        ever received for this subscription — only the newest one matters."""
        def handler(bar_list, has_new_bar) -> None:
            if has_new_bar:
                self._on_realtime_bar(symbol, bar_list[-1])
        return handler

    def _on_realtime_bar(self, symbol: str, bar) -> None:
        # Bucket by wall-clock minute (seconds/microseconds zeroed) rather
        # than counting to 12 — a dropped or delayed 5s bar shouldn't skew
        # which minute a bar belongs to.
        bar_minute = bar.time.replace(second=0, microsecond=0)
        if self._current_minute.get(symbol) is None:
            self._current_minute[symbol] = bar_minute
        elif bar_minute != self._current_minute[symbol]:
            self._finalize_minute(symbol)
            self._current_minute[symbol] = bar_minute
        self._minute_buffer[symbol].append(bar)

    def _finalize_minute(self, symbol: str) -> None:
        buf = self._minute_buffer[symbol]
        if not buf:
            return

        df = self.bars[symbol]
        df.loc[len(df)] = [
            buf[0].time, buf[0].open_,
            max(b.high for b in buf), min(b.low for b in buf),
            buf[-1].close, sum(b.volume for b in buf),
        ]
        if len(df) > 500:
            df = df.tail(500).reset_index(drop=True)
        self.bars[symbol] = df
        self._minute_buffer[symbol] = []

        self._check_triggers(symbol, float(buf[-1].close))

    # --- triggers ------------------------------------------------------------

    def _on_cooldown(self, symbol: str, kind: str) -> bool:
        last = self._last_fired.get((symbol, kind))
        return last is not None and (time.time() - last) < config.SIGNAL_COOLDOWN_SECONDS

    def _mark_fired(self, symbol: str, kind: str) -> None:
        self._last_fired[(symbol, kind)] = time.time()

    def _check_triggers(self, symbol: str, price: float) -> None:
        rsi = compute_rsi(self.bars[symbol]["close"], config.RSI_PERIOD)
        if rsi is None:
            return

        if rsi >= config.RSI_OVERBOUGHT and not self._on_cooldown(symbol, "rsi_overbought"):
            self._mark_fired(symbol, "rsi_overbought")
            self.signal_queue.put_nowait(Signal(symbol=symbol, kind="rsi_overbought", price=price, detail={"rsi": rsi}))

        if rsi <= config.RSI_OVERSOLD and not self._on_cooldown(symbol, "rsi_oversold"):
            self._mark_fired(symbol, "rsi_oversold")
            self.signal_queue.put_nowait(Signal(symbol=symbol, kind="rsi_oversold", price=price, detail={"rsi": rsi}))

    # --- connection / subscription ------------------------------------------

    async def _ensure_connected(self) -> None:
        if self.ib.isConnected():
            return
        await self.ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=config.IB_WATCHER_CLIENT_ID, timeout=10)

    async def _subscribe_all(self) -> None:
        for symbol in config.FUTURES_WATCHLIST:
            spec = cs.SPECS[symbol]
            qualified = await self.ib.qualifyContractsAsync(ContFuture(symbol, spec.exchange))
            if not qualified or qualified[0] is None:
                raise RuntimeError(
                    f"IB could not qualify a ContFuture for {symbol} — no futures market data "
                    f"subscription blocks this the same way it blocked spike/ib_connect.py's checks 3-4"
                )
            bars = self.ib.reqRealTimeBars(qualified[0], 5, "TRADES", False)
            bars.updateEvent += self._bar_handler(symbol)

    # --- run loop --------------------------------------------------------

    async def start(self) -> None:
        """Runs forever, reconnecting with growing backoff if the IB
        connection drops — same shape as watcher.py's start()."""
        delay = RECONNECT_BASE_DELAY
        while True:
            started = time.monotonic()
            try:
                print("[FuturesWatcher] connecting to IB...")
                await self._ensure_connected()
                await self._subscribe_all()
                print(f"[FuturesWatcher] subscribed: {', '.join(config.FUTURES_WATCHLIST)}")
                while self.ib.isConnected():
                    await asyncio.sleep(CONNECTION_POLL_SECONDS)
                print("[FuturesWatcher] IB connection dropped")
            except Exception as exc:
                print(f"[FuturesWatcher] error: {exc!r}")

            ran_for = time.monotonic() - started
            delay = RECONNECT_BASE_DELAY if ran_for >= HEALTHY_RUN_SECONDS else min(delay * 2, RECONNECT_MAX_DELAY)
            print(f"[FuturesWatcher] reconnecting in {delay}s")
            await asyncio.sleep(delay)
