import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import analyst as analyst_module
import config
from analyst import Analyst
from watcher import Signal


@pytest.fixture
def an(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_PATH", str(tmp_path / "signals_log.jsonl"))
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(analyst_module, "Anthropic", MagicMock(return_value=MagicMock()))
    a = Analyst()
    a.news_client = None  # skip news entirely; it's best-effort anyway
    return a


def text_response(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def test_successful_call_returns_and_logs_explanation(an, tmp_path):
    an.client.messages.create.return_value = text_response("NVDA spiked on volume.")
    signal = Signal(symbol="NVDA", kind="volume_spike", price=187.42, detail={"volume_ratio": 3.1})

    result = an.process(signal)

    assert result["explanation"] == "NVDA spiked on volume."
    logged = json.loads((tmp_path / "signals_log.jsonl").read_text().strip())
    assert logged["symbol"] == "NVDA"


def test_api_failure_still_alerts_and_logs(an, tmp_path):
    # e.g. exhausted credit balance, expired key, or an Anthropic outage
    an.client.messages.create.side_effect = RuntimeError("credit balance is too low")
    signal = Signal(
        symbol="SPY",
        kind="vwap_cross_up",
        price=776.24,
        detail={"vwap": 775.7746, "rsi": None},
    )

    result = an.process(signal)

    # The caller still gets a usable result rather than an exception...
    assert result["symbol"] == "SPY"
    assert result["kind"] == "vwap_cross_up"
    assert "Analyst unavailable" in result["explanation"]
    assert "SPY triggered vwap_cross_up at $776.24" in result["explanation"]
    assert "775.7746" in result["explanation"]

    # ...and the audit trail still records the signal.
    logged = json.loads((tmp_path / "signals_log.jsonl").read_text().strip())
    assert logged["symbol"] == "SPY"
    assert logged["price"] == 776.24


def test_fallback_description_omits_missing_indicators(an):
    signal = Signal(symbol="TSLA", kind="rsi_oversold", price=329.22, detail={"rsi": 22.5, "vwap": None})
    described = an._describe(signal)
    assert "RSI 22.5" in described
    assert "VWAP" not in described  # None values are skipped, not printed as "None"
