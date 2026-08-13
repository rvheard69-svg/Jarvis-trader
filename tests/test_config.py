import config


def test_placeholder_values_are_not_configured(monkeypatch):
    # Exactly what an untouched copy of .env.example looks like.
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "your_bot_token_from_botfather")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "your_chat_id")
    assert not config.telegram_configured()


def test_empty_values_are_not_configured(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "   ")
    assert not config.telegram_configured()


def test_real_values_are_configured(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "8123456789:AAH-realtokenlookalike")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123456789")
    assert config.telegram_configured()


def test_disabled_flag_wins_over_real_credentials(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", False)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "8123456789:AAH-realtokenlookalike")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123456789")
    assert not config.telegram_configured()


def test_one_placeholder_is_enough_to_disqualify(monkeypatch):
    # Half-finished setup: real token, chat id never filled in.
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "8123456789:AAH-realtokenlookalike")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "your_chat_id")
    assert not config.telegram_configured()
