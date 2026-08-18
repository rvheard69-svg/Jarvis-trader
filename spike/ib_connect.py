"""
Phase 1 spike: does Interactive Brokers support what the port plan assumes?

Standalone on purpose — imports nothing from the app. Run it against a PAPER
gateway and it answers the questions the plan left open, in order, and stops at
the first one that fails:

  1. Can we connect, and on which port?
  2. Does ContFuture qualification resolve to a tradeable front-month Future?
  3. What interval do real-time bars actually arrive at?      (plan assumes 5s)
  4. Does whatIfOrder return usable margin numbers?           (plan assumes yes)
  5. Do account and position reads give us what the Guardrail needs?

Nothing here places an order. whatIfOrder is a margin preview that IB does not
execute — see check 4.

    python spike/ib_connect.py            # default: MES, paper port
    python spike/ib_connect.py --symbol MNQ
    python spike/ib_connect.py --seconds 90
"""
import argparse
import asyncio
import sys
import time

# Same fix main.py carries: Windows encodes stdout as cp1252, which has no
# arrows or em-dashes, so printing this file's own output raises
# UnicodeEncodeError the moment it's redirected to a file.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from ib_async import IB, ContFuture, Future, MarketOrder, util

# Ports IB listens on, in the order we try them. Paper first, deliberately:
# connecting to a live account by accident is the one outcome worth engineering
# against, and the app's whole design is paper-only.
PORTS = [
    (7497, "TWS paper"),
    (4002, "IB Gateway paper"),
    (7496, "TWS LIVE"),
    (4001, "IB Gateway LIVE"),
]

# CME equity-index contracts, with the multipliers the sizing layer will need.
# Multipliers are from CME contract specs; the spike prints what IB reports so
# the two can be compared rather than trusted.
CONTRACTS = {
    "MES": {"exchange": "CME", "expected_multiplier": 5},
    "ES":  {"exchange": "CME", "expected_multiplier": 50},
    "MNQ": {"exchange": "CME", "expected_multiplier": 2},
    "NQ":  {"exchange": "CME", "expected_multiplier": 20},
}

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(status: str, check: str, detail: str = "") -> None:
    results.append((status, check, detail))
    print(f"  [{status}] {check}" + (f" — {detail}" if detail else ""))


async def find_port(ib: IB, host: str, client_id: int) -> tuple[int, str] | None:
    for port, label in PORTS:
        try:
            await ib.connectAsync(host, port, clientId=client_id, timeout=4)
            return port, label
        except Exception:
            continue
    return None


async def main(symbol: str, host: str, client_id: int, seconds: int) -> int:
    spec = CONTRACTS[symbol]
    ib = IB()

    print(f"\n=== 1. CONNECT ({host}) ===")
    found = await find_port(ib, host, client_id)
    if not found:
        record(FAIL, "no IB port reachable", "TWS/Gateway not running, or API not enabled")
        print("\nNothing else can run without a connection. See the checklist printed below.")
        return 1
    port, label = found
    record(PASS, f"connected on {port}", label)
    if "LIVE" in label:
        record(WARN, "this is a LIVE port", "spike places no orders, but disconnect and use paper")

    accounts = ib.managedAccounts()
    record(PASS if accounts else FAIL, "managed accounts", ", ".join(accounts) or "none returned")

    try:
        # --- 2. contract resolution ---------------------------------------
        print(f"\n=== 2. CONTRACT RESOLUTION ({symbol}) ===")
        cont = ContFuture(symbol, spec["exchange"])
        qualified = await ib.qualifyContractsAsync(cont)
        if not qualified or qualified[0] is None:
            record(FAIL, "ContFuture did not qualify", "symbol/exchange wrong, or no futures data subscription")
            return 1
        cf = qualified[0]
        record(PASS, "ContFuture qualified", f"conId={cf.conId}")

        # The plan claims qualification hands back the front month. Verify by
        # resolving the concrete Future and comparing conIds.
        details = await ib.reqContractDetailsAsync(Future(symbol, exchange=spec["exchange"]))
        if details:
            months = sorted(d.contract.lastTradeDateOrContractMonth for d in details)
            front = next((d.contract for d in details
                          if d.contract.lastTradeDateOrContractMonth == months[0]), None)
            record(PASS, f"{len(details)} expiries listed", f"front={months[0]}, next={months[1] if len(months) > 1 else 'n/a'}")
            if front is not None:
                same = front.conId == cf.conId
                record(PASS if same else WARN,
                       "ContFuture maps to front month",
                       "same conId" if same else f"differs: cont={cf.conId} front={front.conId}")
        else:
            record(WARN, "no contract details", "cannot confirm front-month mapping")

        mult = int(float(cf.multiplier)) if cf.multiplier else None
        expected = spec["expected_multiplier"]
        record(PASS if mult == expected else FAIL,
               "multiplier matches CME spec",
               f"IB reports {mult}, spec says {expected}")

        # --- 3. bar interval ----------------------------------------------
        print(f"\n=== 3. REAL-TIME BARS (listening {seconds}s) ===")
        stamps: list[float] = []
        bars = ib.reqRealTimeBars(cf, 5, "TRADES", False)

        def on_bar(bar_list, has_new):
            if has_new:
                stamps.append(time.monotonic())
                b = bar_list[-1]
                print(f"    {b.time:%H:%M:%S}  O {b.open_:>10.2f}  H {b.high:>10.2f}  "
                      f"L {b.low:>10.2f}  C {b.close:>10.2f}  V {b.volume}")

        bars.updateEvent += on_bar
        await asyncio.sleep(seconds)
        ib.cancelRealTimeBars(bars)

        if len(stamps) < 2:
            record(WARN, f"only {len(stamps)} bar(s) in {seconds}s",
                   "market may be closed, or no futures data subscription")
        else:
            gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
            avg = sum(gaps) / len(gaps)
            record(PASS, f"{len(stamps)} bars, mean gap {avg:.1f}s",
                   "confirms the 5s assumption — aggregation to 1m is required"
                   if 4 <= avg <= 6 else f"UNEXPECTED interval, plan assumed 5s")

        # --- 4. margin preview --------------------------------------------
        print("\n=== 4. MARGIN VIA whatIfOrder (no order is placed) ===")
        order = MarketOrder("BUY", 1)
        order.whatIf = True
        state = await ib.whatIfOrderAsync(cf, order)
        init_m = getattr(state, "initMarginChange", None)
        maint_m = getattr(state, "maintMarginChange", None)
        if init_m not in (None, "") and str(init_m).replace("-", "").replace(".", "").isdigit():
            record(PASS, "margin returned",
                   f"1 {symbol}: init ${float(init_m):,.2f}, maint ${float(maint_m):,.2f}")
            record(PASS, "sizing can query margin at runtime", "no hardcoded margin needed")
        elif state == []:
            # whatIfOrderAsync declares -> OrderState but returns the raw
            # future's default when IB never answers. Observed when the
            # account lacks market data for the contract: IB cannot price it,
            # so it computes no margin impact and sends nothing back.
            record(FAIL, "whatIfOrder returned nothing",
                   "IB sent no OrderState — same root cause as missing bars: no market data permission")
        else:
            record(FAIL, "whatIfOrder returned no margin", f"got {state!r}"[:120])

        # --- 5. account + positions ---------------------------------------
        print("\n=== 5. ACCOUNT STATE (what the Guardrail needs) ===")
        summary = {v.tag: v.value for v in await ib.accountSummaryAsync()}
        netliq = summary.get("NetLiquidation")
        record(PASS if netliq else FAIL, "NetLiquidation (replaces equity)",
               f"${float(netliq):,.2f}" if netliq else "missing")
        for tag in ("FullInitMarginReq", "FullMaintMarginReq", "AvailableFunds"):
            val = summary.get(tag)
            record(PASS if val else WARN, tag, f"${float(val):,.2f}" if val else "not returned")

        positions = ib.positions()
        record(PASS, f"{len(positions)} open position(s)",
               ", ".join(f"{p.contract.symbol}×{p.position:g}" for p in positions) or "flat")

    finally:
        ib.disconnect()

    print("\n=== SUMMARY ===")
    for status in (FAIL, WARN, PASS):
        for s, check, detail in results:
            if s == status:
                print(f"  [{s}] {check}" + (f" — {detail}" if detail else ""))
    failures = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results)} checks, {failures} failed")
    return 1 if failures else 0


CHECKLIST = """
IB Gateway or TWS must be running before this can connect:

  1. Install IB Gateway (lighter) or TWS from interactivebrokers.com
  2. Log in with your PAPER credentials
  3. Enable the API:
       TWS      → File → Global Configuration → API → Settings
       Gateway  → Configure → Settings → API → Settings
     Tick "Enable ActiveX and Socket Clients"
     Confirm the socket port (paper: 7497 for TWS, 4002 for Gateway)
     Add 127.0.0.1 to Trusted IPs
  4. Futures market data requires a CME subscription on the account —
     without it, contracts qualify but no bars arrive.

Then re-run:  python spike/ib_connect.py
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="IB connectivity spike for the futures port")
    ap.add_argument("--symbol", default="MES", choices=sorted(CONTRACTS))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--client-id", type=int, default=99, help="keep distinct from the app's")
    ap.add_argument("--seconds", type=int, default=60, help="how long to listen for bars")
    args = ap.parse_args()

    try:
        code = util.run(main(args.symbol, args.host, args.client_id, args.seconds))
    except KeyboardInterrupt:
        code = 130
    if code:
        print(CHECKLIST)
    sys.exit(code)
