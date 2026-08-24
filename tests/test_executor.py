import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderSide

import config
import executor as executor_module
import telegram_confirm as tc_module
from executor import Executor
from watcher import Signal


class FakeGuardrail:
    """Duck-types just enough of RiskGuardrail for the Executor, without touching Alpaca."""

    def __init__(self, equity=10000.0, allowed=True, reason="allowed"):
        self._last_status = SimpleNamespace(equity=equity)
        self._allowed = allowed
        self._reason = reason

    def evaluate_order(self, symbol, notional):
        return self._allowed, self._reason

    def refresh(self):
        return self._last_status


def json_response(payload):
    return MagicMock(json=lambda: payload, raise_for_status=lambda: None)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_LOG_PATH", str(tmp_path / "execution_log.jsonl"))
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(config, "CONFIRMATION_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(config, "CONFIRMATION_POLL_SECONDS", 0.05)
    monkeypatch.setattr(config, "TRADE_SIZE_PCT", 5.0)
    monkeypatch.setattr(config, "MAX_POSITION_PCT", 20.0)
    monkeypatch.setattr(executor_module, "TradingClient", MagicMock(return_value=MagicMock()))
    # Never let a real notification (esp. Telegram) escape to the network during these tests.
    monkeypatch.setattr("notifier.send", MagicMock())
    monkeypatch.setattr(tc_module.requests, "post", MagicMock())


def make_executor(guardrail, position_qty=0.0):
    ex = Executor(guardrail)
    ex._get_position_qty = lambda symbol: position_qty
    return ex


def last_log_entry(tmp_path):
    lines = (tmp_path / "execution_log.jsonl").read_text().strip().splitlines()
    return json.loads(lines[-1])


async def test_blocked_proposal_never_reaches_telegram(monkeypatch, tmp_path):
    guardrail = FakeGuardrail(allowed=False, reason="blocked: over cap")
    ex = make_executor(guardrail)

    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    await ex.process(signal)

    tc_module.requests.post.assert_not_called()
    assert last_log_entry(tmp_path)["outcome"] == "blocked_by_guardrail"


async def test_confirmed_buy_submits_with_right_size_and_side(monkeypatch, tmp_path):
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail, position_qty=0.0)
    ex.trading_client.submit_order.return_value = SimpleNamespace(id="order-123")

    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),  # establishes the starting offset
        json_response({"result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "yes"}}]}),
    ]))

    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    await ex.process(signal)

    ex.trading_client.submit_order.assert_called_once()
    order_request = ex.trading_client.submit_order.call_args[0][0]
    assert order_request.symbol == "AAPL"
    assert float(order_request.notional) == 500.0  # 5% of $10,000 equity
    assert order_request.side == OrderSide.BUY
    assert last_log_entry(tmp_path)["outcome"] == "submitted"


async def test_confirmed_sell_closes_position(monkeypatch, tmp_path):
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail, position_qty=10.0)
    ex.trading_client.close_position.return_value = SimpleNamespace(id="order-456")

    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),
        json_response({"result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "yes"}}]}),
    ]))

    signal = Signal(symbol="NVDA", kind="rsi_overbought", price=200.0, detail={"rsi": 80})
    await ex.process(signal)

    ex.trading_client.close_position.assert_called_once_with("NVDA")
    assert last_log_entry(tmp_path)["outcome"] == "submitted"


async def test_decline_reason_distinguishes_never_asked_from_timeout(monkeypatch, tmp_path):
    """The execution log is the record you read back to understand why a trade
    didn't happen — it must not report a timeout that never occurred."""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "your_bot_token_from_botfather")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "your_chat_id")

    ex = make_executor(FakeGuardrail(equity=10000.0, allowed=True))
    await ex.process(Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20}))

    entry = last_log_entry(tmp_path)
    assert entry["outcome"] == "not_confirmed"
    assert "never asked" in entry["detail"]
    assert "timeout" not in entry["detail"].lower()


async def test_timeout_reason_says_timeout(monkeypatch, tmp_path):
    ex = make_executor(FakeGuardrail(equity=10000.0, allowed=True))
    monkeypatch.setattr(tc_module.requests, "get",
                        MagicMock(return_value=json_response({"result": []})))

    await ex.process(Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20}))

    assert "no reply within" in last_log_entry(tmp_path)["detail"]


async def test_explicit_no_is_recorded_as_your_reply(monkeypatch, tmp_path):
    ex = make_executor(FakeGuardrail(equity=10000.0, allowed=True))
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),
        json_response({"result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "no"}}]}),
    ]))

    await ex.process(Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20}))

    entry = last_log_entry(tmp_path)
    assert entry["outcome"] == "not_confirmed"
    assert "you replied 'no'" in entry["detail"]


async def test_explicit_no_cancels(monkeypatch, tmp_path):
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail)

    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),
        json_response({"result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "no"}}]}),
    ]))

    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    await ex.process(signal)

    ex.trading_client.submit_order.assert_not_called()
    assert last_log_entry(tmp_path)["outcome"] == "not_confirmed"


async def test_timeout_with_no_reply(monkeypatch, tmp_path):
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail)

    monkeypatch.setattr(tc_module.requests, "get", MagicMock(
        return_value=json_response({"result": []})
    ))

    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    await ex.process(signal)

    ex.trading_client.submit_order.assert_not_called()
    assert last_log_entry(tmp_path)["outcome"] == "not_confirmed"


async def test_reply_from_wrong_chat_id_is_ignored(monkeypatch, tmp_path):
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail)

    wrong_chat_update = {"result": [{"update_id": 1, "message": {"chat": {"id": 99999}, "text": "yes"}}]}

    # First call establishes the starting offset; every poll after that keeps
    # returning the same wrong-chat-id message, which should stay ignored.
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),
    ] + [json_response(wrong_chat_update)] * 10))

    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    await ex.process(signal)

    ex.trading_client.submit_order.assert_not_called()
    assert last_log_entry(tmp_path)["outcome"] == "not_confirmed"


async def test_placeholder_credentials_block_any_proposal(monkeypatch, tmp_path):
    # An untouched .env.example: non-empty placeholder strings that used to
    # pass every truthiness check and convince the Executor it had a channel.
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "your_bot_token_from_botfather")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "your_chat_id")
    monkeypatch.setattr(tc_module.requests, "get", MagicMock())

    ex = make_executor(FakeGuardrail(equity=10000.0, allowed=True))
    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    await ex.process(signal)

    tc_module.requests.get.assert_not_called()   # never even reaches out
    tc_module.requests.post.assert_not_called()
    ex.trading_client.submit_order.assert_not_called()
    assert last_log_entry(tmp_path)["outcome"] == "not_confirmed"


async def test_unreachable_telegram_logs_not_confirmed_instead_of_raising(monkeypatch, tmp_path):
    # Valid-looking credentials, but Telegram is down / token revoked.
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail)
    monkeypatch.setattr(
        tc_module.requests,
        "get",
        MagicMock(side_effect=RuntimeError("404 Not Found")),
    )

    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    await ex.process(signal)  # must not raise

    ex.trading_client.submit_order.assert_not_called()
    assert last_log_entry(tmp_path)["outcome"] == "not_confirmed"


async def test_failed_send_short_circuits_without_polling(monkeypatch, tmp_path):
    # Offset lookup succeeds but the proposal never lands — don't sit for the
    # full timeout waiting on a reply to a message that was never delivered.
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail)
    monkeypatch.setattr(
        tc_module.requests,
        "get",
        MagicMock(return_value=json_response({"result": []})),
    )
    failed = MagicMock()
    failed.raise_for_status.side_effect = RuntimeError("401 Unauthorized")
    monkeypatch.setattr(tc_module.requests, "post", MagicMock(return_value=failed))

    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    start = asyncio.get_event_loop().time()
    await ex.process(signal)
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < config.CONFIRMATION_TIMEOUT_SECONDS  # returned early
    ex.trading_client.submit_order.assert_not_called()
    assert last_log_entry(tmp_path)["outcome"] == "not_confirmed"


async def test_orb_fade_buy_reaches_submission(monkeypatch, tmp_path):
    """Regression: process() used to filter signal.kind down to
    rsi_oversold/rsi_overbought before decide() ever saw it, so an
    orb_fade_buy signal — despite strategy.decide() handling it — was
    silently dropped and never proposed a trade."""
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail, position_qty=0.0)
    ex.trading_client.submit_order.return_value = SimpleNamespace(id="order-orb-1")

    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),
        json_response({"result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "yes"}}]}),
    ]))

    signal = Signal(symbol="AAPL", kind="orb_fade_buy", price=100.0, detail={"rsi": 25, "orb": "fade_down"})
    await ex.process(signal)

    ex.trading_client.submit_order.assert_called_once()
    assert last_log_entry(tmp_path)["outcome"] == "submitted"


async def test_duplicate_signal_on_pending_symbol_is_dropped(monkeypatch, tmp_path):
    guardrail = FakeGuardrail(equity=10000.0, allowed=True)
    ex = make_executor(guardrail)

    monkeypatch.setattr(tc_module.requests, "get", MagicMock(
        return_value=json_response({"result": []})
    ))

    signal = Signal(symbol="AAPL", kind="rsi_oversold", price=100.0, detail={"rsi": 20})
    task1 = asyncio.create_task(ex.process(signal))
    await asyncio.sleep(0.05)  # let task1 mark AAPL pending and start polling
    await ex.process(signal)  # duplicate while task1 is still awaiting confirmation

    assert last_log_entry(tmp_path)["outcome"] == "dropped"
    await task1
