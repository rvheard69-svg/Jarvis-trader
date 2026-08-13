"""
The Analyst: the one piece of this system that actually calls an LLM.
It takes a deterministic Signal from the Watcher, adds any recent news
for that symbol, and asks Claude to explain what's happening in plain
English. It does NOT decide whether to trade and does NOT place orders —
that split is deliberate (see the README).
"""
import json
import time

from anthropic import Anthropic

import config
from watcher import Signal

try:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    _NEWS_AVAILABLE = True
except ImportError:
    # News client import paths have moved before in alpaca-py; if this fails
    # on your installed version, the Analyst still works fine without it.
    _NEWS_AVAILABLE = False

SYSTEM_PROMPT = """You are a market analysis assistant helping a retail day trader \
understand why a technical alert fired on one of their watchlist symbols.

Rules:
- Explain what the indicators mean and what commonly causes this pattern.
- If news is provided, connect it to the price action only if it plausibly explains it.
- Never tell the user to buy, sell, or hold. Describe the setup; do not recommend a trade.
- Be concise: 3-5 sentences, plain English, no jargon dumps.
- If you're not confident about the cause, say so directly instead of guessing."""


class Analyst:
    def __init__(self):
        self.client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.news_client = NewsClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY) if _NEWS_AVAILABLE else None

    def _fetch_recent_news(self, symbol: str, limit: int = 3) -> list[str]:
        if not self.news_client:
            return []
        try:
            req = NewsRequest(symbols=symbol, limit=limit)
            news = self.news_client.get_news(req)
            headlines = []
            for item in getattr(news, "data", {}).get("news", []) or []:
                headlines.append(f"{item.headline} ({item.created_at})")
            return headlines
        except Exception as exc:  # news is a nice-to-have, never worth crashing the loop
            print(f"[Analyst] news fetch failed for {symbol}: {exc}")
            return []

    def _build_prompt(self, signal: Signal, headlines: list[str]) -> str:
        lines = [
            f"Symbol: {signal.symbol}",
            f"Trigger: {signal.kind}",
            f"Price at trigger: ${signal.price:.2f}",
            f"Indicator snapshot: {json.dumps(signal.detail)}",
        ]
        if headlines:
            lines.append("Recent headlines:")
            lines.extend(f"- {h}" for h in headlines)
        else:
            lines.append("Recent headlines: none found")
        return "\n".join(lines)

    @staticmethod
    def _describe(signal: Signal) -> str:
        """
        Plain-English fallback built only from the Watcher's own numbers, for
        when Claude can't be reached. No LLM, no network — this must not fail.
        """
        parts = [f"{signal.symbol} triggered {signal.kind} at ${signal.price:.2f}."]
        detail = signal.detail
        if detail.get("rsi") is not None:
            parts.append(f"RSI {detail['rsi']}.")
        if detail.get("vwap") is not None:
            parts.append(f"VWAP {float(detail['vwap']):.4f}.")
        if detail.get("volume_ratio") is not None:
            parts.append(f"Volume {detail['volume_ratio']}x its trailing average.")
        if detail.get("sma20") is not None:
            parts.append(f"20-bar SMA {float(detail['sma20']):.4f}.")
        return " ".join(parts)

    def process(self, signal: Signal) -> dict:
        headlines = self._fetch_recent_news(signal.symbol)
        prompt = self._build_prompt(signal, headlines)

        try:
            response = self.client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            explanation = "".join(block.text for block in response.content if block.type == "text")
        except Exception as exc:
            # The signal is deterministic and already worth knowing about. An
            # Anthropic outage, an expired key, or an exhausted credit balance
            # must not swallow the alert AND the audit-log entry along with the
            # narration — degrade to the raw indicator snapshot instead.
            print(f"[Analyst] narration unavailable for {signal.symbol} {signal.kind}: {exc}")
            explanation = f"[Analyst unavailable: {type(exc).__name__}] {self._describe(signal)}"

        result = {
            "ts": time.time(),
            "symbol": signal.symbol,
            "kind": signal.kind,
            "price": signal.price,
            "detail": signal.detail,
            "headlines": headlines,
            "explanation": explanation,
        }
        self._log(result)
        return result

    def _log(self, result: dict) -> None:
        with open(config.LOG_PATH, "a") as f:
            f.write(json.dumps(result) + "\n")
