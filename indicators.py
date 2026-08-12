"""
Plain deterministic math — no LLM anywhere in this file, on purpose.
Triggers should fire on precise numbers, not on a model's vibes.
"""
import pandas as pd


def compute_rsi(closes: pd.Series, period: int = 14) -> float | None:
    """Standard Wilder RSI. Returns None until there's enough history."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_vwap(bars: pd.DataFrame, lookback: int) -> float | None:
    """Volume-weighted average price over the last `lookback` bars."""
    window = bars.tail(lookback)
    if window.empty or window["volume"].sum() == 0:
        return None
    typical_price = (window["high"] + window["low"] + window["close"]) / 3
    return round((typical_price * window["volume"]).sum() / window["volume"].sum(), 4)


def compute_volume_ratio(bars: pd.DataFrame, avg_lookback: int) -> float | None:
    """Latest bar's volume vs. the trailing average — >1 means above-normal volume."""
    if len(bars) < avg_lookback + 1:
        return None
    avg_vol = bars["volume"].iloc[-(avg_lookback + 1):-1].mean()
    if avg_vol == 0:
        return None
    return round(bars["volume"].iloc[-1] / avg_vol, 2)


def compute_sma(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(closes.tail(period).mean(), 4)
