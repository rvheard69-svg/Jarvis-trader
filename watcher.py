"""
The Watcher: streams live bars from Alpaca, keeps a rolling price/volume
history per symbol, computes indicators, and decides when something is
worth handing to the Analyst. No LLM calls happen in this file — triggers
fire on deterministic math so they're reproducible and debuggable.
"""
import asyncio
import time
from dataclasses import dataclass, field

import pandas as pd
from alpaca.data.live.stock import StockDataStream
from alpaca.data.enums import DataFeed

import config
from indicators import compute_rsi, compute_vwap, compute_volume_ratio, compute_sma

RECONNECT_BASE_DELAY = 5    # seconds
RECONNECT_MAX_DELAY = 300   # cap backoff at 5 minutes
HEALTHY_RUN_SECONDS = 120   # a connection that lasted this long resets the backoff


@dataclass
class Signal:
    symbol: str
    kind: str          # e.g. "rsi_overbought", "rsi_oversold", "volume_spike"
    price: float
    detail: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class Watcher:
    def __init__(self, signal_queue: asyncio.Queue):
        self.signal_queue = signal_queue
        self.bars: dict[str, pd.DataFrame] = {
            symbol: pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            for symbol in config.WATCHLIST
        }
        self._last_fired: dict[tuple[str, str], float] = {}  # (symbol, kind) -> timestamp
        self.stream: StockDataStream | None = None
        self._build_stream()

    def _build_stream(self) -> None:
        # A stream object that has errored out shouldn't be reused — build a
        # fresh one on every (re)connect attempt. Free tier only gets the IEX
        # feed; Algo Trader Plus unlocks full SIP.
        self.stream = StockDataStream(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            feed=DataFeed.IEX,
        )
        self.stream.subscribe_bars(self._on_bar, *config.WATCHLIST)

    def _on_cooldown(self, symbol: str, kind: str) -> bool:
        last = self._last_fired.get((symbol, kind))
        return last is not None and (time.time() - last) < config.SIGNAL_COOLDOWN_SECONDS

    def _mark_fired(self, symbol: str, kind: str) -> None:
        self._last_fired[(symbol, kind)] = time.time()

    async def _on_bar(self, bar) -> None:
        symbol = bar.symbol
        df = self.bars[symbol]
        df.loc[len(df)] = [bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume]
        # Keep the buffer from growing forever
        if len(df) > 500:
            df = df.tail(500).reset_index(drop=True)
        self.bars[symbol] = df

        rsi = compute_rsi(df["close"], config.RSI_PERIOD)
        vwap = compute_vwap(df, config.VWAP_LOOKBACK_BARS)
        vol_ratio = compute_volume_ratio(df, config.VOLUME_AVG_LOOKBACK)
        sma20 = compute_sma(df["close"], 20)
        price = float(bar.close)

        await self._check_triggers(symbol, price, rsi, vwap, vol_ratio, sma20)

    async def _check_triggers(self, symbol, price, rsi, vwap, vol_ratio, sma20) -> None:
        if rsi is not None and rsi >= config.RSI_OVERBOUGHT and not self._on_cooldown(symbol, "rsi_overbought"):
            self._mark_fired(symbol, "rsi_overbought")
            await self.signal_queue.put(Signal(
                symbol=symbol, kind="rsi_overbought", price=price,
                detail={"rsi": rsi, "vwap": vwap, "sma20": sma20},
            ))

        if rsi is not None and rsi <= config.RSI_OVERSOLD and not self._on_cooldown(symbol, "rsi_oversold"):
            self._mark_fired(symbol, "rsi_oversold")
            await self.signal_queue.put(Signal(
                symbol=symbol, kind="rsi_oversold", price=price,
                detail={"rsi": rsi, "vwap": vwap, "sma20": sma20},
            ))

        if vol_ratio is not None and vol_ratio >= config.VOLUME_SPIKE_MULT and not self._on_cooldown(symbol, "volume_spike"):
            self._mark_fired(symbol, "volume_spike")
            await self.signal_queue.put(Signal(
                symbol=symbol, kind="volume_spike", price=price,
                detail={"volume_ratio": vol_ratio, "rsi": rsi, "vwap": vwap},
            ))

        if vwap is not None and not self._on_cooldown(symbol, "vwap_cross"):
            prev_close = self.bars[symbol]["close"].iloc[-2] if len(self.bars[symbol]) > 1 else None
            if prev_close is not None:
                crossed_up = prev_close < vwap <= price
                crossed_down = prev_close > vwap >= price
                if crossed_up or crossed_down:
                    self._mark_fired(symbol, "vwap_cross")
                    await self.signal_queue.put(Signal(
                        symbol=symbol,
                        kind="vwap_cross_up" if crossed_up else "vwap_cross_down",
                        price=price,
                        detail={"vwap": vwap, "rsi": rsi},
                    ))

    async def start(self) -> None:
        """
        Runs the websocket connection forever, reconnecting automatically if
        it drops. Backoff grows on repeated fast failures (bad network, Alpaca
        outage) and resets once a connection has stayed healthy for a while,
        so a single bad patch doesn't leave it waiting a full 5 minutes once
        things recover.
        """
        delay = RECONNECT_BASE_DELAY
        while True:
            started = time.monotonic()
            try:
                print("[Watcher] connecting to Alpaca stream...")
                # StockDataStream.run() is blocking/synchronous under the hood,
                # so we push it onto a thread rather than awaiting it directly.
                await asyncio.to_thread(self.stream.run)
                print("[Watcher] stream closed normally.")
            except Exception as exc:
                print(f"[Watcher] stream error: {exc!r}")

            ran_for = time.monotonic() - started
            delay = RECONNECT_BASE_DELAY if ran_for >= HEALTHY_RUN_SECONDS else min(delay * 2, RECONNECT_MAX_DELAY)
            print(f"[Watcher] was connected for {ran_for:.0f}s; reconnecting in {delay}s")
            await asyncio.sleep(delay)
            self._build_stream()
