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
RISK_CHECK_INTERVAL_SECONDS = int(os.getenv("RISK_CHECK_INTERVAL_SECONDS", "120"))
# Manual kill switch: if this file exists, the guardrail treats the day as
# halted regardless of P&L. Create it with `touch HALT` (or equivalent);
# delete it (or wait for the next trading day) to resume.
HALT_FILE = os.getenv("HALT_FILE", "HALT")
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
