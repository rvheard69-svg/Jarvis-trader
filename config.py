"""
Central place for all settings. Everything is loaded from environment
variables (via a local .env file) so no keys ever get hardcoded or committed.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_list(name: str, default: str) -> list[str]:
    val = os.getenv(name, default)
    return [s.strip().upper() for s in val.split(",") if s.strip()]


# --- Alpaca ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = _get_bool("ALPACA_PAPER", True)  # always True until you deliberately change it

# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

# --- Watchlist & trigger thresholds ---
WATCHLIST = _get_list("WATCHLIST", "AAPL,NVDA,TSLA,SPY")
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30"))
VOLUME_SPIKE_MULT = float(os.getenv("VOLUME_SPIKE_MULT", "2.5"))

# How long to wait before firing the same signal type for the same symbol again.
# This is what stops the Analyst (and your notifications) from getting spammed
# every single minute while a condition stays true.
SIGNAL_COOLDOWN_SECONDS = int(os.getenv("SIGNAL_COOLDOWN_SECONDS", "600"))

# Rolling window sizes for indicator math
RSI_PERIOD = 14
VWAP_LOOKBACK_BARS = 60  # minute bars -> ~1 trading hour
VOLUME_AVG_LOOKBACK = 20

# Local audit log (every signal + every Analyst response gets appended here)
LOG_PATH = os.getenv("LOG_PATH", "signals_log.jsonl")

# --- Notifications ---
NOTIFY_DESKTOP = _get_bool("NOTIFY_DESKTOP", True)
NOTIFY_TELEGRAM = _get_bool("NOTIFY_TELEGRAM", False)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Risk Guardrail ---
# Halt everything for the day if account equity drops this many percent
# below yesterday's closing equity.
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
# Flag any single position worth more than this percent of total equity.
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "20.0"))
# Ceiling on TOTAL invested capital as a percent of equity. MAX_POSITION_PCT
# only bounds each position separately, so without this the strategy will keep
# opening new positions as more symbols go oversold. Worst case is
# TRADE_SIZE_PCT x len(WATCHLIST) — raise this if you widen the watchlist and
# want it fully investable. Closing sells are never blocked by it.
MAX_PORTFOLIO_PCT = float(os.getenv("MAX_PORTFOLIO_PCT", "50.0"))
# Below this equity, the Pattern Day Trader rule applies (max 3 day trades
# per rolling 5 trading days before restrictions kick in on the 4th).
PDT_EQUITY_THRESHOLD = float(os.getenv("PDT_EQUITY_THRESHOLD", "25000"))
# How often the guardrail re-checks account state against Alpaca.
# Also the stop-loss re-check rate, which is what sets this: a position can
# only be detected past STOP_LOSS_PCT on a poll, so this is the worst-case lag
# before one is closed. 30s costs ~4 API calls/min against Alpaca's ~200/min.
RISK_CHECK_INTERVAL_SECONDS = int(os.getenv("RISK_CHECK_INTERVAL_SECONDS", "30"))
# Manual kill switch: if this file exists, the guardrail treats the day as
# halted regardless of P&L. Create it with `touch HALT` (or equivalent);
# delete it (or wait for the next trading day) to resume.
HALT_FILE = os.getenv("HALT_FILE", "HALT")
# Advisory lock proving only one copy of the app is running. Alpaca allows a
# single websocket per account, so a second instance can't work — it just
# fights the first for the connection.
LOCK_FILE = os.getenv("LOCK_FILE", "jarvis.lock")
# Close any position down this many percent, WITHOUT waiting for confirmation.
# strategy.py only exits on RSI >= RSI_OVERBOUGHT, which a falling position
# never reaches — so without this, nothing ever cuts a loser. Set to 0 to
# disable. This is the one path that trades without your explicit yes; it can
# only ever close an existing position, never open one.
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "5.0"))

# --- Time-based exits -------------------------------------------------------
# strategy.py only sells on RSI overbought, so a position whose RSI never
# recovers is held indefinitely. The stop loss does not cover the resulting
# overnight exposure: measured from real bars, average daily range is roughly
# 4.5x the stop distance, so a typical overnight move is several times the
# level the stop defends.
#
# Close everything this many minutes before the session close, so carrying
# overnight becomes a deliberate choice rather than the default. 0 disables.
FLATTEN_BEFORE_CLOSE_MINUTES = float(os.getenv("FLATTEN_BEFORE_CLOSE_MINUTES", "15"))
# Close any position open longer than this, bounding the "RSI never came back"
# case during the session. 0 disables. Default 0: the close-flatten above
# already caps the worst exposure, and a max hold that fires mid-session cuts
# winners as readily as losers — turn it on deliberately.
MAX_HOLD_MINUTES = float(os.getenv("MAX_HOLD_MINUTES", "0"))
RISK_LOG_PATH = os.getenv("RISK_LOG_PATH", "risk_log.jsonl")

# --- Execution (paper trading only) ---
# Position size for a new BUY, as a percent of current equity. Kept well
# under MAX_POSITION_PCT on purpose so one trade never gets close to the cap.
TRADE_SIZE_PCT = float(os.getenv("TRADE_SIZE_PCT", "5.0"))
# How long a trade proposal waits for your "yes"/"no" reply before expiring
# with no order submitted.
CONFIRMATION_TIMEOUT_SECONDS = int(os.getenv("CONFIRMATION_TIMEOUT_SECONDS", "300"))
# How often to poll Telegram for your reply while waiting.
CONFIRMATION_POLL_SECONDS = int(os.getenv("CONFIRMATION_POLL_SECONDS", "5"))
EXECUTION_LOG_PATH = os.getenv("EXECUTION_LOG_PATH", "execution_log.jsonl")


def redact_secrets(text: str) -> str:
    """
    Strip credentials out of anything headed for a log or the console.

    Telegram embeds the bot token in the URL path, and requests puts the full
    URL into HTTPError — so a single failed call writes a live credential into
    jarvis.log in plaintext. Route exception text through here before printing.
    """
    for secret in (TELEGRAM_BOT_TOKEN, ALPACA_SECRET_KEY, ALPACA_API_KEY, ANTHROPIC_API_KEY):
        if secret and not _is_placeholder(secret):
            text = text.replace(secret, "<redacted>")
    return text


# --- Futures port -----------------------------------------------------------
# Not read by the equity app. These are the settings the IB port will use;
# they live here so there is one place to tune them, and so .env can override
# without a code change.

# Stop distance per underlying, in index points. Derived from real bars rather
# than chosen: 2x the median 15-minute ATR, which matches the strategy's own
# timescale (RSI over 14 one-minute bars, 10-minute signal cooldown, and a
# 13-minute observed round-trip). Measured 2026-08 from SPY/QQQ as index
# proxies — ATR taken as a percent of price, so no ETF-to-index ratio is
# assumed. Re-derive from actual MES/MNQ bars once CME data is live.
#
# The same ATR multiple is applied to both, deliberately: an earlier pair of
# hand-picked values used 2.0x on the S&P and 0.8x on the Nasdaq, which would
# have stopped Nasdaq positions out far more often for no principled reason.
FUTURES_STOP_POINTS = {
    "SP500": float(os.getenv("STOP_POINTS_SP500", "20")),        # ~2x 15-min ATR
    "NASDAQ100": float(os.getenv("STOP_POINTS_NASDAQ100", "120")),  # ~2x 15-min ATR
}

# Risk limits, as a percent of equity. Denominated in dollars-at-risk, not
# notional — see risk_budget.py for why notional cannot be the basis once
# sizing is risk-based. per_group exceeds total/2 on purpose so the total cap
# can actually fire; risk_budget.RiskLimits.unreachable_caps() checks that.
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.5"))
RISK_PER_GROUP_PCT = float(os.getenv("RISK_PER_GROUP_PCT", "1.5"))
RISK_TOTAL_PCT = float(os.getenv("RISK_TOTAL_PCT", "2.0"))

# Audit trail for futures_executor.py, parallel to EXECUTION_LOG_PATH above.
FUTURES_EXECUTION_LOG_PATH = os.getenv("FUTURES_EXECUTION_LOG_PATH", "futures_execution_log.jsonl")


# --- IB (futures port) -------------------------------------------------------
# One configured port, deliberately — see ib_broker.py for why this doesn't
# auto-probe the way spike/ib_connect.py does. Default is IB Gateway's paper
# port; override to 7497 for TWS.
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4002"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "7"))  # distinct from the spike's own default (99)
# A second, distinct client id for futures_watcher.py's own IB connection —
# IB requires each simultaneous connection from the same account to use a
# different clientId, and the watcher and futures_executor.py's IBBroker
# connect independently.
IB_WATCHER_CLIENT_ID = int(os.getenv("IB_WATCHER_CLIENT_ID", "8"))
IB_PAPER_PORTS = {7497, 4002}  # TWS paper, IB Gateway paper
IB_LIVE_PORTS = {7496, 4001}   # TWS LIVE, IB Gateway LIVE — validate() refuses these

# Futures symbols this app is allowed to trade. Must all be in
# contract_specs.SPECS — validate() checks this at startup rather than
# discovering an unknown symbol mid-session.
FUTURES_WATCHLIST = _get_list("FUTURES_WATCHLIST", "MES,MNQ")


def _is_placeholder(val: str) -> bool:
    """
    .env.example ships values like `your_bot_token_from_botfather`. Those are
    non-empty strings, so a plain truthiness check reads them as configured —
    which is how an untouched .env ends up firing doomed Telegram requests and,
    worse, convincing the Executor it has a confirmation channel it doesn't.
    """
    return not val.strip() or val.strip().lower().startswith("your_")


def telegram_configured() -> bool:
    """
    Single source of truth for 'can we actually reach Telegram right now'.
    Everything that talks to Telegram — the Notifier, the Executor's
    confirmation prompt, main.py's startup warning — must agree, or they
    disagree about whether a trade can be confirmed.
    """
    return bool(
        NOTIFY_TELEGRAM
        and not _is_placeholder(TELEGRAM_BOT_TOKEN)
        and not _is_placeholder(TELEGRAM_CHAT_ID)
    )


def validate() -> None:
    missing = [
        name
        for name, val in [
            ("ALPACA_API_KEY", ALPACA_API_KEY),
            ("ALPACA_SECRET_KEY", ALPACA_SECRET_KEY),
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ]
        if not val
    ]
    if NOTIFY_TELEGRAM and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        missing += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    if missing:
        raise RuntimeError(
            f"Missing required settings: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill these in."
        )
    if not ALPACA_PAPER:
        raise RuntimeError(
            "ALPACA_PAPER is set to false. This scaffold is analysis/alerting only "
            "and has never placed a real order — flip this back to true. If you "
            "genuinely intend to trade live later, that should be a deliberate, "
            "separate step, not a config default."
        )
    if IB_PORT in IB_LIVE_PORTS:
        raise RuntimeError(
            f"IB_PORT={IB_PORT} is a LIVE IB port ({sorted(IB_LIVE_PORTS)}). This "
            f"project is paper-only, same guarantee as ALPACA_PAPER — point IB_PORT "
            f"at a paper port instead ({sorted(IB_PAPER_PORTS)})."
        )
    import contract_specs as _cs  # deferred: contract_specs has no reason to load for the equity-only app

    unknown_futures = [s for s in FUTURES_WATCHLIST if s not in _cs.SPECS]
    if unknown_futures:
        raise RuntimeError(
            f"FUTURES_WATCHLIST names symbol(s) with no contract spec: "
            f"{unknown_futures}. Known symbols: {sorted(_cs.SPECS)}."
        )
