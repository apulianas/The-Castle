from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from zoneinfo import ZoneInfo

import discord

from .espn_urls import (
    HEADSHOT_FEATURE_WIDTH,
    game_url,
    schedule_url,
    standings_url,
    team_logo_url,
    transactions_url,
)
from .formatting import (
    format_game_status,
    format_game_title,
    format_inactive_player,
    format_kickoff,
    format_long_date,
    format_matchup,
    format_no_game,
    format_no_inactives,
    format_no_scheduled_games,
    format_no_standings,
    format_no_transactions,
    format_ravens_standing,
    format_records,
    format_schedule_day,
    format_schedule_entry,
    format_standings,
    format_standings_detail,
    format_transaction,
    format_venue,
)
from .models import (
    AFC_NORTH_GROUP_ID,
    RAVENS_SLUG,
    Game,
    InactiveReport,
    Standing,
    Transaction,
)


RAVENS_PURPLE = 0x24125F
ERROR_RED = 0xB00020
MAX_EMBED_FIELDS = 25
MAX_DESCRIPTION_CHARS = 4096
MAX_FIELD_CHARS = 1024
DATA_SOURCE = "Data: ESPN"


def _limit_description(text: str, max_chars: int = MAX_DESCRIPTION_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _limit_field(text: str, max_chars: int = MAX_FIELD_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _base_embed(
    title: str,
    description: str | None = None,
    url: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=title, color=RAVENS_PURPLE)
    if description:
        embed.description = _limit_description(description)
    if url:
        embed.url = url
    return embed


def _set_game_art(embed: discord.Embed, game: Game) -> None:
    """Show the opponent's logo, since the Ravens are the constant in every post."""
    opponent = game.opponent
    logo = opponent.team.logo_url if opponent is not None else None
    embed.set_thumbnail(url=logo or team_logo_url(RAVENS_SLUG))


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="Ravens data unavailable", description=message, color=ERROR_RED
    )


def _set_transaction_art(
    embed: discord.Embed, transactions: Sequence[Transaction]
) -> None:
    """Show a large headshot when the post is about one specific player.

    Automatic announcements post one transaction at a time, so a signing gets a
    full-width photo. A multi-player move or a digest of several transactions
    falls back to the team logo, where a single face would misrepresent the post.
    """
    solo = transactions[0] if len(transactions) == 1 else None
    if solo is not None:
        player = solo.player
        if player is not None:
            photo = player.photo_url(HEADSHOT_FEATURE_WIDTH)
            if photo:
                embed.set_image(url=photo)
                return

    for transaction in transactions:
        for player in transaction.players:
            photo = player.photo_url()
            if photo:
                embed.set_thumbnail(url=photo)
                return
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))


def transaction_embeds(
    transactions: list[Transaction], target_date: date
) -> list[discord.Embed]:
    title = f"Ravens transactions — {format_long_date(target_date)}"
    if not transactions:
        return [_base_embed(title, format_no_transactions(target_date))]

    embed = _base_embed(title, url=transactions_url(RAVENS_SLUG))
    for transaction in transactions[:MAX_EMBED_FIELDS]:
        embed.add_field(
            name=_limit_field(transaction.headline, 256),
            value=_limit_field(format_transaction(transaction)),
            inline=False,
        )

    _set_transaction_art(embed, transactions)

    if len(transactions) > MAX_EMBED_FIELDS:
        embed.set_footer(
            text=f"Showing {MAX_EMBED_FIELDS} of {len(transactions)} moves • {DATA_SOURCE}"
        )
    else:
        embed.set_footer(text=DATA_SOURCE)
    return [embed]


def inactive_embeds(
    reports: list[InactiveReport], target_date: date, time_zone: ZoneInfo
) -> list[discord.Embed]:
    if not reports:
        return [
            _base_embed(
                f"Ravens inactives — {format_long_date(target_date)}",
                format_no_game(target_date),
            )
        ]

    embeds: list[discord.Embed] = []
    for report in reports:
        game = report.game
        description = f"{format_kickoff(game, time_zone)}\n{format_game_status(game)}"
        embed = _base_embed(
            f"{format_matchup(game)} — inactives",
            description,
            url=game_url(game.event_id),
        )
        _set_game_art(embed, game)

        if not report.players:
            embed.add_field(
                name="Inactive list", value=format_no_inactives(), inline=False
            )
        else:
            by_team: dict[str, list[str]] = {}
            for player in report.players:
                team = player.team or "Team"
                by_team.setdefault(team, []).append(format_inactive_player(player))
            for team, players in list(by_team.items())[:MAX_EMBED_FIELDS]:
                embed.add_field(
                    name=f"{team} ({len(players)})",
                    value=_limit_field("\n".join(players)),
                    inline=False,
                )

        footer = format_venue(game) or DATA_SOURCE
        embed.set_footer(text=footer if footer == DATA_SOURCE else f"{footer} • {DATA_SOURCE}")
        embeds.append(embed)
    return embeds


def schedule_embed(games: list[Game], time_zone: ZoneInfo, days: int = 7) -> discord.Embed:
    embed = _base_embed("Upcoming Ravens games", url=schedule_url(RAVENS_SLUG))
    if not games:
        embed.description = format_no_scheduled_games(days)
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
        return embed

    for game in games[:MAX_EMBED_FIELDS]:
        embed.add_field(
            name=_limit_field(format_schedule_day(game, time_zone), 256),
            value=_limit_field(format_schedule_entry(game, time_zone)),
            inline=False,
        )
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    if len(games) > MAX_EMBED_FIELDS:
        embed.set_footer(
            text=f"Showing {MAX_EMBED_FIELDS} of {len(games)} games • {DATA_SOURCE}"
        )
    else:
        embed.set_footer(text=DATA_SOURCE)
    return embed


def standings_embed(standings: list[Standing]) -> discord.Embed:
    embed = _base_embed(
        "AFC North standings", url=standings_url(AFC_NORTH_GROUP_ID)
    )
    if not standings:
        embed.description = format_no_standings()
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
        return embed

    embed.description = _limit_description(format_standings(standings))
    for standing in standings[:MAX_EMBED_FIELDS]:
        detail = format_standings_detail(standing)
        if not detail:
            continue
        embed.add_field(
            name=standing.team.short_name,
            value=_limit_field(detail),
            inline=False,
        )
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    summary = format_ravens_standing(standings)
    embed.set_footer(text=f"{summary} • {DATA_SOURCE}" if summary else DATA_SOURCE)
    return embed


def next_game_embed(game: Game | None, time_zone: ZoneInfo) -> discord.Embed:
    if game is None:
        embed = _base_embed("Next Ravens game", "No upcoming Ravens game found.")
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
        return embed

    lines = [format_kickoff(game, time_zone), format_game_status(game)]
    venue = format_venue(game)
    if venue:
        lines.append(venue)
    records = format_records(game)
    if records:
        lines.append(records)

    embed = _base_embed(
        format_game_title(game), "\n".join(lines), url=game_url(game.event_id)
    )
    _set_game_art(embed, game)
    embed.set_footer(text=DATA_SOURCE)
    return embed


def help_embed() -> discord.Embed:
    embed = _base_embed(
        "The Castle commands",
        "Baltimore Ravens roster transactions, inactives, standings, and schedule.",
    )
    embed.add_field(
        name="/transactions [date]",
        value=(
            "Roster moves for today or a `YYYY-MM-DD` date. Every player named is "
            "linked to their ESPN page, and a single-player move gets their photo."
        ),
        inline=False,
    )
    embed.add_field(
        name="/inactives [date]",
        value="Game day inactives by team, with position and reason, when ESPN publishes them.",
        inline=False,
    )
    embed.add_field(
        name="/standings",
        value=(
            "AFC North standings with record, win percentage, games back, and streak, "
            "plus division, conference, home, and away splits."
        ),
        inline=False,
    )
    embed.add_field(
        name="/nextgame",
        value="The next Ravens matchup with kickoff time, broadcast, venue, and records.",
        inline=False,
    )
    embed.add_field(
        name="/schedule [days]",
        value="Upcoming Ravens games for 1-30 days, with kickoff, broadcast, and venue.",
        inline=False,
    )
    embed.add_field(name="/help", value="Show this help message.", inline=False)
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=DATA_SOURCE)
    return embed
