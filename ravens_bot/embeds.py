from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import discord

from .models import RAVENS_NAME, Game, InactiveReport, Standing, Transaction


RAVENS_PURPLE = 0x24125F


def _format_game_time(game: Game, time_zone: ZoneInfo) -> str:
    if game.start_time is None:
        return "Time TBA"
    return game.start_time.astimezone(time_zone).strftime("%a, %b %-d at %-I:%M %p %Z")


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="Ravens bot error", description=message, color=0xB00020)


def transaction_embeds(transactions: list[Transaction], target_date: date) -> list[discord.Embed]:
    title = f"Ravens transactions — {target_date:%B %-d, %Y}"
    if not transactions:
        return [
            discord.Embed(
                title=title,
                description=f"No {RAVENS_NAME} roster transactions found for {target_date:%A, %B %-d, %Y}.",
                color=RAVENS_PURPLE,
            )
        ]
    embed = discord.Embed(title=title, color=RAVENS_PURPLE)
    for transaction in transactions[:25]:
        name = transaction.type_text or transaction.athlete or "Roster move"
        embed.add_field(name=name, value=transaction.description[:1024], inline=False)
    return [embed]


def inactive_embeds(reports: list[InactiveReport], target_date: date, time_zone: ZoneInfo) -> list[discord.Embed]:
    if not reports:
        return [
            discord.Embed(
                title=f"Ravens inactives — {target_date:%B %-d, %Y}",
                description=f"No {RAVENS_NAME} game found for this date.",
                color=RAVENS_PURPLE,
            )
        ]

    embeds: list[discord.Embed] = []
    for report in reports:
        embed = discord.Embed(
            title=f"{report.game.short_name} inactives",
            description=f"{_format_game_time(report.game, time_zone)} • {report.game.status}",
            color=RAVENS_PURPLE,
        )
        if report.game.venue:
            embed.set_footer(text=report.game.venue)
        if not report.players:
            embed.add_field(
                name="Inactive list",
                value="ESPN has not published game day inactives for this game yet.",
                inline=False,
            )
        else:
            by_team: dict[str, list[str]] = {}
            for player in report.players:
                team = player.team or "Team"
                details = player.name if not player.reason else f"{player.name} — {player.reason}"
                by_team.setdefault(team, []).append(details)
            for team, players in by_team.items():
                embed.add_field(name=team, value="\n".join(players[:25])[:1024], inline=False)
        embeds.append(embed)
    return embeds


def schedule_embed(games: list[Game], time_zone: ZoneInfo) -> discord.Embed:
    embed = discord.Embed(title="Upcoming Ravens games", color=RAVENS_PURPLE)
    if not games:
        embed.description = "No upcoming Ravens games found."
        return embed
    for game in games[:10]:
        details = f"{_format_game_time(game, time_zone)} • {game.status}"
        if game.venue:
            details += f"\n{game.venue}"
        embed.add_field(name=game.name, value=details, inline=False)
    return embed


def standings_embed(standings: list[Standing]) -> discord.Embed:
    embed = discord.Embed(title="NFL AFC North standings", color=RAVENS_PURPLE)
    if not standings:
        embed.description = "Standings are not available right now."
        return embed
    lines = []
    for standing in standings:
        prefix = f"{standing.rank}. " if standing.rank is not None else ""
        name = f"**{standing.team}**" if standing.team == RAVENS_NAME else standing.team
        suffix = f" — {standing.summary}" if standing.summary else ""
        lines.append(f"{prefix}{name}: {standing.record}{suffix}")
    embed.description = "\n".join(lines[:20])
    return embed


def next_game_embed(game: Game | None, time_zone: ZoneInfo) -> discord.Embed:
    embed = discord.Embed(title="Next Ravens game", color=RAVENS_PURPLE)
    if game is None:
        embed.description = "No upcoming Ravens game found."
    else:
        embed.description = f"{game.name}\n{_format_game_time(game, time_zone)} • {game.status}"
        if game.venue:
            embed.set_footer(text=game.venue)
    return embed


def help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="The Castle commands",
        description="Baltimore Ravens roster transactions, inactives, standings, and schedule.",
        color=RAVENS_PURPLE,
    )
    embed.add_field(name="/transactions [date]", value="Roster moves for today or YYYY-MM-DD.", inline=False)
    embed.add_field(name="/inactives [date]", value="Game day inactives when ESPN publishes them.", inline=False)
    embed.add_field(name="/standings", value="AFC North standings.", inline=False)
    embed.add_field(name="/nextgame", value="Who the Ravens play next.", inline=False)
    embed.add_field(name="/schedule [days]", value="Upcoming Ravens games for 1-30 days.", inline=False)
    return embed
