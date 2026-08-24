# jarvis-trader — Watcher + Analyst + Risk Guardrail + Executor

All the agents from the architecture we sketched, wired end to end: the
**Watcher** (streams live bars from Alpaca, computes indicators, decides
what's worth flagging), the **Analyst** (the one LLM-powered piece — takes a
flagged signal plus any recent news and explains it in plain English via
Claude), the **Risk Guardrail** (reads your actual Alpaca account and
decides whether everything should just stop for the day), and the
**Executor** (the only piece that ever submits an order — and only after a
narrow, deterministic rule fires, the Risk Guardrail clears it, AND you
reply "yes" in Telegram).

**Still paper money only, always.** `config.validate()` refuses to start if
`ALPACA_PAPER` isn't true, and both the Executor and Risk Guardrail talk to
Alpaca's paper endpoint exclusively. The Analyst still never recommends a
trade — the Executor's trade rule (`strategy.py`) is a completely separate,
narrow, deterministic function, not an AI decision. See "Execution" below
for exactly what it will and won't do on its own.

## Setup

1. **Get Alpaca paper keys** (free): sign up at https://app.alpaca.markets,
   go to your dashboard, and generate **Paper Trading** API keys (not live
   keys — the paper ones are what this project uses).

2. **Get an Anthropic API key**: https://console.anthropic.com → API Keys.
   Check https://platform.claude.com/docs/en/about-claude/models for the
   current model id and update `ANTHROPIC_MODEL` in your `.env` if needed —
   model ids get retired periodically.

3. **Install dependencies** (Python 3.10+):
   ```bash
   cd jarvis-trader
   python -m venv .venv
   source .venv/bin/activate   # on Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

4. **Configure**:
   ```bash
   cp .env.example .env
   ```
   Then open `.env` and fill in `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and
   `ANTHROPIC_API_KEY`. Adjust `WATCHLIST` to whatever symbols you want to
   track. Leave `ALPACA_PAPER=true` — the app refuses to start otherwise.

5. **Set up Telegram** (2 minutes — required if you want the Executor to
   ever be able to trade; skip only if you're fine with alerts-only and no
   trade proposals):
   - In Telegram, message **@BotFather** → `/newbot` → follow the prompts.
     It gives you a token — that's `TELEGRAM_BOT_TOKEN`.
   - Message your new bot anything (e.g. "hi") so it has a conversation to
     reply into.
   - Message **@userinfobot** (or **@getidsbot**) to get your numeric chat
     id — that's `TELEGRAM_CHAT_ID`.
   - Paste both into `.env`. `NOTIFY_TELEGRAM=true` and `NOTIFY_DESKTOP=true`
     are already on by default — set either to `false` if you only want one
     alert channel (this doesn't affect the Executor, which always needs
     Telegram specifically, since that's where it asks for your confirmation).

6. **Run it** (during market hours, so there's actually live data to watch):
   ```bash
   python main.py
   ```
   For actual unattended use, run it through the supervisor script instead
   — see "Keeping this running unattended" below.

## What you'll see

Nothing happens until a trigger condition is actually met — this is by
design, so you're not getting an alert every minute. When one fires, you'll
see something like:

```
[10:32:04] NVDA — volume_spike @ $187.42
------------------------------------------------------------
NVDA just traded at roughly 3x its recent average volume with RSI at 68,
approaching overbought territory. This kind of volume surge often follows
a news catalyst or a large institutional order; check the headlines below
for a plausible cause. No related headlines were found in the last check,
so this could also be options-related flow or a broader sector move.

Related headlines:
  - NVDA announces new partnership (2026-08-12T14:20:00Z)
============================================================
```

The same message goes out on every enabled channel at once: printed to the
console, popped up as a desktop notification, and sent to Telegram. Every
signal and every Analyst explanation is also appended to `signals_log.jsonl`
— this is your audit trail. Read it back before you ever trust this system
with anything beyond alerts.

## Current triggers (tune these in `.env`)

- **RSI overbought / oversold** — `RSI_OVERBOUGHT` / `RSI_OVERSOLD` (default 70 / 30)
- **Volume spike** — current bar's volume vs. trailing 20-bar average, `VOLUME_SPIKE_MULT` (default 2.5x)
- **VWAP cross** — price crossing the rolling VWAP in either direction
- **ORB fade** — a failed Opening Range Breakout: price pokes outside the
  first `ORB_WINDOW_MINUTES` (default 15) of the session's high/low range,
  then closes back inside it. Only becomes a signal (`orb_fade_buy` /
  `orb_fade_sell`) when RSI also confirms it on that same bar — RSI is the
  gate, not an independent trigger. See `orb.py`.

`SIGNAL_COOLDOWN_SECONDS` (default 600) stops the same trigger firing
repeatedly for the same symbol while a condition stays true — otherwise
you'd get paged every single minute during a strong trend.

## Risk Guardrail

This is the deterministic checkpoint between "the Analyst flagged something"
and anything happening — no LLM involved, just your actual Alpaca account
numbers, checked every `RISK_CHECK_INTERVAL_SECONDS` (default 2 minutes):

- **Daily loss halt** — if equity drops more than `MAX_DAILY_LOSS_PCT`
  (default 3%) below yesterday's close, everything halts: no more signals
  reach the Analyst, no more alerts go out, for the rest of the day. This is
  deliberately **sticky** — if equity ticks back up ten minutes later, it
  stays halted anyway, on purpose. It resets automatically at the start of
  the next trading day.
- **Manual kill switch** — create a file named `HALT` in the project folder
  (`touch HALT`) any time you want to stop everything yourself, for any
  reason. Unlike the daily-loss halt, this is **live**: delete the file and
  it lifts immediately, same day.
- **PDT risk warning** — if you're under the $25k equity threshold and have
  already made 3 day trades in the last 5 trading days, you get a one-time
  warning that a 4th risks a Pattern Day Trader restriction. This doesn't
  halt anything (it's informational — Alpaca's own account flag for this
  was deprecated, so this is computed independently from your day-trade
  count and equity).
- **Position size flags** — any open position worth more than
  `MAX_POSITION_PCT` (default 20%) of your total equity gets a one-time
  notification. Also informational.

All of this is verified with a mocked Alpaca account in `risk_guardrail.py`'s
test coverage during development — daily-loss halting and staying halted
through a recovery, the manual kill switch lifting immediately on file
delete, PDT risk triggering under $25k with 3+ day trades, and oversized
positions getting flagged.

**`evaluate_order()` now has a caller.** It was built ahead of time as the
one gate any execution logic must go through — the Executor below is that
caller. Nothing bypasses it: a proposal that fails this check never reaches
Telegram for your confirmation at all.

## Execution

This is the only part of the project that can submit an order — and it's
built to be boring on purpose. Three independent things all have to be true:

1. **A narrow, deterministic rule fires** (`strategy.py`, not an LLM):
   - RSI oversold + you don't already hold the symbol → propose a BUY sized
     at `TRADE_SIZE_PCT` of equity (default 5%, well under the Guardrail's
     20% position cap).
   - RSI overbought + you hold the symbol → propose closing that position.
   - ORB fade (`orb_fade_buy` / `orb_fade_sell`, already RSI-confirmed by
     the Watcher — see "Current triggers" above) follows the exact same two
     rules, just with a different reason string.
   - Every other signal (volume spike, VWAP cross) never reaches this rule
     at all — those stay Analyst-narrated only, same as before.
2. **The Risk Guardrail clears it** — `evaluate_order()` checks the halt
   state, the position-size cap, and PDT risk. A blocked proposal is logged
   and you get a notification saying so, but Telegram never even asks you
   to confirm it.
3. **You reply "yes" in Telegram** — a cleared proposal sends you the exact
   order (symbol, side, dollar amount or "close full position") and polls
   for your reply for `CONFIRMATION_TIMEOUT_SECONDS` (default 5 minutes).
   Reply "no" to cancel immediately, anything else (or silence) and it
   expires with nothing submitted. **This means `NOTIFY_TELEGRAM` must be
   configured for the Executor to ever do anything** — `main.py` prints a
   warning at startup if it isn't, and the Executor logs why instead of
   pretending to work.

One proposal at a time per symbol: a second signal on a symbol that's
already awaiting your confirmation is dropped (logged, not queued) rather
than piling up. Buys use Alpaca's market order endpoint sized by dollar
amount; sells use Alpaca's dedicated close-position endpoint so you're never
guessing the share count. Every outcome — no proposal, blocked, not
confirmed, submitted, or a submit failure — is appended to
`execution_log.jsonl`.

This was tested with a mocked Alpaca account and a mocked Telegram
conversation covering: a guardrail-blocked proposal never reaching Telegram,
a confirmed buy submitting with the right size and side, a confirmed sell
closing the position, an explicit "no," a timeout with no reply, a reply
from the wrong chat id being ignored, and a duplicate signal on a pending
symbol being dropped rather than double-processed.

**What this deliberately does not do:** short positions, average into an
existing position, size based on conviction, or use anything from the
Analyst's explanation to decide direction. `strategy.py` is intentionally
the smallest possible rule that exercises the whole pipeline — replace it,
not the plumbing around it, if you want different trade logic.

## Futures (IB) — experimental, off by default

A second, parallel pipeline for MES/MNQ futures via Interactive Brokers,
built the same way as the equity one but reading dollars-at-risk instead of
percent-of-notional (`contract_specs.py` / `risk_budget.py`, since a single
percentage can't size both a mini and a micro contract sensibly). Same
shape: `futures_watcher.py`'s RSI/ORB-fade signals → `futures_strategy.py`'s
rule → `risk_budget.evaluate()` → the same Telegram yes/no confirmation →
a paper order via `ib_broker.py`.

**Off by default (`FUTURES_ENABLED=false` in `.env.example`) — read this
before turning it on:**

- **Unverified against real IB.** The phase-1 spike (`spike/ib_connect.py`)
  found real-time bars and margin previews both blocked on the tested
  account (no CME futures market data subscription). Contract resolution
  worked. Everything downstream of that is unit-tested against a mocked IB
  connection, not a real one — re-run the spike and confirm bars actually
  arrive before you trust this to generate real signals.
- **Stop loss, but no time-exit yet.** `futures_risk_monitor_loop` polls IB
  positions the same way `risk_monitor_loop` polls Alpaca, and closes
  anything that has moved `FUTURES_STOP_POINTS` against its entry
  (`futures_stop_loss.py`) — no confirmation needed, same as the equity
  stop loss. There is still no futures equivalent of
  `FLATTEN_BEFORE_CLOSE_MINUTES`/`MAX_HOLD_MINUTES` — a position with no
  stop hit is held indefinitely.
- **Requires IB Gateway or TWS running** with the API enabled — see
  `spike/ib_connect.py`'s `CHECKLIST` for setup steps — on a **paper** port
  (`IB_PORT`, default 4002). `config.validate()` and `ib_broker.py` both
  refuse to start against a live port (7496/4001).

To turn it on: set `FUTURES_ENABLED=true`, confirm `IB_HOST`/`IB_PORT`
point at your paper Gateway/TWS, and start `python main.py` as usual — it
prints `Watching futures: ...` on startup when the flag is on.

## Keeping this running unattended

Two layers of resilience, since a day-trading assistant that silently dies
mid-session is worse than one that never ran:

**Websocket reconnects automatically.** If Alpaca's stream drops (network
blip, their side restarting, your laptop waking from sleep), `watcher.py`
catches it, waits with growing backoff (5s → 10s → 20s... capped at 5 min),
and reconnects with a fresh stream object. This happens inside the running
process — you don't need to do anything for this layer.

**Process-level restart for actual crashes.** No amount of internal error
handling covers everything (an unexpected exception, the process getting
killed, a dependency bug) — for that, run the app through a supervisor
instead of calling `python main.py` directly:

```bash
./scripts/run_forever.sh     # macOS / Linux / Git Bash on Windows
scripts\run_forever.bat      # native Windows
```

This restarts `main.py` whenever it exits, logs everything to `jarvis.log`,
and uses the same growing-backoff pattern so a bad patch doesn't turn into a
crash loop that burns through your Anthropic API quota.

**Starting automatically on login/boot** (optional, so you don't have to
remember to launch it each morning):
- **Linux (systemd)**: create `~/.config/systemd/user/jarvis-trader.service`
  pointing `ExecStart` at `scripts/run_forever.sh`, then
  `systemctl --user enable --now jarvis-trader`.
- **macOS (launchd)**: create a `~/Library/LaunchAgents/com.jarvis-trader.plist`
  with `ProgramArguments` pointing at `scripts/run_forever.sh`, then
  `launchctl load` it.
- **Windows (Task Scheduler)**: create a task triggered "At log on," action
  = `scripts\run_forever.bat`, "Start in" = the project folder.

## Staying up to date

Two things can go stale: the code itself, and the Python packages it depends
on. `scripts/update_all.sh` (or `.bat` on native Windows) handles both:

```bash
./scripts/update_all.sh
```

**For dependency updates**, this just works out of the box — it runs
`pip install --upgrade -r requirements.txt` against whatever's in your
virtual environment.

**For code updates**, it only works once this folder is an actual git repo
with a remote, since that's what it pulls from. One-time setup:

```bash
git init
git add .
git commit -m "Initial jarvis-trader scaffold"
# create an empty repo on GitHub (or GitLab, etc.), then:
git remote add origin <your-repo-url>
git branch -M main
git push -u origin main
```

After that, any time you (or a future round of changes from me) commit and
push updates, `update_all.sh` will pull them the next time it runs.

**To run this on a schedule** instead of remembering to do it by hand:
- **macOS/Linux**: `crontab -e`, add a weekly line like
  `0 8 * * 1 /full/path/to/jarvis-trader/scripts/update_all.sh >> /full/path/to/jarvis-trader/update.log 2>&1`
- **Windows**: Task Scheduler → new task, trigger "Weekly," action =
  `scripts\update_all.bat`.

One safety note: after `update_all.sh` pulls new code, it does **not**
automatically restart a running `run_forever.sh` process — restart it
yourself once you've glanced at what changed, rather than having code
changes silently take effect on a live process mid-session.

## Known limitations to know about before you lean on this

- **Free-tier data is IEX-only**, not the full consolidated tape — good
  enough to learn on, but volume/price can lag the "real" market slightly.
  Upgrading to Alpaca's Algo Trader Plus ($99/mo) switches this to the full
  SIP feed.
- **News fetching is best-effort.** Alpaca-py's news client import path has
  shifted between versions before; if it fails to import on your installed
  version, the Analyst still runs fine, just without headlines. Check
  `pip show alpaca-py` and their docs if you want to fix that path.
- **The Risk Guardrail reads your account, not your intentions.** It knows
  your equity, positions, and day-trade count from Alpaca — it doesn't know
  what you're planning to do next. It's a safety net for what this system
  proposes, not a substitute for your own judgment.
- **The Executor waits serially, one symbol at a time.** If two different
  symbols both trip the trade rule close together, the second one's
  confirmation prompt only starts after the first resolves (confirm,
  reject, or time out). Fine for a watchlist of a handful of symbols;
  worth knowing if you expand it a lot.
- **`strategy.py`'s rule is intentionally naive.** RSI mean-reversion with
  fixed sizing isn't a claim that this makes money — it's the smallest rule
  that exercises the full pipeline end to end. Watch what it proposes for a
  while before you'd trust a more aggressive one.

## Reasonable next steps, in order

1. Let this run for a few sessions (via `run_forever.sh`, so it survives you
   closing the terminal) and read `signals_log.jsonl` and `risk_log.jsonl` —
   does the Analyst's reasoning hold up, and do the risk thresholds
   (`MAX_DAILY_LOSS_PCT`, `MAX_POSITION_PCT`) actually match how you want to
   trade, or do they need tuning in `.env`?
2. Try the manual kill switch once on purpose (`touch HALT`, watch the alert
   arrive, then `rm HALT` and confirm it lifts) so you trust it before you
   ever need it for real.
3. Let `strategy.py` propose a few paper trades and actually confirm one
   end to end — watch it show up in your Alpaca paper dashboard — before
   you trust the pipeline with anything more aggressive.
4. Read `execution_log.jsonl` after a few days: how many proposals got
   blocked by the Guardrail vs. timed out vs. got confirmed? That ratio
   tells you whether the thresholds in `.env` actually match how you trade.
