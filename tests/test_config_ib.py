"""IB/futures config gates — same shape as the ALPACA_PAPER guarantee:
refuse to start rather than let a live port or an unconfigured symbol
through silently."""
import pytest

import config


def test_ib_defaults_point_at_a_paper_port():
    assert config.IB_PORT in config.IB_PAPER_PORTS


def test_futures_disabled_by_default():
    assert config.FUTURES_ENABLED is False


def test_validate_refuses_a_live_ib_port(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "k")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "IB_PORT", 7496)  # TWS LIVE
    with pytest.raises(RuntimeError, match="LIVE"):
        config.validate()


def test_validate_refuses_the_other_live_port(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "k")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "IB_PORT", 4001)  # IB Gateway LIVE
    with pytest.raises(RuntimeError, match="LIVE"):
        config.validate()


def test_validate_accepts_paper_ports(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "k")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "IB_PORT", 4002)
    config.validate()  # must not raise


def test_validate_refuses_an_unknown_futures_symbol(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "k")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "FUTURES_WATCHLIST", ["MES", "CL"])  # CL not in contract_specs
    with pytest.raises(RuntimeError, match="CL"):
        config.validate()


def test_validate_accepts_the_shipped_futures_watchlist(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "k")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    config.validate()  # must not raise on the real default FUTURES_WATCHLIST
