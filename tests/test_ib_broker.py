"""ib_broker.py never touches a real IB Gateway in tests — everything below
mocks the ib_async.IB object at the boundary, same style test_executor.py
uses for Alpaca's TradingClient. What these tests do NOT prove: that this
code works against a real IB connection. The phase-1 spike found real-time
market data blocked (no CME subscription), so the front-month resolution
and order-submission paths here have never been exercised end to end —
re-run spike/ib_connect.py and a real paper submit_market_order() by hand
before trusting this against an account that matters."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import ib_broker as ib_broker_module
from ib_broker import IBBroker


def fake_ib(connected=False):
    fib = MagicMock()
    fib.isConnected = MagicMock(side_effect=lambda: connected)
    fib.connectAsync = AsyncMock(side_effect=_mark_connected(fib))
    fib.accountSummaryAsync = AsyncMock(return_value=[account_value("NetLiquidation", "250258.24")])
    fib.positions = MagicMock(return_value=[])
    fib.reqContractDetailsAsync = AsyncMock(return_value=[])
    fib.qualifyContractsAsync = AsyncMock(return_value=[])
    fib.placeOrder = MagicMock()
    fib.disconnect = MagicMock()
    return fib


def _mark_connected(fib):
    async def _connect(*a, **kw):
        fib.isConnected = MagicMock(return_value=True)
    return _connect


def account_value(tag, value):
    return SimpleNamespace(tag=tag, value=value)


def position(symbol, qty, avg_cost=0.0):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), position=qty, avgCost=avg_cost)


def contract_detail(month, con_id):
    return SimpleNamespace(contract=SimpleNamespace(
        lastTradeDateOrContractMonth=month, conId=con_id, symbol="MES",
    ))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "IB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "IB_PORT", 4002)
    monkeypatch.setattr(config, "IB_CLIENT_ID", 7)


def install(monkeypatch, fib):
    monkeypatch.setattr(ib_broker_module, "IB", MagicMock(return_value=fib))


# --- construction: paper-only guarantee -------------------------------------

def test_refuses_to_construct_against_a_live_port(monkeypatch):
    monkeypatch.setattr(config, "IB_PORT", 7496)  # TWS LIVE
    with pytest.raises(RuntimeError, match="LIVE"):
        IBBroker()


def test_constructs_fine_against_a_paper_port(monkeypatch):
    fib = fake_ib()
    install(monkeypatch, fib)
    IBBroker()  # must not raise


# --- get_equity --------------------------------------------------------------

async def test_get_equity_reads_netliquidation(monkeypatch):
    fib = fake_ib()
    fib.accountSummaryAsync = AsyncMock(return_value=[
        account_value("NetLiquidation", "250258.24"),
        account_value("AvailableFunds", "100000.00"),
    ])
    install(monkeypatch, fib)
    broker = IBBroker()

    equity = await broker.get_equity()

    assert equity == 250258.24
    fib.connectAsync.assert_awaited_once()


async def test_get_equity_raises_when_netliquidation_missing(monkeypatch):
    fib = fake_ib()
    fib.accountSummaryAsync = AsyncMock(return_value=[account_value("AvailableFunds", "100000")])
    install(monkeypatch, fib)
    broker = IBBroker()

    with pytest.raises(RuntimeError, match="NetLiquidation"):
        await broker.get_equity()


async def test_does_not_reconnect_once_already_connected(monkeypatch):
    fib = fake_ib()
    install(monkeypatch, fib)
    broker = IBBroker()

    await broker.get_equity()
    await broker.get_equity()

    fib.connectAsync.assert_awaited_once()


# --- get_positions -------------------------------------------------------

async def test_get_positions_keeps_only_known_futures_symbols(monkeypatch):
    fib = fake_ib()
    fib.positions = MagicMock(return_value=[
        position("MES", 3.0),
        position("ES", -1.0),
        position("AAPL", 100.0),  # not a futures symbol this app knows — must be ignored
    ])
    install(monkeypatch, fib)
    broker = IBBroker()

    positions = await broker.get_positions()

    assert positions == {"MES": 3.0, "ES": -1.0}


async def test_get_positions_omits_flat_entries(monkeypatch):
    fib = fake_ib()
    fib.positions = MagicMock(return_value=[position("MES", 0.0)])
    install(monkeypatch, fib)
    broker = IBBroker()

    assert await broker.get_positions() == {}


# --- get_position_details: avgCost -> raw index price -----------------------

async def test_get_position_details_converts_avg_cost_by_multiplier(monkeypatch):
    fib = fake_ib()
    # IB reports avgCost as price x multiplier for futures. MES multiplier
    # is 5, so an entry at index level 7764 reports avgCost=38820.
    fib.positions = MagicMock(return_value=[position("MES", 3.0, avg_cost=5 * 7764.0)])
    install(monkeypatch, fib)
    broker = IBBroker()

    details = await broker.get_position_details()

    assert details["MES"].qty == 3.0
    assert details["MES"].avg_entry_price == 7764.0


async def test_get_position_details_converts_the_mini_too(monkeypatch):
    fib = fake_ib()
    # ES multiplier is 50.
    fib.positions = MagicMock(return_value=[position("ES", -1.0, avg_cost=50 * 7770.0)])
    install(monkeypatch, fib)
    broker = IBBroker()

    details = await broker.get_position_details()

    assert details["ES"].qty == -1.0
    assert details["ES"].avg_entry_price == 7770.0


async def test_get_position_details_ignores_unknown_symbols(monkeypatch):
    fib = fake_ib()
    fib.positions = MagicMock(return_value=[position("AAPL", 100.0, avg_cost=150.0)])
    install(monkeypatch, fib)
    broker = IBBroker()

    assert await broker.get_position_details() == {}


async def test_get_positions_still_returns_plain_quantities(monkeypatch):
    """get_positions() is now a thin view over get_position_details() —
    existing callers (futures_strategy.decide(), etc.) must see no change."""
    fib = fake_ib()
    fib.positions = MagicMock(return_value=[
        position("MES", 3.0, avg_cost=5 * 7764.0),
        position("ES", -1.0, avg_cost=50 * 7770.0),
    ])
    install(monkeypatch, fib)
    broker = IBBroker()

    assert await broker.get_positions() == {"MES": 3.0, "ES": -1.0}


# --- submit_market_order: front-month resolution -----------------------------

async def test_submit_market_order_resolves_and_caches_the_front_month(monkeypatch):
    fib = fake_ib()
    fib.reqContractDetailsAsync = AsyncMock(return_value=[
        contract_detail("20261219", con_id=2),
        contract_detail("20260919", con_id=1),  # earlier expiry -> front month
    ])
    qualified_front = SimpleNamespace(conId=1, symbol="MES")
    fib.qualifyContractsAsync = AsyncMock(return_value=[qualified_front])
    install(monkeypatch, fib)
    broker = IBBroker()

    trade1 = await broker.submit_market_order("MES", "BUY", 3)
    trade2 = await broker.submit_market_order("MES", "SELL", 2)

    assert fib.reqContractDetailsAsync.await_count == 1  # cached after first resolution
    assert fib.placeOrder.call_count == 2
    first_contract, first_order = fib.placeOrder.call_args_list[0][0]
    assert first_contract is qualified_front
    assert first_order.action == "BUY"
    assert first_order.totalQuantity == 3
    second_contract, second_order = fib.placeOrder.call_args_list[1][0]
    assert second_order.action == "SELL"
    assert second_order.totalQuantity == 2


async def test_submit_market_order_rejects_bad_action(monkeypatch):
    fib = fake_ib()
    install(monkeypatch, fib)
    broker = IBBroker()
    with pytest.raises(ValueError, match="BUY.*SELL"):
        await broker.submit_market_order("MES", "HOLD", 1)


async def test_submit_market_order_rejects_non_positive_quantity(monkeypatch):
    fib = fake_ib()
    install(monkeypatch, fib)
    broker = IBBroker()
    with pytest.raises(ValueError, match="quantity"):
        await broker.submit_market_order("MES", "BUY", 0)


async def test_submit_market_order_raises_when_no_contract_details(monkeypatch):
    fib = fake_ib()
    fib.reqContractDetailsAsync = AsyncMock(return_value=[])
    install(monkeypatch, fib)
    broker = IBBroker()
    with pytest.raises(RuntimeError, match="no contract details"):
        await broker.submit_market_order("MES", "BUY", 1)


async def test_submit_market_order_raises_when_qualification_fails(monkeypatch):
    fib = fake_ib()
    fib.reqContractDetailsAsync = AsyncMock(return_value=[contract_detail("20260919", con_id=1)])
    fib.qualifyContractsAsync = AsyncMock(return_value=[None])
    install(monkeypatch, fib)
    broker = IBBroker()
    with pytest.raises(RuntimeError, match="could not qualify"):
        await broker.submit_market_order("MES", "BUY", 1)


# --- disconnect ----------------------------------------------------------

def test_disconnect_only_when_connected(monkeypatch):
    fib = fake_ib()
    install(monkeypatch, fib)
    broker = IBBroker()

    broker.disconnect()

    fib.disconnect.assert_not_called()  # never connected in this test — nothing to tear down
