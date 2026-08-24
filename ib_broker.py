"""
Deterministic IB wrapper for the futures execution path: one configured
port, no auto-probing.

spike/ib_connect.py tries every port in PORTS in sequence to answer "which
port is IB actually listening on" — that's the right question for a
one-off connectivity check, but wrong for an execution path. Auto-probing
here would mean picking up whichever server answers first, live or paper —
the one distinction this entire project protects everywhere else
(ALPACA_PAPER, config.validate()'s IB_LIVE_PORTS check). This wrapper uses
the answer config.validate() already locked in, and refuses to even
construct against a live port as a second line of defense.

ContFuture vs. Future: the spike qualifies ContFuture for market data and
front-month verification — IB does not accept orders against a ContFuture,
it's market-data-only. This wrapper resolves and trades the concrete
front-month Future contract instead, the same way the spike verified it
(earliest lastTradeDateOrContractMonth from reqContractDetailsAsync).

IMPORTANT — unverified against real IB: the phase-1 spike's real-time bars
and whatIfOrder margin checks both came back empty (no CME market data
subscription on the account — see spike/ib_connect.py checks 3-4). Contract
resolution (check 2) did work. That means the front-month resolution and
order-submission paths below have been reviewed and unit-tested against a
mocked IB object (tests/test_ib_broker.py), but never exercised end to end
against a live IB connection. Re-run spike/ib_connect.py and place one real
paper order by hand through this wrapper before trusting it with an account
that matters.

get_position_details()'s avgCost conversion is a second, separate unverified
assumption: IB's own docs and community reports say a futures position's
avgCost is reported as price x multiplier (matching the dollar notional
convention contract_specs.py already uses), not the raw index price — so
this divides back out by the multiplier to recover it. Confirm this against
a real position before trusting futures_stop_loss.py's output.
"""
from __future__ import annotations

from dataclasses import dataclass

from ib_async import IB, Future, MarketOrder, Trade

import config
import contract_specs as cs


@dataclass(frozen=True)
class PositionDetail:
    qty: float
    avg_entry_price: float  # index points — NOT dollars; see module docstring


class IBBroker:
    def __init__(self):
        if config.IB_PORT in config.IB_LIVE_PORTS:
            raise RuntimeError(
                f"IB_PORT={config.IB_PORT} is a LIVE IB port ({sorted(config.IB_LIVE_PORTS)}). "
                f"This project is paper-only — see config.validate()."
            )
        self.ib = IB()
        self._front_months: dict[str, object] = {}  # symbol -> qualified concrete Future, cached per connection

    async def _ensure_connected(self) -> None:
        if self.ib.isConnected():
            return
        self._front_months.clear()  # a fresh connection means fresh, re-verifiable contract IDs
        await self.ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID, timeout=10)

    async def _front_month(self, symbol: str):
        """The concrete, tradable front-month contract for `symbol`. Cached
        for the life of the connection — expiries don't change intraday, and
        re-resolving on every order is a network round trip for nothing."""
        if symbol in self._front_months:
            return self._front_months[symbol]

        spec = cs.SPECS[symbol]
        details = await self.ib.reqContractDetailsAsync(Future(symbol, exchange=spec.exchange))
        if not details:
            raise RuntimeError(
                f"IB returned no contract details for {symbol} — cannot resolve a tradable "
                f"front-month contract (no futures market data subscription blocks this the "
                f"same way it blocked spike/ib_connect.py's checks 3-4)"
            )
        front = min(details, key=lambda d: d.contract.lastTradeDateOrContractMonth).contract
        qualified = await self.ib.qualifyContractsAsync(front)
        if not qualified or qualified[0] is None:
            raise RuntimeError(f"IB could not qualify the front-month contract for {symbol}")

        self._front_months[symbol] = qualified[0]
        return qualified[0]

    async def get_equity(self) -> float:
        """NetLiquidation from IB's account summary — the futures-side
        equivalent of Alpaca's account.equity."""
        await self._ensure_connected()
        summary = {v.tag: v.value for v in await self.ib.accountSummaryAsync()}
        netliq = summary.get("NetLiquidation")
        if netliq is None:
            raise RuntimeError("IB account summary did not include NetLiquidation")
        return float(netliq)

    async def get_position_details(self) -> dict[str, PositionDetail]:
        """Held quantity AND average entry price (in index points) per
        known futures symbol. A position in a symbol this app doesn't know
        about — an options leg, something manually opened — is ignored; it
        isn't futures exposure risk_budget or futures_stop_loss can reason
        about. Flat entries are omitted."""
        await self._ensure_connected()
        out: dict[str, PositionDetail] = {}
        for p in self.ib.positions():
            symbol = p.contract.symbol
            spec = cs.SPECS.get(symbol)
            if spec is None or not p.position:
                continue
            entry_price = float(p.avgCost) / spec.multiplier if spec.multiplier else 0.0
            existing = out.get(symbol)
            if existing is None:
                out[symbol] = PositionDetail(qty=float(p.position), avg_entry_price=entry_price)
            else:
                # Two reports for the same symbol shouldn't happen in
                # practice; sum the quantity (matching get_positions()'s
                # prior behavior) and keep the first entry price rather than
                # silently overwrite or average two possibly-different lots.
                out[symbol] = PositionDetail(qty=existing.qty + float(p.position), avg_entry_price=existing.avg_entry_price)
        return out

    async def get_positions(self) -> dict[str, float]:
        """Held quantity per known futures symbol — a thin view over
        get_position_details() for callers that only need the quantity."""
        details = await self.get_position_details()
        return {symbol: d.qty for symbol, d in details.items()}

    async def submit_market_order(self, symbol: str, action: str, quantity: int) -> Trade:
        """Submit a market order for `quantity` contracts of `symbol`.
        `action` is "BUY" or "SELL". This does not size or gate anything —
        the caller (futures_executor.py) is responsible for having already
        passed risk_budget.evaluate() and your Telegram confirmation."""
        if action not in ("BUY", "SELL"):
            raise ValueError(f"action must be 'BUY' or 'SELL', got {action!r}")
        if quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {quantity}")

        await self._ensure_connected()
        contract = await self._front_month(symbol)
        order = MarketOrder(action, quantity)
        return self.ib.placeOrder(contract, order)

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
