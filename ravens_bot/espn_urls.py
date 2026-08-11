from __future__ import annotations

from urllib.parse import urlencode


ESPN_WEB = "https://www.espn.com/nfl"
ESPN_CDN = "https://a.espncdn.com"
# The combiner resizes on ESPN's side, so an embed thumbnail does not download a
# 500px asset and a feature image is not upscaled from a small one.
COMBINER_URL = f"{ESPN_CDN}/combiner/i"
HEADSHOT_PATH = "/i/headshots/nfl/players/full/{athlete_id}.png"
HEADSHOT_THUMBNAIL_WIDTH = 200
HEADSHOT_FEATURE_WIDTH = 520
TEAM_LOGO_URL_TEMPLATE = f"{ESPN_CDN}/i/teamlogos/nfl/500/{{slug}}.png"


def team_logo_url(slug: str | None) -> str | None:
    if not slug:
        return None
    return TEAM_LOGO_URL_TEMPLATE.format(slug=slug.strip().lower())


def headshot_url(
    athlete_id: str | int | None, width: int = HEADSHOT_THUMBNAIL_WIDTH
) -> str | None:
    """A player photo sized for an embed.

    ESPN 404s for players without a portrait rather than serving a placeholder,
    so callers still guard on the player actually being resolved.
    """
    if athlete_id is None or not str(athlete_id).strip():
        return None
    params = urlencode(
        {"img": HEADSHOT_PATH.format(athlete_id=str(athlete_id).strip()), "w": width}
    )
    return f"{COMBINER_URL}?{params}"


def player_url(athlete_id: str | int | None) -> str | None:
    if athlete_id is None or not str(athlete_id).strip():
        return None
    return f"{ESPN_WEB}/player/_/id/{str(athlete_id).strip()}"


def team_url(slug: str | None) -> str | None:
    if not slug:
        return None
    return f"{ESPN_WEB}/team/_/name/{slug.strip().lower()}"


def game_url(event_id: str | int | None) -> str | None:
    if event_id is None or not str(event_id).strip():
        return None
    return f"{ESPN_WEB}/game/_/gameId/{str(event_id).strip()}"


def standings_url(group_id: str | int) -> str:
    return f"{ESPN_WEB}/standings/_/group/{group_id}"


def transactions_url(slug: str) -> str:
    return f"{ESPN_WEB}/team/transactions/_/name/{slug.strip().lower()}"


def schedule_url(slug: str) -> str:
    return f"{ESPN_WEB}/team/schedule/_/name/{slug.strip().lower()}"


def link(text: str, url: str | None) -> str:
    """Markdown link when a destination is known, plain text otherwise."""
    return f"[{text}]({url})" if url else text
