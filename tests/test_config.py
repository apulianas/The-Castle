from __future__ import annotations

import pytest

from ravens_bot.config import load_config


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("TIME_ZONE", raising=False)


def test_load_config_reads_multiple_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "123, 456,123")

    config = load_config()

    assert config.discord_channel_ids == (123, 456)


def test_load_config_rejects_short_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "10")

    with pytest.raises(ValueError, match="POLL_INTERVAL_SECONDS"):
        load_config()


def test_load_config_rejects_bad_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/webhook")

    with pytest.raises(ValueError, match="DISCORD_WEBHOOK_URL"):
        load_config()
