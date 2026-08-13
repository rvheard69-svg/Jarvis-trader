"""
The Risk Guardrail: the deterministic checkpoint between "the Analyst flagged
something" and "anything happens." No LLM anywhere in this file — it reads
your actual Alpaca paper account (equity, positions, day-trade count) and
answers two separate questions:

  1. Should everything just stop for today? -> `refresh()` / `is_halted()`
     (daily loss limit, or a manual kill switch you control with a file)
  2. Would a specific trade violate a hard limit? -> `evaluate_order()`
     (position size cap, PDT risk)

This system still does not place any trades — main.py only uses #1, to
decide whether to keep sending Analyst alerts. #2 is not called by anything
yet. It exists so that whenever you DO build execution logic, there is
already one gate everything has to go through, instead of risk controls
getting bolted on after the fact.
"""
import json
import os
import time
from dataclasses import dataclass, field, asdict

from alpaca.trading.client import TradingClient

import config


@dataclass
class RiskStatus:
    ts: float
    halted: bool
    halt_reason: str | None
    equity: float
    last_equity: float
    daily_pnl: float
    daily_pnl_pct: float
    day_trade_count: int
    pdt_risk: bool
    buying_power: float
    # Sum of all open positions. MAX_POSITION_PCT bounds each position on its
    # own, which says nothing about total exposure: N positions each just
    # under the per-position cap are individually fine and collectively not.
    total_position_value: float = 0.0
    total_position_pct: float = 0.0
    oversized_positions: list[dict] = field(default_factory=list)


class RiskGuardrail:
    def __init__(self):
        self.trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)
        self._daily_loss_halted = False
        self._halt_date: str | None = None
        self._last_status: RiskStatus | None = None

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _manual_kill_switch_active(self) -> bool:
        return os.path.exists(config.HALT_FILE)

    def refresh(self) -> RiskStatus:
        """
        Pulls fresh account + position data from Alpaca and recomputes
        everything. Call this on a timer (see main.py's risk_monitor_loop).
        On an API error, keeps whatever halted/not-halted state was already
        in effect rather than guessing — a network blip here shouldn't
        silently clear a real halt.
        """
        try:
            account = self.trading_client.get_account()
            positions = self.trading_client.get_all_positions()
        except Exception as exc:
            print(f"[RiskGuardrail] failed to fetch account state: {exc!r}")
            if self._last_status is not None:
                return self._last_status
            # No prior reading at all — report unhalted-but-unknown rather
            # than crash; main.py's loop will just try again next interval.
            return RiskStatus(
                ts=time.time(), halted=False, halt_reason="unknown (account fetch failed)",
                equity=0.0, last_equity=0.0, daily_pnl=0.0, daily_pnl_pct=0.0,
                day_trade_count=0, pdt_risk=False, buying_power=0.0,
            )

        equity = float(account.equity)
        last_equity = float(account.last_equity)
        daily_pnl = equity - last_equity
        daily_pnl_pct = (daily_pnl / last_equity * 100) if last_equity else 0.0
        # A brand-new paper account with no trade history yet returns None here.
        day_trade_count = int(account.daytrade_count or 0)
        buying_power = float(account.buying_power)

        # Alpaca deprecated the `pattern_day_trader` account flag, so this is
        # computed here instead: 3 day trades already + under the PDT equity
        # threshold means the *next* one risks tripping the restriction.
        pdt_risk = equity < config.PDT_EQUITY_THRESHOLD and day_trade_count >= 3

        oversized = []
        total_position_value = 0.0
        for p in positions:
            market_value = abs(float(p.market_value))
            total_position_value += market_value
            pct_of_equity = (market_value / equity * 100) if equity else 0.0
            if pct_of_equity > config.MAX_POSITION_PCT:
                oversized.append({
                    "symbol": p.symbol,
                    "pct_of_equity": round(pct_of_equity, 1),
                    "market_value": round(market_value, 2),
                })

        # These two halt sources behave differently on purpose:
        #  - Daily loss halt is STICKY for the rest of the day. The whole
        #    point of a daily loss limit is that a brief recovery ten minutes
        #    later doesn't quietly re-enable alerts.
        #  - Manual kill switch is LIVE. You created the file on purpose;
        #    deleting it should lift the halt immediately, not tomorrow.
        today = self._today()
        if self._halt_date is not None and self._halt_date != today:
            self._daily_loss_halted = False
            self._halt_date = None

        if daily_pnl_pct <= -config.MAX_DAILY_LOSS_PCT and not self._daily_loss_halted:
            self._daily_loss_halted = True
            self._halt_date = today

        manual_halt = self._manual_kill_switch_active()

        reasons = []
        if manual_halt:
            reasons.append(f"manual kill switch active ({config.HALT_FILE} file exists)")
        if self._daily_loss_halted:
            reasons.append(f"daily loss limit hit: {daily_pnl_pct:.2f}% (limit -{config.MAX_DAILY_LOSS_PCT:.1f}%)")

        halted = bool(reasons)
        halt_reason = "; ".join(reasons) if reasons else None

        status = RiskStatus(
            ts=time.time(),
            halted=halted,
            halt_reason=halt_reason if halted else None,
            equity=equity,
            last_equity=last_equity,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            day_trade_count=day_trade_count,
            pdt_risk=pdt_risk,
            buying_power=buying_power,
            total_position_value=round(total_position_value, 2),
            total_position_pct=round((total_position_value / equity * 100) if equity else 0.0, 2),
            oversized_positions=oversized,
        )
        self._last_status = status
        self._log(status)
        return status

    def is_halted(self) -> bool:
        status = self._last_status or self.refresh()
        return status.halted

    def evaluate_order(self, symbol: str, notional: float) -> tuple[bool, str]:
        """
        The pre-trade gate for future execution logic. NOTHING calls this
        yet — main.py only ever reads is_halted(). This is here so an
        execution agent, whenever it exists, has exactly one function to
        call before submitting anything, and doesn't need to reimplement
        these checks itself.
        """
        status = self._last_status or self.refresh()
        if status.halted:
            return False, f"blocked: trading halted ({status.halt_reason})"
        pct_of_equity = (notional / status.equity * 100) if status.equity else 100.0
        if pct_of_equity > config.MAX_POSITION_PCT:
            return False, (
                f"blocked: ${notional:,.0f} on {symbol} is {pct_of_equity:.1f}% of equity, "
                f"over the {config.MAX_POSITION_PCT:.0f}% cap"
            )

        # Portfolio-level cap. Only applies to orders that ADD exposure: a
        # closing sell arrives here as notional=0 and must never be blocked —
        # refusing to let you reduce risk because you already hold too much
        # would be exactly backwards, and would trap you in an oversized book.
        if notional > 0:
            projected = status.total_position_value + notional
            projected_pct = (projected / status.equity * 100) if status.equity else 100.0
            if projected_pct > config.MAX_PORTFOLIO_PCT:
                return False, (
                    f"blocked: ${notional:,.0f} on {symbol} would take total exposure to "
                    f"${projected:,.0f} ({projected_pct:.1f}% of equity), over the "
                    f"{config.MAX_PORTFOLIO_PCT:.0f}% portfolio cap "
                    f"(currently ${status.total_position_value:,.0f} / {status.total_position_pct:.1f}%)"
                )
        if status.pdt_risk:
            return False, (
                f"blocked: {status.day_trade_count} day trades already in the last 5 days on a "
                f"sub-${config.PDT_EQUITY_THRESHOLD:,.0f} account — one more risks a PDT restriction"
            )
        return True, "allowed"

    def _log(self, status: RiskStatus) -> None:
        with open(config.RISK_LOG_PATH, "a") as f:
            f.write(json.dumps(asdict(status)) + "\n")
