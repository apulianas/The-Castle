from __future__ import annotations

import json

from ravens_bot.state import AnnouncementState, channel_key


def test_state_persists_announced_keys(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = AnnouncementState(str(path))
    key = channel_key("transaction:1", "123")

    state.load()
    assert state.unseen(key)
    state.mark(key)

    reloaded = AnnouncementState(str(path))
    reloaded.load()
    assert not reloaded.unseen(key)


def test_load_migrates_legacy_dated_transaction_keys(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"announced": ["transaction:2026-08-08:tx1@123", "inactives:401:Raven One@123"]}),
        encoding="utf-8",
    )
    state = AnnouncementState(str(path))

    state.load()

    assert not state.unseen(channel_key("transaction:tx1", "123"))
    assert not state.unseen(channel_key("inactives:401:Raven One", "123"))


def test_undated_transaction_is_not_reannounced_after_a_restart(tmp_path) -> None:
    """NFL transactions carry no ESPN id, so their key looks like a legacy one.

    The identity falls back to "date:description", which the legacy migration
    would strip on load, leaving nothing that matches the key the bot computes
    and reposting every move on the next restart.
    """
    path = tmp_path / "state.json"
    key = channel_key("transaction:2025-08-26:Waived TE Baylor Cupp.", "123")

    state = AnnouncementState(str(path))
    state.load()
    assert state.unseen(key)
    state.mark(key)

    restarted = AnnouncementState(str(path))
    restarted.load()

    assert not restarted.unseen(key)


def test_unreadable_state_does_not_crash_startup(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    state = AnnouncementState(str(path))

    state.load()

    assert state.unseen(channel_key("transaction:tx1", "123"))
