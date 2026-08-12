import config
import strategy
from watcher import Signal


def test_rsi_oversold_no_existing_position_proposes_buy(monkeypatch):
    monkeypatch.setattr(config, "TRADE_SIZE_PCT", 5.0)
    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 25})
    proposal = strategy.decide(signal, existing_qty=0, equity=10000)
    assert proposal is not None
    assert proposal.side == "buy"
    assert proposal.symbol == "AAPL"
    assert proposal.notional == 500.0


def test_rsi_oversold_with_existing_position_does_not_propose():
    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 25})
    assert strategy.decide(signal, existing_qty=10, equity=10000) is None


def test_rsi_overbought_with_position_proposes_sell():
    signal = Signal(symbol="NVDA", kind="rsi_overbought", price=200.0, detail={"rsi": 80})
    proposal = strategy.decide(signal, existing_qty=5, equity=10000)
    assert proposal is not None
    assert proposal.side == "sell"
    assert proposal.notional is None


def test_rsi_overbought_no_position_does_not_propose():
    signal = Signal(symbol="NVDA", kind="rsi_overbought", price=200.0, detail={"rsi": 80})
    assert strategy.decide(signal, existing_qty=0, equity=10000) is None


def test_other_signal_kinds_never_propose_a_trade():
    for kind in ("volume_spike", "vwap_cross_up", "vwap_cross_down"):
        signal = Signal(symbol="SPY", kind=kind, price=400.0, detail={})
        assert strategy.decide(signal, existing_qty=0, equity=10000) is None
