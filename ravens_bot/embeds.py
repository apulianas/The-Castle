from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import discord

from .espn_urls import (
    HEADSHOT_FEATURE_WIDTH,
    game_url,
    injuries_url,
    schedule_url,
    standings_url,
    team_logo_url,
    transactions_url,
)
from .formatting import (
    format_game_status,
    format_game_title,
    format_inactive_player,
    format_injury,
    format_injury_updated,
    format_kickoff,
    format_last_play,
    format_live_score,
    format_long_date,
    format_matchup,
    format_fourth_down_call,
    format_fourth_down_option,
    format_fourth_down_situation,
    format_no_game,
    format_no_game_today,
    format_no_live_stats,
    format_no_inactives,
    format_no_injuries,
    format_no_scheduled_games,
    format_no_standings,
    format_no_snap_counts,
    format_no_transactions,
    format_player_snap_totals,
    format_player_stat_line,
    format_pregame,
    format_player_snaps,
    format_ravens_standing,
    format_records,
    format_schedule_day,
    format_schedule_entry,
    format_schedule_line,
    format_situation,
    format_snap_breakdown,
    format_snap_game_line,
    format_snap_period,
    format_snap_row,
    format_snap_totals_row,
    format_standings,
    format_standings_detail,
    format_time_of_day,
    format_team_stat_lines,
    format_transaction,
    format_venue,
)
from .dates import MAX_SCHEDULE_DAYS
from .fourthdown import FourthDownAdvice
from .models import (
    AFC_NORTH_GROUP_ID,
    RAVENS_SLUG,
    SNAP_UNITS,
    Game,
    InactiveReport,
    InjuryReport,
    InjuryUpdate,
    LiveGameReport,
    PlayerRef,
    PlayerSnaps,
    PlayerSnapTotals,
    RosterNews,
    SnapCountReport,
    Standing,
    Transaction,
)
from .snapcounts import MAX_SNAP_GAMES


RAVENS_PURPLE = 0x24125F
ERROR_RED = 0xB00020
MAX_EMBED_FIELDS = 25
MAX_DESCRIPTION_CHARS = 4096
MAX_FIELD_CHARS = 1024
# Discord counts a whole embed, not just its parts; a long report hits this
# ceiling well before it runs out of fields.
MAX_EMBED_CHARS = 6000
# Room for the footer, which is written after the fields are filled.
SNAP_FOOTER_RESERVE = 120
# The live footer also carries a timestamp, so it reserves a little more.
LIVE_FOOTER_RESERVE = 160
DATA_SOURCE = "Data: ESPN"
# Snap counts come from the NFL game book participation page, not ESPN.
SNAP_DATA_SOURCE = "Data: NFL game book via nflverse"
# Every number behind a fourth down call is a league average, so the footer says
# so rather than letting the recommendation read as a scouted opinion.
FOURTH_DOWN_FOOTER = (
    "League-average expected points. Ignores the clock, the score, and how good "
    "either team is. • Live data: ESPN"
)


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


def _set_player_art(
    embed: discord.Embed, players: Sequence[PlayerRef], feature: bool
) -> None:
    """Picture a post: a full-width photo of one player, or a small thumbnail.

    ``players`` is in priority order, so a caller that knows who is joining the
    roster puts them first and their face leads the post. Anything without a
    usable photo falls through to the team logo.
    """
    if feature and players:
        photo = players[0].photo_url(HEADSHOT_FEATURE_WIDTH)
        if photo:
            embed.set_image(url=photo)
            return
    for player in players:
        photo = player.photo_url()
        if photo:
            embed.set_thumbnail(url=photo)
            return
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))


def _transaction_art_players(
    transactions: Sequence[Transaction],
) -> tuple[PlayerRef, ...]:
    """Players a transaction post could picture, arrivals first.

    A day's moves usually pair an activation with someone going the other way,
    and the post belongs to the player who joined the roster.
    """
    joining = [
        transaction.joining_player
        for transaction in transactions
        if transaction.joining_player is not None
    ]
    others = [player for transaction in transactions for player in transaction.players]
    return tuple([*joining, *others])


def _set_transaction_art(
    embed: discord.Embed, transactions: Sequence[Transaction]
) -> None:
    """Show a large headshot when the post is about one specific player.

    Automatic announcements post one transaction at a time, so a signing gets a
    full-width photo. A multi-player move or a digest of several transactions
    falls back to a thumbnail, where a single face would misrepresent the post.
    """
    solo = len(transactions) == 1 and len(transactions[0].players) == 1
    _set_player_art(embed, _transaction_art_players(transactions), feature=solo)


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


def roster_news_post(
    news: RosterNews, target_date: date, time_zone: ZoneInfo
) -> tuple[list[discord.Embed], tuple[InjuryUpdate, ...]]:
    """The post for a roster move, and the injury updates it actually carries.

    A move with nothing on the injury report reads exactly as it always has, so
    only the paired case gets the combined layout. Discord's limits can cut the
    injury section short on a move naming a whole cut list, so the caller is
    told what made it in: what did not stays unannounced, and the next poll
    posts it on its own rather than recording news nobody was shown.
    """
    if not news.injuries:
        return transaction_embeds([news.transaction], target_date), ()

    transaction = news.transaction
    embed = _base_embed(
        f"Ravens roster move — {format_long_date(target_date)}",
        format_injury_updated(news.last_updated, time_zone),
        url=transactions_url(RAVENS_SLUG),
    )
    embed.add_field(
        name=_limit_field(transaction.headline, 256),
        value=_limit_field(format_transaction(transaction)),
        inline=False,
    )
    by_status: dict[str, list[InjuryUpdate]] = {}
    for update in news.injuries:
        by_status.setdefault(update.status_text, []).append(update)
    # _add_field_blocks fills fields in this order and stops at the first one
    # that does not fit, so the updates it covers are this list's leading run.
    ordered = [update for group in by_status.values() for update in group]
    shown = _add_field_blocks(
        embed,
        [
            # ESPN's practice note has no length limit of its own, so each line
            # is clamped before it is packed into a field, the way a standalone
            # injury post clamps its own.
            (
                f"Injury report — {status}",
                [_limit_field(format_injury(item)) for item in group],
            )
            for status, group in by_status.items()
        ],
    )
    _set_player_art(embed, news.art_players, feature=news.is_one_player)
    if shown < len(ordered):
        embed.set_footer(
            text=f"Showing {shown} of {len(ordered)} updates • {DATA_SOURCE}"
        )
    else:
        embed.set_footer(text=DATA_SOURCE)
    return [embed], tuple(ordered[:shown])


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


def _set_injury_art(embed: discord.Embed, updates: Sequence[InjuryUpdate]) -> None:
    """A small thumbnail only: one player's headshot, or the team logo.

    An injury post is a status line rather than a feature, so even a single
    player keeps the thumbnail-sized photo instead of a full-width image.
    """
    if len(updates) == 1:
        photo = updates[0].player.photo_url()
        if photo:
            embed.set_thumbnail(url=photo)
            return
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))


def injury_embeds(report: InjuryReport, time_zone: ZoneInfo) -> list[discord.Embed]:
    title = "Ravens injury report"
    updates = report.updates
    if not updates:
        embed = _base_embed(title, format_no_injuries(), url=injuries_url(RAVENS_SLUG))
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
        embed.set_footer(text=DATA_SOURCE)
        return [embed]

    embed = _base_embed(
        title,
        format_injury_updated(report.last_updated, time_zone),
        url=injuries_url(RAVENS_SLUG),
    )
    by_status: dict[str, list[str]] = {}
    for update in updates:
        by_status.setdefault(update.status_text, []).append(format_injury(update))
    shown = 0
    for status, lines in list(by_status.items())[:MAX_EMBED_FIELDS]:
        shown += len(lines)
        embed.add_field(
            name=_limit_field(f"{status} ({len(lines)})", 256),
            value=_limit_field("\n".join(lines)),
            inline=False,
        )
    _set_injury_art(embed, updates)
    if shown < len(updates):
        embed.set_footer(
            text=f"Showing {shown} of {len(updates)} players • {DATA_SOURCE}"
        )
    else:
        embed.set_footer(text=DATA_SOURCE)
    return [embed]


def schedule_embed(games: list[Game], time_zone: ZoneInfo, days: int = 7) -> discord.Embed:
    embed = _base_embed("Upcoming Ravens games", url=schedule_url(RAVENS_SLUG))
    if not games:
        embed.description = format_no_scheduled_games(days)
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
        return embed

    if len(games) > MAX_EMBED_FIELDS:
        # Discord allows 25 fields, which is fewer than a season holds, so a
        # long schedule is written as one line per game instead of losing the
        # games that would not fit.
        shown = _describe_schedule(embed, games, time_zone)
    else:
        shown = len(games)
        for game in games:
            embed.add_field(
                name=_limit_field(format_schedule_day(game, time_zone), 256),
                value=_limit_field(format_schedule_entry(game, time_zone)),
                inline=False,
            )
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    if shown < len(games):
        embed.set_footer(text=f"Showing {shown} of {len(games)} games • {DATA_SOURCE}")
    else:
        embed.set_footer(text=DATA_SOURCE)
    return embed


def _describe_schedule(
    embed: discord.Embed, games: list[Game], time_zone: ZoneInfo
) -> int:
    """Write games into the description, and report how many fitted."""
    lines: list[str] = []
    length = 0
    for game in games:
        line = format_schedule_line(game, time_zone)
        if length + len(line) + 1 > MAX_DESCRIPTION_CHARS:
            break
        lines.append(line)
        length += len(line) + 1
    embed.description = "\n".join(lines)
    return len(lines)


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


def fourth_down_embed(game: Game, advice: FourthDownAdvice) -> discord.Embed:
    """The call, the situation behind it, and what each option is worth."""
    embed = _base_embed(
        format_fourth_down_call(advice),
        format_fourth_down_situation(game, advice.situation),
        url=game_url(game.event_id),
    )
    for index, option in enumerate(advice.options):
        embed.add_field(
            name=option.label,
            value=_limit_field(format_fourth_down_option(option, best=index == 0)),
            inline=False,
        )
    for caveat in advice.caveats:
        embed.add_field(name="Worth knowing", value=_limit_field(caveat), inline=False)
    embed.set_thumbnail(url=advice.situation.possession.logo_url or team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=FOURTH_DOWN_FOOTER)
    return embed


def no_fourth_down_embed(message: str, game: Game | None = None) -> discord.Embed:
    """Used both when nothing is being played and when it is not fourth down."""
    embed = _base_embed("Fourth down", message, url=game_url(game.event_id) if game else None)
    logo = None
    if game is not None:
        possession = game.situation.possession if game.situation else None
        logo = possession.logo_url if possession else None
        if logo is None and game.home is not None:
            logo = game.home.team.logo_url
    embed.set_thumbnail(url=logo or team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=DATA_SOURCE)
    return embed


def help_embed() -> discord.Embed:
    embed = _base_embed(
        "The Castle commands",
        "Baltimore Ravens roster transactions, inactives, injuries, standings, and schedule.",
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
        name="/injuries",
        value=(
            "The current Ravens injury report, grouped by status, with the "
            "injury, ESPN's note, and an expected return when one is listed."
        ),
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
        name="/live",
        value=(
            "Live score, clock, possession, and down and distance for today's "
            "game, with team totals and leading players. Shows the final box "
            "score once the game ends."
        ),
        inline=False,
    )
    embed.add_field(
        name="/schedule [days]",
        value=(
            f"Upcoming Ravens games for 1-{MAX_SCHEDULE_DAYS} days, with kickoff, "
            "broadcast, and venue. Ask for a year to get the whole schedule."
        ),
        inline=False,
    )
    embed.add_field(
        name="/snapcounts [player] [weeks]",
        value=(
            "Snap counts from the NFL game book for the last game, or the last "
            f"1-{MAX_SNAP_GAMES} games. Name a player for their own line and a per-game "
            "breakdown; omit one for the full team report by unit."
        ),
        inline=False,
    )
    embed.add_field(
        name="/fourthdown [team]",
        value=(
            "Whether the team with the ball in a live fourth down should go for "
            "it, kick, or punt, with the expected points behind each option. "
            "Defaults to the Ravens game, then the configured second team, then "
            "whatever else is being played."
        ),
        inline=False,
    )
    embed.add_field(name="/help", value="Show this help message.", inline=False)
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=DATA_SOURCE)
    return embed


def _snap_footer(shown: int, available: int, extra: str | None = None) -> str:
    parts = []
    if shown < available:
        parts.append(f"Showing {shown} of {available} players")
    if extra:
        parts.append(extra)
    parts.append(SNAP_DATA_SOURCE)
    return " • ".join(parts)


def _add_field_blocks(
    embed: discord.Embed,
    blocks: list[tuple[str, list[str]]],
    reserve: int = SNAP_FOOTER_RESERVE,
) -> int:
    """Add one field per block, splitting a block that outgrows a field.

    A full snap report names forty players, which is past Discord's 25 field
    limit if each player were a field, so players are lines inside a unit's
    field. Several games of totals then run past the whole embed's character
    budget long before the field limit, so both ceilings are honoured and
    whatever is dropped is counted for the footer.
    """
    budget = MAX_EMBED_CHARS - reserve
    shown = 0
    for name, lines in blocks:
        if not lines:
            continue
        chunk: list[str] = []
        length = 0
        part = 0
        for line in lines:
            if chunk and length + len(line) + 1 > MAX_FIELD_CHARS:
                title = name if part == 0 else f"{name} (cont.)"
                if not _add_block_field(embed, title, chunk, budget):
                    return shown - len(chunk)
                part += 1
                chunk = []
                length = 0
            chunk.append(line)
            length += len(line) + 1
            shown += 1
        if chunk:
            title = name if part == 0 else f"{name} (cont.)"
            if not _add_block_field(embed, title, chunk, budget):
                return shown - len(chunk)
    return shown


def _add_block_field(
    embed: discord.Embed, title: str, lines: list[str], budget: int
) -> bool:
    """Add one field unless it would breach a Discord limit."""
    value = "\n".join(lines)
    if len(embed.fields) >= MAX_EMBED_FIELDS:
        return False
    if len(embed) + len(title) + len(value) > budget:
        return False
    embed.add_field(name=title, value=value, inline=False)
    return True


def no_snap_counts_embed(message: str) -> discord.Embed:
    embed = _base_embed("Ravens snap counts", message)
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=SNAP_DATA_SOURCE)
    return embed


def snap_count_embed(report: SnapCountReport) -> discord.Embed:
    """Every Ravens player's snaps in one game, grouped by unit."""
    game = report.game
    embed = _base_embed(
        f"{format_game_title(game)} — snap counts",
        format_game_status(game),
        url=game_url(game.event_id),
    )
    _set_game_art(embed, game)
    if not report.players:
        embed.description = format_no_snap_counts(game)
        embed.set_footer(text=SNAP_DATA_SOURCE)
        return embed

    blocks = []
    for unit in SNAP_UNITS:
        entries = report.unit(unit)
        title = f"{unit.capitalize()} ({report.total(unit)} snaps)"
        blocks.append(
            (title, [format_snap_row(entry, report, unit) for entry in entries])
        )
    available = sum(len(lines) for _, lines in blocks)
    shown = _add_field_blocks(embed, blocks)
    embed.set_footer(text=_snap_footer(shown, available))
    return embed


def snap_totals_embed(
    totals: list[PlayerSnapTotals], reports: list[SnapCountReport], weeks: int
) -> discord.Embed:
    """Team snap totals across several games, without the per-game rows."""
    period = format_snap_period(weeks)
    embed = _base_embed(f"Ravens snap counts — {period}")
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    if not totals:
        embed.description = format_no_snap_counts()
        embed.set_footer(text=SNAP_DATA_SOURCE)
        return embed

    embed.description = _limit_description(
        "\n".join(format_snap_game_line(report.game) for report in reports)
    )
    blocks = []
    for unit in SNAP_UNITS:
        entries = [
            item for item in totals if item.primary_unit == unit and item.snaps(unit)
        ]
        entries.sort(key=lambda item: (-item.snaps(unit), item.player.name))
        blocks.append(
            (unit.capitalize(), [format_snap_totals_row(item, unit) for item in entries])
        )
    available = sum(len(lines) for _, lines in blocks)
    shown = _add_field_blocks(embed, blocks)
    embed.set_footer(text=_snap_footer(shown, available, f"{len(reports)} games"))
    return embed


def player_snap_embed(entry: PlayerSnaps, report: SnapCountReport) -> discord.Embed:
    """One player's snaps in one game, with their headshot."""
    game = report.game
    embed = _base_embed(
        f"{entry.player.display_name} — snap counts",
        f"{format_game_title(game)}\n{format_game_status(game)}",
        url=game_url(game.event_id),
    )
    embed.add_field(
        name="Snaps", value=_limit_field(format_player_snaps(entry, report)), inline=False
    )
    photo = entry.player.photo_url(HEADSHOT_FEATURE_WIDTH)
    if photo:
        embed.set_image(url=photo)
    else:
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=SNAP_DATA_SOURCE)
    return embed


def player_snap_totals_embed(
    totals: PlayerSnapTotals, reports: list[SnapCountReport], weeks: int
) -> discord.Embed:
    """One player's snaps across several games, with a per-game breakdown."""
    period = format_snap_period(weeks)
    embed = _base_embed(
        f"{totals.player.display_name} — snap counts",
        f"{period.capitalize()} • {totals.games} played",
    )
    embed.add_field(
        name="Totals", value=_limit_field(format_player_snap_totals(totals)), inline=False
    )
    by_game = {report.game.event_id: report for report in reports}
    lines = [
        format_snap_breakdown(game, entry, by_game[game.event_id])
        for game, entry in totals.entries
        if game.event_id in by_game
    ]
    if lines:
        embed.add_field(
            name="By game", value=_limit_field("\n".join(lines)), inline=False
        )
    photo = totals.player.photo_url(HEADSHOT_FEATURE_WIDTH)
    if photo:
        embed.set_image(url=photo)
    else:
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=SNAP_DATA_SOURCE)
    return embed


def no_live_game_embed(target_date: date) -> discord.Embed:
    embed = _base_embed("Ravens live stats", format_no_game_today(target_date))
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=DATA_SOURCE)
    return embed


def _live_title(game: Game) -> str:
    if game.state == "in":
        return f"{format_matchup(game)} — live"
    if game.completed:
        return f"{format_game_title(game)} — final"
    return f"{format_matchup(game)} — pregame"


def _add_live_fields(embed: discord.Embed, report: LiveGameReport) -> None:
    """Team totals and leader lines, within Discord's field and size limits."""
    blocks = [
        ("Team stats", format_team_stat_lines(report)),
        ("Leaders", [format_player_stat_line(line) for line in report.leaders]),
    ]
    _add_field_blocks(embed, blocks, LIVE_FOOTER_RESERVE)


def live_game_embed(
    report: LiveGameReport, time_zone: ZoneInfo, as_of: datetime | None = None
) -> discord.Embed:
    """A live snapshot, or the closest thing ESPN publishes for this game.

    A game that has not kicked off has no stats to show, so it points at
    `/nextgame` instead; a finished game shows the same layout as a live one,
    which is what a final box score is.
    """
    game = report.game
    lines = []
    if game.state == "pre" and not game.completed:
        lines.append(format_pregame(game, time_zone))
        lines.append("Use `/nextgame` for broadcast, venue, and records.")
    else:
        lines.append(f"**{format_live_score(game)}**")
        situation = format_situation(report.situation)
        if situation:
            lines.append(situation)
        lines.append(format_game_status(game))
        last_play = format_last_play(report.situation)
        if last_play:
            lines.append(last_play)
        if not report.has_details:
            lines.append(format_no_live_stats())

    embed = _base_embed(
        _live_title(game), "\n".join(lines), url=game_url(game.event_id)
    )
    _set_game_art(embed, game)
    if game.state != "pre" or game.completed:
        _add_live_fields(embed, report)

    moment = as_of or datetime.now(timezone.utc)
    stamp = format_time_of_day(moment, time_zone)
    embed.set_footer(text=f"As of {stamp} • {DATA_SOURCE}")
    return embed
