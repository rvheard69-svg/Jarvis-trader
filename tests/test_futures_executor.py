"""FuturesExecutor — the futures-side counterpart to Executor. Same shape:
strategy proposes, risk_budget.evaluate() gates, TelegramConfirmer asks,
then (and only then) orders go out through the broker. Everything below
mocks IBBroker and Telegram at the boundary, same style test_executor.py
uses for Alpaca — nothing here touches a real IB connection."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import config
import futures_executor as futures_executor_module
import telegram_confirm as tc_module
from contract_specs import NASDAQ100, SP500
from futures_executor import FuturesExecutor

EQUITY = 250_258.24


class FakeBroker:
    def __init__(self, equity=EQUITY, positions=None, fail_on=()):
        self.equity = equity
        self.positions = positions or {}
        self.fail_on = set(fail_on)  # symbols whose submit_market_order should raise
        self.orders: list[tuple[str, str, int]] = []

    async def get_equity(self):
        return self.equity

    async def get_positions(self):
        return dict(self.positions)

    async def submit_market_order(self, symbol, action, quantity):
        if symbol in self.fail_on:
            raise RuntimeError(f"IB rejected the order for {symbol}")
        self.orders.append((symbol, action, quantity))
        return SimpleNamespace(order=SimpleNamespace(orderId=f"order-{len(self.orders)}"))


def json_response(payload):
    return MagicMock(json=lambda: payload, raise_for_status=lambda: None)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FUTURES_EXECUTION_LOG_PATH", str(tmp_path / "futures_execution_log.jsonl"))
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(config, "CONFIRMATION_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(config, "CONFIRMATION_POLL_SECONDS", 0.05)
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.5)
    monkeypatch.setattr(config, "RISK_PER_GROUP_PCT", 1.5)
    monkeypatch.setattr(config, "RISK_TOTAL_PCT", 2.0)
    monkeypatch.setattr(config, "FUTURES_STOP_POINTS", {SP500: 20.0, NASDAQ100: 50.0})
    monkeypatch.setattr("notifier.send", MagicMock())
    monkeypatch.setattr(tc_module.requests, "post", MagicMock())


def last_log_entry(tmp_path):
    lines = (tmp_path / "futures_execution_log.jsonl").read_text().strip().splitlines()
    return json.loads(lines[-1])


def all_log_entries(tmp_path):
    lines = (tmp_path / "futures_execution_log.jsonl").read_text().strip().splitlines()
    return [json.loads(line) for line in lines]


def approve(monkeypatch):
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),
        json_response({"result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "yes"}}]}),
    ]))


def no_reply(monkeypatch):
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(return_value=json_response({"result": []})))


# --- guardrail blocking ------------------------------------------------------

async def test_blocked_by_total_cap_never_reaches_telegram(monkeypatch, tmp_path):
    """SP500 is flat (so futures_strategy itself allows the open), but
    NASDAQ100 is already loaded enough that adding SP500 risk breaches the
    TOTAL cap — the guardrail catches what strategy's own group-flat check
    cannot, since it only looks at NASDAQ100 exposure it doesn't share."""
    broker = FakeBroker(positions={"NQ": 4})  # NASDAQ100 already at $4,000 risk
    ex = FuturesExecutor(broker)

    await ex.process_signal("MES", "rsi_oversold", rsi=25)

    tc_module.requests.post.assert_not_called()
    assert broker.orders == []
    entry = last_log_entry(tmp_path)
    assert entry["outcome"] == "blocked_by_guardrail"
    assert "portfolio cap" in entry["detail"]


# --- opening -----------------------------------------------------------------

async def test_confirmed_open_submits_a_buy_per_leg(monkeypatch, tmp_path):
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)
    approve(monkeypatch)

    await ex.process_signal("MES", "rsi_oversold", rsi=25)

    # 0.5% of EQUITY = $1,251.29 risk / $100 per MES micro -> 12 micros -> 1 ES + 2 MES
    assert broker.orders == [("ES", "BUY", 1), ("MES", "BUY", 2)]
    outcomes = [e["outcome"] for e in all_log_entries(tmp_path)]
    assert outcomes == ["submitted", "submitted"]


# --- closing -------------------------------------------------------------

async def test_confirmed_close_flattens_every_leg_with_correct_direction(monkeypatch, tmp_path):
    """A long ES and a short MES in the same group — closing must BUY to
    flatten the short and SELL to flatten the long, not assume one side."""
    broker = FakeBroker(positions={"ES": 1, "MES": -2})
    ex = FuturesExecutor(broker)
    approve(monkeypatch)

    await ex.process_signal("ES", "rsi_overbought", rsi=80)

    assert broker.orders == [("ES", "SELL", 1), ("MES", "BUY", 2)]


# --- no proposal / not confirmed ---------------------------------------------

async def test_no_proposal_never_contacts_telegram(monkeypatch, tmp_path):
    broker = FakeBroker(positions={})  # nothing to close
    ex = FuturesExecutor(broker)
    monkeypatch.setattr(tc_module.requests, "get", MagicMock())

    await ex.process_signal("MES", "rsi_overbought", rsi=80)

    tc_module.requests.get.assert_not_called()
    tc_module.requests.post.assert_not_called()
    assert last_log_entry(tmp_path)["outcome"] == "no_proposal"


async def test_not_confirmed_submits_nothing(monkeypatch, tmp_path):
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)
    no_reply(monkeypatch)

    await ex.process_signal("MES", "rsi_oversold", rsi=25)

    assert broker.orders == []
    assert last_log_entry(tmp_path)["outcome"] == "not_confirmed"


# --- partial-leg failure -------------------------------------------------

async def test_one_leg_failing_does_not_stop_the_other(monkeypatch, tmp_path):
    broker = FakeBroker(positions={}, fail_on={"ES"})
    ex = FuturesExecutor(broker)
    approve(monkeypatch)

    await ex.process_signal("MES", "rsi_oversold", rsi=25)

    assert broker.orders == [("MES", "BUY", 2)]  # ES failed, MES still went through
    outcomes = {e["symbol"]: e["outcome"] for e in all_log_entries(tmp_path)}
    assert outcomes["ES"] == "submit_failed"
    assert outcomes["MES"] == "submitted"


async def test_orb_fade_buy_reaches_submission(monkeypatch, tmp_path):
    """Regression: process_signal() used to filter kind down to
    rsi_oversold/rsi_overbought before decide() ever saw it, so an
    orb_fade_buy signal — despite futures_strategy.decide() handling it —
    was silently dropped and never proposed a trade."""
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)
    approve(monkeypatch)

    await ex.process_signal("MES", "orb_fade_buy", rsi=25)

    assert broker.orders == [("ES", "BUY", 1), ("MES", "BUY", 2)]


# --- dedup ---------------------------------------------------------------

async def test_duplicate_signal_on_pending_group_is_dropped(monkeypatch, tmp_path):
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)
    no_reply(monkeypatch)

    task1 = asyncio.create_task(ex.process_signal("MES", "rsi_oversold", rsi=25))
    await asyncio.sleep(0.05)  # let task1 mark SP500 pending and start polling
    await ex.process_signal("ES", "rsi_oversold", rsi=25)  # same group (SP500), different symbol

    assert last_log_entry(tmp_path)["outcome"] == "dropped"
    await task1


# --- force_close (stop loss — the one path with no confirmation) -----------

async def test_force_close_closes_and_logs(tmp_path):
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)

    assert await ex.force_close("MES", 2, "18.4pts against, past the 20-pt stop") is True

    assert broker.orders == [("MES", "SELL", 2)]
    entry = last_log_entry(tmp_path)
    assert entry["outcome"] == "stop_loss_closed"
    assert entry["symbol"] == "MES"


async def test_force_close_never_submits_a_buy_to_open():
    """Structurally incapable of opening or increasing a position — the
    action is always SELL, never BUY."""
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)
    await ex.force_close("MES", 2, "stopped out")
    assert all(action == "SELL" for _, action, _ in broker.orders)


async def test_force_close_never_contacts_telegram(monkeypatch):
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)
    monkeypatch.setattr(tc_module.requests, "get", MagicMock())
    monkeypatch.setattr(tc_module.requests, "post", MagicMock())

    await ex.force_close("MES", 2, "stopped out")

    tc_module.requests.get.assert_not_called()
    tc_module.requests.post.assert_not_called()


async def test_force_close_defers_when_the_group_is_pending(tmp_path):
    """Closing while a confirmation is outstanding for the same underlying
    risks a double close — ES and MES share a group, so a pending MES
    proposal must also defer an ES force_close."""
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)
    ex._pending.add(SP500)

    assert await ex.force_close("ES", 1, "stopped out") is False
    assert broker.orders == []
    assert last_log_entry(tmp_path)["outcome"] == "stop_loss_deferred"


async def test_failed_force_close_is_reported_and_retryable(tmp_path):
    broker = FakeBroker(positions={}, fail_on={"MES"})
    ex = FuturesExecutor(broker)

    assert await ex.force_close("MES", 2, "stopped out") is False

    assert last_log_entry(tmp_path)["outcome"] == "stop_loss_failed"
    # the group must not stay latched in _pending, or it can never retry
    assert SP500 not in ex._pending


async def test_other_signal_kinds_are_ignored(monkeypatch, tmp_path):
    broker = FakeBroker(positions={})
    ex = FuturesExecutor(broker)
    monkeypatch.setattr(tc_module.requests, "get", MagicMock())

    await ex.process_signal("MES", "volume_spike", rsi=50)

    tc_module.requests.get.assert_not_called()
    assert broker.orders == []
