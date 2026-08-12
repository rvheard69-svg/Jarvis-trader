import pandas as pd

from indicators import compute_rsi, compute_sma, compute_volume_ratio, compute_vwap


def test_compute_rsi_returns_none_without_enough_history():
    closes = pd.Series([100.0] * 10)
    assert compute_rsi(closes, period=14) is None


def test_compute_rsi_all_gains_is_100():
    closes = pd.Series([100 + i for i in range(20)])  # strictly increasing
    assert compute_rsi(closes, period=14) == 100.0


def test_compute_rsi_all_losses_is_0():
    closes = pd.Series([100 - i for i in range(20)])  # strictly decreasing
    assert compute_rsi(closes, period=14) == 0.0


def test_compute_vwap_weights_by_volume():
    bars = pd.DataFrame({
        "high": [10, 20],
        "low": [10, 20],
        "close": [10, 20],
        "volume": [100, 300],
    })
    # typical price == close here (high=low=close): (10*100 + 20*300) / 400 = 17.5
    assert compute_vwap(bars, lookback=2) == 17.5


def test_compute_vwap_empty_or_zero_volume_is_none():
    bars = pd.DataFrame({"high": [], "low": [], "close": [], "volume": []})
    assert compute_vwap(bars, lookback=5) is None


def test_compute_volume_ratio_above_average():
    bars = pd.DataFrame({"volume": [100] * 20 + [300]})
    assert compute_volume_ratio(bars, avg_lookback=20) == 3.0


def test_compute_volume_ratio_insufficient_history_is_none():
    bars = pd.DataFrame({"volume": [100] * 5})
    assert compute_volume_ratio(bars, avg_lookback=20) is None


def test_compute_sma():
    closes = pd.Series([1, 2, 3, 4, 5])
    assert compute_sma(closes, period=5) == 3.0
