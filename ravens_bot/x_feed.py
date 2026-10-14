"""Read the host browser's public snapshot, falling back to the Bluesky mirror."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from .bluesky import BlueskyClient, BlueskyPost, FEED_LIMIT
from .x_snapshot import MAX_AGE_SECONDS, MAX_SNAPSHOT_BYTES, normalize_posts


LOGGER = logging.getLogger(__name__)
def read_snapshot(path: Path, now: datetime) -> list[BlueskyPost]:
    with path.open("rb") as source:
        raw = source.read(MAX_SNAPSHOT_BYTES + 1)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("X snapshot is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("error"):
        raise ValueError("X snapshot is unavailable or unsupported")
    fetched = datetime.fromisoformat(str(payload.get("fetched_at", "")))
    if fetched.tzinfo is None or not -30 <= (now - fetched).total_seconds() <= MAX_AGE_SECONDS:
        raise ValueError("X snapshot is stale; start or check the Windows collector")
    return [
        BlueskyPost(
            uri=post["url"], text=post["text"],
            created_at=datetime.fromisoformat(post["created_at"]),
            image_urls=tuple(post["image_urls"]), handle="Ravens",
            source="X",
        )
        for post in normalize_posts(payload.get("posts"), now)
    ]


class XFeedClient(BlueskyClient):
    """Reuse graphic/news parsers while changing only their source of posts."""

    def __init__(self, session: aiohttp.ClientSession, path: str) -> None:
        super().__init__(session)
        self._path = Path(path)
        self._unavailable = False

    async def fetch_posts(self, limit: int = FEED_LIMIT) -> list[BlueskyPost]:
        try:
            posts = await asyncio.to_thread(read_snapshot, self._path, datetime.now(timezone.utc))
        except (OSError, ValueError) as exc:
            if not self._unavailable:
                LOGGER.warning(
                    "X browser feed unavailable (%s); falling back to Bluesky. "
                    "Check the Windows collector and sign-in.", type(exc).__name__,
                )
            self._unavailable = True
            return await super().fetch_posts(limit)
        if self._unavailable:
            LOGGER.info("X browser feed recovered; using X again.")
        self._unavailable = False
        return posts[:limit]
