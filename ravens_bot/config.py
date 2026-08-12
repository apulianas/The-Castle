from __future__ import annotations

import os
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_TIME_ZONE = "America/New_York"
STATE_FILE = "/data/state.json"
WEBHOOK_URL_RE = re.compile(
    r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/"
    r"(?P<id>[0-9]{17,20})/(?P<token>[A-Za-z0-9.\-_]{60,})$"
)


@dataclass(frozen=True)
class BotConfig:
    discord_token: str
    discord_channel_ids: tuple[int, ...]
    discord_webhook_urls: tuple[str, ...]
    poll_interval_seconds: int
    time_zone: ZoneInfo
    state_file: str = STATE_FILE
    # A second team to fall back on for live insight commands when the Ravens
    # are not playing.
    secondary_team: str | None = None

    @property
    def has_announcement_targets(self) -> bool:
        return bool(self.discord_channel_ids or self.discord_webhook_urls)


def webhook_id(url: str) -> str:
    match = WEBHOOK_URL_RE.match(url)
    return match.group("id") if match else "unknown"


def _optional_int(value: str | None, name: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _channel_ids(value: str | None) -> tuple[int, ...]:
    if value is None or not value.strip():
        return ()
    ids: list[int] = []
    for raw in value.replace(",", " ").split():
        try:
            channel_id = int(raw)
        except ValueError as exc:
            raise ValueError(
                "DISCORD_CHANNEL_ID must be a channel id, or several separated by commas"
            ) from exc
        if channel_id not in ids:
            ids.append(channel_id)
    return tuple(ids)


def _webhook_urls(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()
    urls: list[str] = []
    for raw in value.replace(",", " ").split():
        if not WEBHOOK_URL_RE.match(raw):
            raise ValueError(
                "DISCORD_WEBHOOK_URL must contain full Discord webhook URLs that look "
                "like https://discord.com/api/webhooks/<id>/<token>"
            )
        if raw not in urls:
            urls.append(raw)
    return tuple(urls)


MAX_TEAM_NAME_CHARS = 40
_TEAM_NAME_RE = re.compile(r"^[A-Za-z0-9 .'\-]+$")


def _team_name(value: str | None) -> str | None:
    """A team a person could have typed, not a free-form string."""
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > MAX_TEAM_NAME_CHARS or not _TEAM_NAME_RE.match(text):
        raise ValueError(
            "SECONDARY_TEAM must be a team name, city, or abbreviation, "
            "such as BUF or Buffalo Bills"
        )
    return text


def load_config() -> BotConfig:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise ValueError("DISCORD_TOKEN is required")

    poll_interval = _optional_int(
        os.getenv("POLL_INTERVAL_SECONDS"), "POLL_INTERVAL_SECONDS"
    )
    if poll_interval is None:
        poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
    if poll_interval < 30:
        raise ValueError("POLL_INTERVAL_SECONDS must be at least 30")

    time_zone_name = os.getenv("TIME_ZONE", DEFAULT_TIME_ZONE).strip() or DEFAULT_TIME_ZONE
    try:
        time_zone = ZoneInfo(time_zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"TIME_ZONE is not valid: {time_zone_name}") from exc

    return BotConfig(
        discord_token=token,
        discord_channel_ids=_channel_ids(os.getenv("DISCORD_CHANNEL_ID")),
        discord_webhook_urls=_webhook_urls(os.getenv("DISCORD_WEBHOOK_URL")),
        poll_interval_seconds=poll_interval,
        time_zone=time_zone,
        secondary_team=_team_name(os.getenv("SECONDARY_TEAM")),
    )
