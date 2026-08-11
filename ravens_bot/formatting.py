from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from .espn_urls import link
from .models import (
    RAVENS_NAME,
    Game,
    InactivePlayer,
    Standing,
    Transaction,
)


NO_STAT = "—"
# ESPN sends "-" for a leader with no deficit; that should read as even, not as
# missing data.
NO_GAMES_BACK = {"-", "–", "—", "+0.0", "0.0", "0"}
# Beyond this many names a description is a mass roster cut, where linking each
# one costs more room than it is worth.
MAX_LINKED_PLAYERS = 6


def format_long_date(value: date) -> str:
    """Month day, year, without strftime padding flags that only exist on glibc."""
    return f"{value:%B} {value.day}, {value.year}"


def format_full_date(value: date) -> str:
    return f"{value:%A}, {format_long_date(value)}"


def format_game_time(game: Game, time_zone: ZoneInfo) -> str:
    if game.start_time is None:
        return "Time TBA"
    moment = game.start_time.astimezone(time_zone)
    hour = moment.hour % 12 or 12
    zone = moment.tzname()
    stamp = f"{moment:%a}, {moment:%b} {moment.day} at {hour}:{moment:%M} {moment:%p}"
    return f"{stamp} {zone}" if zone else stamp


def format_kickoff(game: Game, time_zone: ZoneInfo) -> str:
    """Kickoff plus the broadcaster, which is what people actually need."""
    parts = [format_game_time(game, time_zone)]
    if game.broadcast:
        parts.append(f"📺 {game.broadcast}")
    return " • ".join(parts)


def format_matchup(game: Game) -> str:
    """The matchup line, with the Ravens' side stated from their point of view."""
    ravens = game.ravens
    opponent = game.opponent
    if ravens is None or opponent is None:
        return game.name
    location = "vs" if ravens.is_home else "at"
    return f"{RAVENS_NAME} {location} {opponent.team.name}"


def format_score(game: Game) -> str | None:
    """The result from the Ravens' side, e.g. "W 23-10"."""
    ravens = game.ravens
    opponent = game.opponent
    if ravens is None or opponent is None:
        return None
    if ravens.score is None or opponent.score is None:
        return None
    verb = "W" if ravens.is_winner else ("L" if opponent.is_winner else "T")
    return f"{verb} {ravens.score}-{opponent.score}"


def format_game_title(game: Game) -> str:
    score = format_score(game)
    matchup = format_matchup(game)
    return f"{matchup} — {score}" if score else matchup


def format_game_status(game: Game) -> str:
    parts = [game.status]
    if game.week:
        parts.append(game.week)
    return " • ".join(part for part in parts if part)


def format_venue(game: Game) -> str | None:
    if not game.venue:
        return None
    return f"{game.venue} ({game.location})" if game.location else game.venue


def format_records(game: Game) -> str | None:
    sides = [side for side in (game.away, game.home) if side is not None]
    parts = [
        f"{side.team.short_name} {side.record}" for side in sides if side.record
    ]
    return " • ".join(parts) or None


def format_schedule_entry(game: Game, time_zone: ZoneInfo) -> str:
    lines = [format_kickoff(game, time_zone)]
    score = format_score(game)
    lines.append(score if score else format_game_status(game))
    venue = format_venue(game)
    if venue:
        lines.append(venue)
    return "\n".join(lines)


def format_schedule_day(game: Game, time_zone: ZoneInfo) -> str:
    prefix = f"{game.week} — " if game.week else ""
    return f"{prefix}{format_matchup(game)}"


def format_no_scheduled_games(days: int) -> str:
    return f"No Ravens games scheduled in the next {days} days."


def format_transaction(transaction: Transaction) -> str:
    """The move's prose, with named players linked to their ESPN pages.

    A mass roster cut can name twenty players, where link markup would eat the
    field's character budget and push the actual wording out of view, so long
    lists are left as plain text.
    """
    text = transaction.description
    if len(transaction.players) > MAX_LINKED_PLAYERS:
        return text
    for player in transaction.players:
        url = player.page_url
        if not url:
            continue
        # Replace the name as ESPN wrote it, so surrounding prose is untouched.
        text = text.replace(player.name, link(player.name, url), 1)
    return text


def format_no_transactions(target_date: date) -> str:
    return f"No {RAVENS_NAME} roster transactions found for {format_full_date(target_date)}."


def format_inactive_player(player: InactivePlayer) -> str:
    name = link(player.name, player.page_url)
    if player.position:
        name = f"{player.position} {name}"
    return f"{name} — {player.reason}" if player.reason else name


def format_no_inactives() -> str:
    return "ESPN has not published game day inactives for this game yet."


def format_no_game(target_date: date) -> str:
    return f"No {RAVENS_NAME} game found for {format_full_date(target_date)}."


def format_games_back(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return NO_STAT
    return NO_STAT if text in NO_GAMES_BACK else text


def format_streak(value: str | None) -> str:
    return (value or "").strip() or NO_STAT


def format_differential(value: int | None) -> str:
    if value is None:
        return NO_STAT
    return f"+{value}" if value > 0 else str(value)


def format_win_percent(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return NO_STAT
    return text[1:] if text.startswith("0.") else text


def format_standings_row(standing: Standing) -> str:
    """One division line: rank, team, record, pct, GB, and streak.

    The Ravens row is bolded so the team the bot exists for is findable at a
    glance, and a clinch marker is appended only when ESPN reports one.
    """
    rank = standing.rank if standing.rank is not None else "-"
    name = link(standing.team.name, standing.team.page_url)
    if standing.clinch:
        name = f"{name} ({standing.clinch})"
    if standing.is_ravens:
        name = f"**{name}**"
    return (
        f"{rank}. {name} — {standing.record} "
        f"({format_win_percent(standing.win_percent)}), "
        f"GB {format_games_back(standing.games_back)}, "
        f"{format_streak(standing.streak)}"
    )


def format_standings(standings: list[Standing]) -> str:
    if not standings:
        return format_no_standings()
    return "\n".join(format_standings_row(standing) for standing in standings)


def format_standings_detail(standing: Standing) -> str:
    """The secondary splits for one team, shown as an embed field."""
    parts = []
    if standing.division_record:
        parts.append(f"Div {standing.division_record}")
    if standing.conference_record:
        parts.append(f"Conf {standing.conference_record}")
    if standing.home_record:
        parts.append(f"Home {standing.home_record}")
    if standing.road_record:
        parts.append(f"Away {standing.road_record}")
    if standing.points_for and standing.points_against:
        parts.append(
            f"PF {standing.points_for} / PA {standing.points_against} "
            f"({format_differential(standing.differential)})"
        )
    return " • ".join(parts)


def format_ravens_standing(standings: list[Standing]) -> str | None:
    """A one-line summary of where the Ravens sit, for the embed footer."""
    ravens = next((item for item in standings if item.is_ravens), None)
    if ravens is None:
        return None
    parts = [f"{RAVENS_NAME}: {ravens.record}"]
    if ravens.rank is not None:
        parts.append(f"{ordinal(ravens.rank)} in the AFC North")
    if ravens.playoff_seed:
        parts.append(f"seed {ravens.playoff_seed}")
    parts.append(f"point diff {format_differential(ravens.differential)}")
    return " • ".join(parts)


def ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def format_no_standings() -> str:
    return "Standings are unavailable right now. Try again shortly."
