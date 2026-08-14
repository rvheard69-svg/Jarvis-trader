"""The stop loss is the only path that trades without confirmation, so these
tests focus on what it must never do as much as what it must."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import config
import executor as executor_module
import risk_guardrail as rg_module
from executor import Executor
from risk_guardrail import RiskGuardrail


def make_account(equity=100000, last_equity=100000):
    return SimpleNamespace(equity=str(equity), last_equity=str(last_equity),
                           daytrade_count=0, buying_power=str(equity * 4))


def make_position(symbol, market_value, plpc):
    """plpc as a fraction, matching Alpaca (-0.0523 == -5.23%)."""
    return SimpleNamespace(symbol=symbol, market_value=str(market_value),
                           unrealized_plpc=str(plpc),
                           unrealized_pl=str(round(market_value * plpc, 2)))


@pytest.fixture
def guardrail(tmp_path, monkeypatch):
    monkeypatch.setattr(rg_module, "TradingClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(config, "HALT_FILE", str(tmp_path / "HALT"))
    monkeypatch.setattr(config, "RISK_LOG_PATH", str(tmp_path / "risk_log.jsonl"))
    monkeypatch.setattr(config, "STOP_LOSS_PCT", 5.0)
    return RiskGuardrail()


@pytest.fixture
def ex(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_LOG_PATH", str(tmp_path / "execution_log.jsonl"))
    monkeypatch.setattr(executor_module, "TradingClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("notifier.send", MagicMock())
    return Executor(MagicMock())


def last_log(tmp_path):
    return json.loads((tmp_path / "execution_log.jsonl").read_text().strip().splitlines()[-1])


# --- detection -------------------------------------------------------------

def test_position_past_threshold_is_flagged(guardrail):
    guardrail.trading_client.get_account.return_value = make_account()
    guardrail.trading_client.get_all_positions.return_value = [
        make_position("NVDA", 4700, -0.06),   # -6%, past the 5% stop
        make_position("AAPL", 5100, +0.02),   # green
    ]
    status = guardrail.refresh()
    assert [p["symbol"] for p in status.stopped_out_positions] == ["NVDA"]
    assert status.stopped_out_positions[0]["unrealized_plpc"] == -6.0


def test_position_just_inside_threshold_is_not_flagged(guardrail):
    guardrail.trading_client.get_account.return_value = make_account()
    guardrail.trading_client.get_all_positions.return_value = [make_position("NVDA", 4760, -0.0499)]
    assert guardrail.refresh().stopped_out_positions == []


def test_exactly_at_threshold_triggers(guardrail):
    guardrail.trading_client.get_account.return_value = make_account()
    guardrail.trading_client.get_all_positions.return_value = [make_position("NVDA", 4750, -0.05)]
    assert len(guardrail.refresh().stopped_out_positions) == 1


def test_zero_threshold_disables_the_stop(guardrail, monkeypatch):
    monkeypatch.setattr(config, "STOP_LOSS_PCT", 0.0)
    guardrail.trading_client.get_account.return_value = make_account()
    guardrail.trading_client.get_all_positions.return_value = [make_position("NVDA", 2000, -0.60)]
    assert guardrail.refresh().stopped_out_positions == []


# --- execution -------------------------------------------------------------

async def test_force_close_closes_and_logs(ex, tmp_path):
    ex.trading_client.close_position.return_value = SimpleNamespace(id="ord-1")
    assert await ex.force_close("NVDA", "down 6%") is True
    ex.trading_client.close_position.assert_called_once_with("NVDA")
    assert last_log(tmp_path)["outcome"] == "stop_loss_closed"


async def test_force_close_never_submits_a_buy(ex):
    """It must be structurally incapable of opening or increasing a position."""
    ex.trading_client.close_position.return_value = SimpleNamespace(id="ord-1")
    await ex.force_close("NVDA", "down 6%")
    ex.trading_client.submit_order.assert_not_called()


async def test_force_close_never_contacts_telegram(ex, monkeypatch):
    """The whole point is that it does not wait for a reply."""
    monkeypatch.setattr(executor_module.requests, "get", MagicMock())
    monkeypatch.setattr(executor_module.requests, "post", MagicMock())
    ex.trading_client.close_position.return_value = SimpleNamespace(id="ord-1")
    await ex.force_close("NVDA", "down 6%")
    executor_module.requests.get.assert_not_called()
    executor_module.requests.post.assert_not_called()


async def test_force_close_defers_when_a_proposal_is_pending(ex, tmp_path):
    """Closing while a confirmation is outstanding risks a double close."""
    ex._pending.add("NVDA")
    assert await ex.force_close("NVDA", "down 6%") is False
    ex.trading_client.close_position.assert_not_called()
    assert last_log(tmp_path)["outcome"] == "stop_loss_deferred"


async def test_failed_close_is_reported_and_retryable(ex, tmp_path):
    ex.trading_client.close_position.side_effect = RuntimeError("market closed")
    assert await ex.force_close("NVDA", "down 6%") is False
    assert last_log(tmp_path)["outcome"] == "stop_loss_failed"
    # the symbol must not stay latched in _pending, or it can never retry
    assert "NVDA" not in ex._pending


async def test_secrets_are_redacted_from_failure_detail(ex, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "super-secret-value")
    ex.trading_client.close_position.side_effect = RuntimeError("bad key super-secret-value")
    await ex.force_close("NVDA", "down 6%")
    assert "super-secret-value" not in last_log(tmp_path)["detail"]
