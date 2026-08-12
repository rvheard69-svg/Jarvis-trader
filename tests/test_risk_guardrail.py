from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import config
import risk_guardrail as rg_module
from risk_guardrail import RiskGuardrail


def make_account(equity, last_equity, daytrade_count=0, buying_power=10000):
    return SimpleNamespace(
        equity=str(equity),
        last_equity=str(last_equity),
        daytrade_count=daytrade_count,
        buying_power=str(buying_power),
    )


def make_position(symbol, market_value):
    return SimpleNamespace(symbol=symbol, market_value=str(market_value))


@pytest.fixture
def guardrail(tmp_path, monkeypatch):
    monkeypatch.setattr(rg_module, "TradingClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(config, "HALT_FILE", str(tmp_path / "HALT"))
    monkeypatch.setattr(config, "RISK_LOG_PATH", str(tmp_path / "risk_log.jsonl"))
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 3.0)
    monkeypatch.setattr(config, "MAX_POSITION_PCT", 20.0)
    monkeypatch.setattr(config, "PDT_EQUITY_THRESHOLD", 25000.0)
    return RiskGuardrail()


def test_daily_loss_halts_and_stays_halted_through_recovery(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=9600, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []
    status = guardrail.refresh()
    assert status.halted
    assert "daily loss" in status.halt_reason

    # Equity recovers above the threshold minutes later — halt is deliberately sticky for the day.
    guardrail.trading_client.get_account.return_value = make_account(equity=9999, last_equity=10000)
    status2 = guardrail.refresh()
    assert status2.halted
    assert "daily loss" in status2.halt_reason


def test_manual_kill_switch_lifts_immediately_on_delete(guardrail, tmp_path):
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []

    halt_file = tmp_path / "HALT"
    halt_file.write_text("")
    status = guardrail.refresh()
    assert status.halted
    assert "manual kill switch" in status.halt_reason

    halt_file.unlink()
    status2 = guardrail.refresh()
    assert not status2.halted


def test_pdt_risk_triggers_under_threshold_with_3_day_trades(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=20000, last_equity=20000, daytrade_count=3)
    guardrail.trading_client.get_all_positions.return_value = []
    status = guardrail.refresh()
    assert status.pdt_risk


def test_pdt_risk_does_not_trigger_over_equity_threshold(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=30000, last_equity=30000, daytrade_count=5)
    guardrail.trading_client.get_all_positions.return_value = []
    status = guardrail.refresh()
    assert not status.pdt_risk


def test_brand_new_account_with_no_trade_history_does_not_crash(guardrail):
    # Alpaca returns daytrade_count=None for a paper account with no trades yet.
    guardrail.trading_client.get_account.return_value = make_account(equity=100000, last_equity=100000, daytrade_count=None)
    guardrail.trading_client.get_all_positions.return_value = []
    status = guardrail.refresh()
    assert status.day_trade_count == 0
    assert not status.pdt_risk


def test_oversized_position_flagged(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = [make_position("NVDA", 2500)]
    status = guardrail.refresh()
    assert len(status.oversized_positions) == 1
    assert status.oversized_positions[0]["symbol"] == "NVDA"


def test_evaluate_order_blocked_when_halted(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=9000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []
    guardrail.refresh()
    allowed, reason = guardrail.evaluate_order("AAPL", 100)
    assert not allowed
    assert "halted" in reason


def test_evaluate_order_blocked_over_position_cap(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []
    guardrail.refresh()
    allowed, reason = guardrail.evaluate_order("AAPL", 3000)  # 30% of equity, over the 20% cap
    assert not allowed
    assert "cap" in reason


def test_evaluate_order_allowed(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []
    guardrail.refresh()
    allowed, reason = guardrail.evaluate_order("AAPL", 500)
    assert allowed
