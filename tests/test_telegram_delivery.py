"""Delivery errors reach the pipeline; notifications never consume commands."""

from unittest.mock import Mock

import pytest
import requests

from src import telegram_bot as bot
from src import telegram_notify as notify


@pytest.mark.parametrize("failure", ["http", "api", "network"])
def test_summary_delivery_errors_are_reported(monkeypatch, failure):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(notify, "build_message", lambda: "summary")
    response = Mock()
    response.json.return_value = {"ok": failure != "api"}
    if failure == "http":
        response.raise_for_status.side_effect = requests.HTTPError("HTTP 500")
    transport = Mock(return_value=response)
    if failure == "network":
        transport.side_effect = requests.Timeout("timeout")
    monkeypatch.setattr(requests, "post", transport)
    cards = Mock()
    monkeypatch.setattr(bot, "send_review_cards", cards)

    with pytest.raises(RuntimeError, match="delivery failed: summary"):
        notify.send()
    cards.assert_called_once()


@pytest.mark.parametrize("result", [None, {"ok": False}, {}])
def test_card_delivery_errors_reach_notification_caller(monkeypatch, result):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(bot, "CHAT_ID", "123")
    monkeypatch.setattr(bot, "get_pending_review_jobs", lambda **_kwargs: [])
    monkeypatch.setattr(bot, "send_telegram_request", lambda *_args: result)
    monkeypatch.setattr(notify, "build_message", lambda: "summary")
    response = Mock()
    response.json.return_value = {"ok": True}
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match="delivery failed: cards"):
        notify.send()


def test_successful_notification_does_not_poll_commands(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(notify, "build_message", lambda: "summary")
    response = Mock()
    response.json.return_value = {"ok": True}
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: response)
    cards, poll = Mock(), Mock()
    monkeypatch.setattr(bot, "send_review_cards", cards)
    monkeypatch.setattr(bot, "process_updates", poll)

    notify.send()

    cards.assert_called_once()
    poll.assert_not_called()


def test_api_failure_does_not_leak_bot_token(monkeypatch, capsys):
    monkeypatch.setattr(bot, "TOKEN", "private-test-token")
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError(
        "Failure https://api.telegram.org/botprivate-test-token/sendMessage"
    )
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: response)

    assert bot.send_telegram_request("sendMessage", {}) is None
    assert "private-test-token" not in capsys.readouterr().out
