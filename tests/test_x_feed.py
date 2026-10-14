from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from ravens_bot.bluesky import BlueskyClient, BlueskyError, parse_game_injury, parse_roster_move
from ravens_bot.embeds import game_injury_embed, transaction_embeds
from ravens_bot.x_browser import write_snapshot
from ravens_bot.x_feed import MAX_AGE_SECONDS, MAX_SNAPSHOT_BYTES, XFeedClient, read_snapshot


NOW = datetime(2026, 10, 13, 18, tzinfo=timezone.utc)
ZONE = ZoneInfo("America/New_York")


def snapshot():
    return {
        "version": 1, "fetched_at": NOW.isoformat(),
        "posts": [{
            "url": "https://x.com/Ravens/status/123",
            "text": "We have signed WR Chris Moore.",
            "created_at": (NOW - timedelta(hours=1)).isoformat(),
            "image_urls": ["https://pbs.twimg.com/media/photo?format=jpg&name=small"],
        }],
    }


def save(tmp_path, payload=None):
    write_snapshot(tmp_path, snapshot() if payload is None else payload)
    return tmp_path / "ravens.json"


def test_reads_x_posts_and_preserves_links_and_original_images(tmp_path):
    post, = read_snapshot(save(tmp_path), NOW)
    assert post.source == "X"
    assert post.url == "https://x.com/Ravens/status/123"
    assert post.image_urls == ("https://pbs.twimg.com/media/photo?format=jpg&name=orig",)
    move = parse_roster_move(post, ZONE)
    assert move.transaction_id == "x:123"
    assert move.source_url == post.url
    embed, = transaction_embeds([move], NOW.date())
    assert "via X" in embed.footer.text
    update = parse_game_injury(replace(post, text="WR Chris Moore (knee) has been ruled out."))
    assert "via X" in game_injury_embed(update, None).footer.text


@pytest.mark.parametrize("age,valid", [(120, True), (121, False), (-30, True), (-31, False)])
def test_exact_snapshot_freshness_threshold(tmp_path, age, valid):
    payload = snapshot()
    payload["fetched_at"] = (NOW - timedelta(seconds=age)).isoformat()
    path = save(tmp_path, payload)
    if valid:
        assert read_snapshot(path, NOW)
    else:
        with pytest.raises(ValueError, match="stale"):
            read_snapshot(path, NOW)


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("error", "login required"), ("posts", []), ("posts", None),
    ("fetched_at", "bad"), ("fetched_at", "2026-10-13T18:00:00"),
])
def test_rejects_bad_snapshot(tmp_path, field, value):
    payload = snapshot()
    payload[field] = value
    with pytest.raises(ValueError):
        read_snapshot(save(tmp_path, payload), NOW)


@pytest.mark.parametrize("field,value", [
    ("url", "https://x.com/someone/status/123"),
    ("url", "https://x.com/Ravens/status/123?query=1"),
    ("text", None), ("image_urls", None), ("created_at", "yesterday"),
    ("created_at", "2026-10-13T18:00:00"),
    ("created_at", "2027-10-13T18:00:00Z"),
    ("image_urls", ["http://pbs.twimg.com/media/a"]),
    ("image_urls", ["https://evil.example/media/a"]),
    ("image_urls", ["https://pbs.twimg.com.evil.example/media/a"]),
    ("image_urls", ["https://pbs.twimg.com/profile_images/a"]),
    ("image_urls", [123]),
])
def test_rejects_bad_post(tmp_path, field, value):
    payload = snapshot()
    payload["posts"][0][field] = value
    with pytest.raises(ValueError):
        read_snapshot(save(tmp_path, payload), NOW)


def test_bounded_snapshot_read(tmp_path):
    path = tmp_path / "ravens.json"
    path.write_bytes(b" " * (MAX_SNAPSHOT_BYTES + 1))
    with pytest.raises(ValueError, match="too large"):
        read_snapshot(path, NOW)


@pytest.mark.parametrize("contents", [b"{", b"\xff"])
def test_corrupt_snapshot(tmp_path, contents):
    path = tmp_path / "ravens.json"
    path.write_bytes(contents)
    with pytest.raises(ValueError):
        read_snapshot(path, NOW)


def test_missing_snapshot_falls_back_then_recovers(tmp_path, monkeypatch, caplog):
    fallback = AsyncMock(return_value=[])
    monkeypatch.setattr(BlueskyClient, "fetch_posts", fallback)
    client = XFeedClient(None, str(tmp_path / "ravens.json"))
    assert asyncio.run(client.fetch_posts(40)) == []
    assert asyncio.run(client.fetch_posts(40)) == []
    assert fallback.await_count == 2
    assert caplog.text.count("falling back") == 1
    payload = snapshot()
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    save(tmp_path, payload)
    with caplog.at_level("INFO"):
        assert asyncio.run(client.fetch_posts(1))[0].source == "X"
    assert "recovered" in caplog.text
    assert fallback.await_count == 2


def test_stale_snapshot_uses_bluesky_and_surfaces_failure(tmp_path, monkeypatch):
    payload = snapshot()
    payload["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=MAX_AGE_SECONDS + 1)
    ).isoformat()
    client = XFeedClient(None, str(save(tmp_path, payload)))
    fallback = AsyncMock(side_effect=BlueskyError("offline"))
    monkeypatch.setattr(BlueskyClient, "fetch_posts", fallback)
    with pytest.raises(BlueskyError, match="offline"):
        asyncio.run(client.fetch_posts())


def test_atomic_snapshot_replaces_contents_without_temp_files(tmp_path):
    path = save(tmp_path)
    write_snapshot(tmp_path, {"version": 1, "error": "unavailable"})
    assert json.loads(path.read_text())["error"] == "unavailable"
    assert list(tmp_path.iterdir()) == [path]
