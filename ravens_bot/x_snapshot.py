"""Public-feed validation shared by the bot and the dependency-free Edge host."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TypedDict
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_AGE_SECONDS = 120
POST_URL = re.compile(r"https://x\.com/ravens/status/(?P<id>\d+)$", re.IGNORECASE)


class PublicPost(TypedDict):
    url: str
    text: str
    created_at: str
    image_urls: list[str]


def normalize_posts(items: object, now: datetime) -> list[PublicPost]:
    if not isinstance(items, list) or not items or len(items) > 300:
        raise ValueError("X snapshot must contain between 1 and 300 posts")
    posts: list[PublicPost] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"url", "text", "created_at", "image_urls"}:
            raise ValueError("Invalid X post fields")
        url = item["url"]
        match = POST_URL.fullmatch(url) if isinstance(url, str) else None
        text = item["text"]
        images = item["image_urls"]
        if (
            match is None or not isinstance(text, str) or len(text) > 25000
            or not isinstance(images, list) or len(images) > 4
        ):
            raise ValueError("Invalid X post")
        created = datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None or created > now:
            raise ValueError("Invalid X post timestamp")
        image_urls: list[str] = []
        for image in images:
            if not isinstance(image, str) or len(image) > 2048:
                raise ValueError("Invalid X image")
            parsed = urlsplit(image)
            if parsed.scheme != "https" or parsed.netloc != "pbs.twimg.com" or not parsed.path.startswith("/media/"):
                raise ValueError("Invalid X image host")
            params = parse_qs(parsed.query)
            params["name"] = ["orig"]
            image_urls.append(urlunsplit(parsed._replace(query=urlencode(params, doseq=True))))
        posts.append(PublicPost(
            url=f"https://x.com/Ravens/status/{match['id']}",
            text=text, created_at=created.astimezone(timezone.utc).isoformat(),
            image_urls=image_urls,
        ))
    return sorted(posts, key=lambda post: post["created_at"], reverse=True)
