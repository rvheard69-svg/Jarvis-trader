import time
from unittest.mock import MagicMock

import pytest

import config
import notifier


@pytest.fixture
def result():
    return {
        "ts": time.time(),
        "symbol": "NVDA",
        "kind": "vwap_cross_up",
        "price": 225.04,
        "detail": {},
        "headlines": [],
        # Characters Claude routinely emits that cp1252 cannot encode.
        "explanation": "Price → resistance; RSI ≈ 68, volume ≥ 2x average.",
    }


def cp1252_print(sink):
    """A print() that behaves like a real cp1252 stdout: rejects non-ASCII,
    accepts the ASCII fallback."""
    def _print(*args, **kwargs):
        text = args[0] if args else ""
        try:
            str(text).encode("cp1252")
        except UnicodeEncodeError as exc:
            raise UnicodeEncodeError(*exc.args) from None
        sink.append(text)
    return _print


def test_console_encoding_failure_still_reaches_desktop(result, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_DESKTOP", True)
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", False)

    printed = []
    monkeypatch.setattr("builtins.print", cp1252_print(printed))
    desktop = MagicMock()
    monkeypatch.setattr(notifier, "_send_desktop", desktop)

    notifier.send(result)  # must not raise

    # Console degraded to ASCII rather than dying...
    assert printed and "RSI" in printed[0]
    # ...and the desktop notification still went out with the full unicode body.
    desktop.assert_called_once()
    assert "→" in desktop.call_args[0][1]


def test_desktop_failure_does_not_block_telegram(result, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_DESKTOP", True)
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", True)
    monkeypatch.setattr(notifier, "_send_desktop", MagicMock(side_effect=RuntimeError("no daemon")))
    telegram = MagicMock()
    monkeypatch.setattr(notifier, "_send_telegram", telegram)

    notifier.send(result)

    telegram.assert_called_once()


def test_unicode_body_degrades_to_ascii_rather_than_raising(result, monkeypatch, capsys):
    printed = []

    real_print = print
    calls = {"n": 0}

    def flaky_print(*args, **kwargs):
        # First call mimics cp1252 stdout; the ASCII retry must succeed.
        calls["n"] += 1
        if calls["n"] == 1:
            raise UnicodeEncodeError("charmap", "→", 0, 1, "undefined")
        printed.append(args[0] if args else "")

    monkeypatch.setattr("builtins.print", flaky_print)
    notifier._send_console("NVDA — vwap_cross_up", result["explanation"])

    assert printed, "fallback print never ran"
    assert "?" in printed[0]  # non-encodable chars replaced, not dropped
    assert "RSI" in printed[0]  # the actual content survived
