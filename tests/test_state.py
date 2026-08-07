from __future__ import annotations

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
