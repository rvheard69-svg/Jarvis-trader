"""A failed Telegram call must never write a live credential into the log."""
import config


TOKEN = "8444227523:AAFexampletokenvaluenotarealsecret000"


def test_bot_token_is_redacted_from_error_text(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    # Exactly the shape requests produces: the token sits in the URL path.
    raw = (
        "HTTPError('400 Client Error: Bad Request for url: "
        f"https://api.telegram.org/bot{TOKEN}/sendMessage')"
    )
    cleaned = config.redact_secrets(raw)
    assert TOKEN not in cleaned
    assert "<redacted>" in cleaned
    assert "400 Client Error" in cleaned  # the diagnostic value survives


def test_alpaca_and_anthropic_secrets_are_redacted(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "alpaca-secret-abc123")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-fake-key-xyz")
    text = "boom: alpaca-secret-abc123 and sk-ant-fake-key-xyz leaked"
    cleaned = config.redact_secrets(text)
    assert "alpaca-secret-abc123" not in cleaned
    assert "sk-ant-fake-key-xyz" not in cleaned


def test_placeholder_values_are_not_used_as_redaction_targets(monkeypatch):
    # Redacting on a placeholder would blank out unrelated text containing it.
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "your_bot_token_from_botfather")
    text = "set your_bot_token_from_botfather in .env"
    assert config.redact_secrets(text) == text


def test_empty_secrets_do_not_blank_everything(monkeypatch):
    # A naive str.replace("", "<redacted>") would corrupt every character.
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "")
    monkeypatch.setattr(config, "ALPACA_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert config.redact_secrets("nothing to hide") == "nothing to hide"
