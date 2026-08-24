"""TelegramConfirmer — extracted out of executor.py so a second execution path
(futures) doesn't duplicate this logic and drift from it. Covers the same
behaviors test_executor.py already exercises end-to-end, but directly against
the extracted class."""
from unittest.mock import MagicMock

import pytest

import config
import telegram_confirm as tc_module
from telegram_confirm import TelegramConfirmer


def json_response(payload):
    return MagicMock(json=lambda: payload, raise_for_status=lambda: None)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(config, "CONFIRMATION_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(config, "CONFIRMATION_POLL_SECONDS", 0.05)
    monkeypatch.setattr(tc_module.requests, "post", MagicMock())


async def test_not_configured_short_circuits(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM", False)
    confirmer = TelegramConfirmer()
    confirmed, reason = await confirmer.propose_and_confirm("hello")
    assert confirmed is False
    assert "never asked" in reason


async def test_yes_confirms(monkeypatch):
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),
        json_response({"result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "yes"}}]}),
    ]))
    confirmer = TelegramConfirmer()
    confirmed, reason = await confirmer.propose_and_confirm("hello")
    assert confirmed is True
    assert "yes" in reason


async def test_no_declines(monkeypatch):
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=[
        json_response({"result": []}),
        json_response({"result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "no"}}]}),
    ]))
    confirmer = TelegramConfirmer()
    confirmed, reason = await confirmer.propose_and_confirm("hello")
    assert confirmed is False
    assert "no" in reason


async def test_timeout_with_no_reply(monkeypatch):
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(return_value=json_response({"result": []})))
    confirmer = TelegramConfirmer()
    confirmed, reason = await confirmer.propose_and_confirm("hello")
    assert confirmed is False
    assert "no reply within" in reason


async def test_wrong_chat_id_is_ignored(monkeypatch):
    wrong = {"result": [{"update_id": 1, "message": {"chat": {"id": 99999}, "text": "yes"}}]}
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(
        side_effect=[json_response({"result": []})] + [json_response(wrong)] * 10
    ))
    confirmer = TelegramConfirmer()
    confirmed, reason = await confirmer.propose_and_confirm("hello")
    assert confirmed is False
    assert "no reply within" in reason


async def test_unreachable_telegram_does_not_raise(monkeypatch):
    monkeypatch.setattr(tc_module.requests, "get", MagicMock(side_effect=RuntimeError("404")))
    confirmer = TelegramConfirmer()
    confirmed, reason = await confirmer.propose_and_confirm("hello")  # must not raise
    assert confirmed is False
    assert "couldn't reach Telegram" in reason


async def test_placeholder_credentials_never_reach_out(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "your_bot_token_from_botfather")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "your_chat_id")
    monkeypatch.setattr(tc_module.requests, "get", MagicMock())
    confirmer = TelegramConfirmer()
    confirmed, reason = await confirmer.propose_and_confirm("hello")
    assert confirmed is False
    tc_module.requests.get.assert_not_called()


async def test_offset_established_once_and_reused_across_calls(monkeypatch):
    """Two proposals in a row from the same confirmer must not re-establish
    the offset the second time — that would silently re-read old history.
    Asserts on the offsets actually requested rather than a call count, since
    the polling loop's iteration count depends on timing, not on how many
    proposals were made."""
    offsets_requested = []

    def fake_get_updates(offset):
        offsets_requested.append(offset)
        return [{"update_id": 5, "message": {}}] if offset is None else []

    confirmer = TelegramConfirmer()
    monkeypatch.setattr(confirmer, "_get_updates", fake_get_updates)

    await confirmer.propose_and_confirm("first")
    await confirmer.propose_and_confirm("second")

    assert offsets_requested[0] is None  # first call establishes the offset
    # Every request after that reuses the established offset (5 + 1) — never
    # re-establishes it, across either proposal.
    assert all(o == 6 for o in offsets_requested[1:])
