"""
Wires the Watcher, Analyst, Risk Guardrail, and Executor together:

  Alpaca websocket -> Watcher (indicators, triggers) -> queue -> dispatch()
                                                                    |    |
                                                       +------------+    +------------+
                                                       v                              v
                                        Analyst (Claude call) -> Notifier    Executor (strategy.py's
                                        [suppressed while halted]             rule -> Risk Guardrail
                                                                              gate -> your Telegram
                                                                              confirmation -> paper
                                                                              order via Alpaca)

Every signal is dispatched to BOTH paths concurrently: the Analyst narrates
everything, the Executor only acts on rsi_oversold/rsi_overbought (see
strategy.py) and only after the Risk Guardrail allows it AND you reply "yes"
in Telegram. This process only ever touches Alpaca's PAPER endpoint — see
README.md for what that guarantees and what it doesn't.
"""
import asyncio
import sys
import time

# Claude's explanations routinely contain characters outside the Windows
# console default (cp1252) — arrows, math symbols, typographic marks. When
# run_forever redirects stdout to jarvis.log, Python encodes with the locale
# codec and a single such character raises UnicodeEncodeError mid-alert.
# Force UTF-8 before anything can print.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import config
from watcher import Watcher
from analyst import Analyst
from executor import Executor
from notifier import send as notify
from risk_guardrail import RiskGuardrail


async def dispatch(queue: asyncio.Queue, analyst: Analyst, executor: Executor, guardrail: RiskGuardrail) -> None:
    """
    Fans each signal out to the Analyst and the Executor as independent
    background tasks, so a slow Telegram confirmation on one symbol never
    blocks the Analyst from explaining the next signal, or the Watcher from
    picking up new bars.
    """
    while True:
        signal = await queue.get()
        asyncio.create_task(_handle_analysis(signal, analyst, guardrail))
        asyncio.create_task(_handle_execution(signal, executor))


async def _handle_analysis(signal, analyst: Analyst, guardrail: RiskGuardrail) -> None:
    # guardrail.is_halted() falls back to a synchronous refresh() (blocking
    # HTTPS calls to Alpaca) the first time it's called before
    # risk_monitor_loop has completed its first refresh — run it off the
    # event loop so that fallback never stalls the Watcher/Executor.
    halted = await asyncio.to_thread(guardrail.is_halted)
    if halted:
        print(f"[main] suppressed analysis for {signal.symbol} {signal.kind} — trading halted for today")
        return
    try:
        # Anthropic's SDK call is synchronous; run it off the event loop
        # so it doesn't block the Watcher's incoming bar stream.
        result = await asyncio.to_thread(analyst.process, signal)
        notify(result)
    except Exception as exc:
        print(f"[Analyst] failed to process signal {signal}: {exc}")


async def _handle_execution(signal, executor: Executor) -> None:
    try:
        await executor.process(signal)
    except Exception as exc:
        print(f"[Executor] failed to process signal {signal}: {exc}")


async def risk_monitor_loop(guardrail: RiskGuardrail) -> None:
    """
    Polls your actual Alpaca account on a timer and pushes a notification
    the moment anything crosses a line — a fresh halt, approaching the PDT
    limit, or a position that's grown past the size cap. Repeats-once-per-
    day-per-condition, so you get told, not paged on every single check.
    """
    notified_halt = False
    notified_pdt = False
    notified_oversized: set[str] = set()
    last_date = None

    while True:
        status = await asyncio.to_thread(guardrail.refresh)

        today = time.strftime("%Y-%m-%d")
        if today != last_date:
            notified_halt = notified_pdt = False
            notified_oversized.clear()
            last_date = today

        if status.halted and not notified_halt:
            notified_halt = True
            _notify_risk_event(
                f"TRADING HALTED — {status.halt_reason}",
                f"Equity ${status.equity:,.2f} (day P&L {status.daily_pnl_pct:+.2f}%). "
                f"New alerts and trade proposals are suppressed. A manual kill-switch halt "
                f"lifts as soon as you delete the {config.HALT_FILE} file; a daily-loss halt "
                f"resets automatically at the start of the next trading day.",
            )
        elif not status.halted and notified_halt:
            notified_halt = False  # halt cleared (e.g. manual file removed) — allow re-alerting if it happens again

        if status.pdt_risk and not notified_pdt:
            notified_pdt = True
            _notify_risk_event(
                "PDT RISK",
                f"{status.day_trade_count} day trades in the last 5 trading days on a "
                f"${status.equity:,.0f} account (PDT applies under ${config.PDT_EQUITY_THRESHOLD:,.0f}). "
                f"One more day trade risks a Pattern Day Trader restriction.",
            )

        for pos in status.oversized_positions:
            if pos["symbol"] not in notified_oversized:
                notified_oversized.add(pos["symbol"])
                _notify_risk_event(
                    f"POSITION SIZE — {pos['symbol']}",
                    f"${pos['market_value']:,.2f} is {pos['pct_of_equity']:.1f}% of equity, "
                    f"over the {config.MAX_POSITION_PCT:.0f}% cap.",
                )

        await asyncio.sleep(config.RISK_CHECK_INTERVAL_SECONDS)


def _notify_risk_event(title: str, body: str) -> None:
    # Reuses the same desktop/Telegram channels as trade alerts, formatted
    # to match what notifier.send() expects.
    notify({
        "ts": time.time(),
        "symbol": "ACCOUNT",
        "kind": title,
        "price": 0.0,
        "detail": {},
        "headlines": [],
        "explanation": body,
    })


async def main() -> None:
    config.validate()
    print(f"Watching: {', '.join(config.WATCHLIST)}  (paper trading: {config.ALPACA_PAPER})")
    if not config.telegram_configured():
        print("[main] WARNING: Telegram isn't fully configured — the Executor can never get a "
              "confirmation, so no trade will ever be submitted, even if the strategy fires. "
              "Analysis and desktop alerts still work.")

    queue: asyncio.Queue = asyncio.Queue()
    watcher = Watcher(queue)
    analyst = Analyst()
    guardrail = RiskGuardrail()
    executor = Executor(guardrail)

    await asyncio.gather(
        watcher.start(),
        dispatch(queue, analyst, executor, guardrail),
        risk_monitor_loop(guardrail),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[main] stopped by user.")
    # Any other exception is intentionally left to propagate and crash the
    # process with a nonzero exit code — scripts/run_forever.sh (or an
    # OS-level supervisor) is what restarts it. Swallowing errors here would
    # hide real problems instead of recovering from them.
