import json
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


def make_position(symbol, market_value, unrealized_plpc=0.0):
    # unrealized_plpc is a fraction, as Alpaca returns it (-0.0523 == -5.23%).
    # Defaults to flat so these tests exercise sizing rules, not the stop loss.
    return SimpleNamespace(
        symbol=symbol,
        market_value=str(market_value),
        unrealized_plpc=str(unrealized_plpc),
        unrealized_pl=str(round(market_value * unrealized_plpc, 2)),
    )


@pytest.fixture
def guardrail(tmp_path, monkeypatch):
    monkeypatch.setattr(rg_module, "TradingClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(config, "HALT_FILE", str(tmp_path / "HALT"))
    monkeypatch.setattr(config, "RISK_LOG_PATH", str(tmp_path / "risk_log.jsonl"))
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 3.0)
    monkeypatch.setattr(config, "MAX_POSITION_PCT", 20.0)
    monkeypatch.setattr(config, "PDT_EQUITY_THRESHOLD", 25000.0)
    g = RiskGuardrail()
    # Market shut by default, so these tests exercise sizing rules without the
    # time-based exits joining in.
    g.trading_client.get_clock.return_value = SimpleNamespace(is_open=False)
    return g


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


def test_portfolio_cap_blocks_order_that_would_breach_it(guardrail, monkeypatch):
    monkeypatch.setattr(config, "MAX_PORTFOLIO_PCT", 50.0)
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    # $4,800 already invested across positions each under the 20% single cap.
    guardrail.trading_client.get_all_positions.return_value = [
        make_position("AAPL", 1600), make_position("NVDA", 1600), make_position("SPY", 1600),
    ]
    guardrail.refresh()

    allowed, reason = guardrail.evaluate_order("TSLA", 500)  # -> $5,300 = 53%
    assert not allowed
    assert "portfolio cap" in reason
    assert "53" in reason  # reports the projected figure, not just the current one


def test_portfolio_cap_allows_order_that_stays_within_it(guardrail, monkeypatch):
    monkeypatch.setattr(config, "MAX_PORTFOLIO_PCT", 50.0)
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = [make_position("AAPL", 1600)]
    guardrail.refresh()

    allowed, _ = guardrail.evaluate_order("TSLA", 500)  # -> $2,100 = 21%
    assert allowed


def test_portfolio_cap_never_blocks_a_closing_sell(guardrail, monkeypatch):
    """A sell reduces exposure. Blocking it because exposure is already high
    would trap you in an oversized book — exactly backwards."""
    monkeypatch.setattr(config, "MAX_PORTFOLIO_PCT", 50.0)
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    # Already far over the portfolio cap (e.g. positions ran up in value).
    guardrail.trading_client.get_all_positions.return_value = [
        make_position("AAPL", 1900), make_position("NVDA", 1900),
        make_position("SPY", 1900), make_position("QQQ", 1900),
    ]
    guardrail.refresh()

    # executor passes notional=0.0 for a close-position order
    allowed, reason = guardrail.evaluate_order("AAPL", 0.0)
    assert allowed, f"a closing sell must never be blocked, got: {reason}"


def test_total_exposure_is_tracked(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = [
        make_position("AAPL", 1000), make_position("NVDA", 1500),
    ]
    status = guardrail.refresh()
    assert status.total_position_value == 2500.0
    assert status.total_position_pct == 25.0


def test_open_market_reports_seconds_to_close(guardrail):
    from datetime import datetime, timedelta
    now = datetime(2026, 8, 14, 15, 30)
    guardrail.trading_client.get_clock.return_value = SimpleNamespace(
        is_open=True, timestamp=now, next_close=now + timedelta(minutes=30))
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []
    assert guardrail.refresh().seconds_to_close == 1800.0


def test_closed_market_reports_none_not_zero(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []
    assert guardrail.refresh().seconds_to_close is None


def test_unusable_clock_does_not_break_the_risk_log(guardrail, tmp_path):
    """This value is written to risk_log.jsonl. Anything non-numeric coming
    back from the broker would break serialisation and take the whole risk
    check down with it."""
    guardrail.trading_client.get_clock.return_value = SimpleNamespace(
        is_open=True, timestamp=object(), next_close=object())
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []

    status = guardrail.refresh()          # must not raise
    assert status.seconds_to_close is None
    # and the log line must still be valid JSON
    written = (tmp_path / "risk_log.jsonl").read_text().strip().splitlines()[-1]
    assert json.loads(written)["seconds_to_close"] is None


def test_held_symbols_are_reported_for_the_time_exits(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = [
        make_position("AAPL", 1000), make_position("NVDA", 1500)]
    assert guardrail.refresh().held_symbols == ["AAPL", "NVDA"]


def test_evaluate_order_allowed(guardrail):
    guardrail.trading_client.get_account.return_value = make_account(equity=10000, last_equity=10000)
    guardrail.trading_client.get_all_positions.return_value = []
    guardrail.refresh()
    allowed, reason = guardrail.evaluate_order("AAPL", 500)
    assert allowed
