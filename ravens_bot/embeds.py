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
)
from .formatting import (
    format_game_state,
    format_game_status,
    format_game_title,
    format_inactive_player,
    format_injury,
    format_injury_detail,
    format_kickoff,
    format_last_play,
    format_live_score,
    format_long_date,
    format_matchup,
    format_field_goal_call,
    format_field_goal_detail,
    format_fourth_down_call,
    format_fourth_down_matchup,
    format_fourth_down_option,
    format_fourth_down_situation,
    format_recalled_fourth_down,
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
    format_roster_cut_blocks,
    format_schedule_day,
    format_schedule_entry,
    format_schedule_line,
    format_situation,
    format_snap_breakdown,
    format_snap_changes,
    format_snap_comparison,
    format_snap_game_line,
    format_snap_period,
    format_snap_row,
    format_snap_totals_row,
    format_standings,
    format_standings_detail,
    format_time_of_day,
    format_team_stat_lines,
    format_trade_other_moves,
    format_trade_side,
    format_transaction,
    format_transaction_detail,
    format_venue,
    short_team_name,
)
from .dates import MAX_SCHEDULE_DAYS
from .calibration import MODEL_LIMITS, WP_DESCRIPTION
from .fourthdown import FieldGoalOutlook, FourthDownAdvice
from .injury_report import INJURY_REPORT_URL, OfficialInjuryReport
from .models import (
    AFC_NORTH_GROUP_ID,
    OFFENSE,
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
    same_player,
)
from .snapcounts import MAX_SNAP_GAMES
from .official_transactions import transaction_log_url
from .recap import RecapReport
from .recap_formatting import recap_fields


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
# A roster move's footer carries the report's update stamp as well as a count of
# anything the embed could not fit.
ROSTER_FOOTER_RESERVE = 200
DATA_SOURCE = "Data: ESPN"
OFFICIAL_INJURY_DATA_SOURCE = "Data: Baltimore Ravens"
SNAP_DATA_SOURCE = "Data: Pro Football Reference via nflverse"
# Every number behind a fourth down call is a league average, so the footer says
# so rather than letting the recommendation read as a scouted opinion. Which of
# the two models answered is stated as well, since they are different questions.
FOURTH_DOWN_FOOTER = (
    f"{MODEL_LIMITS} Expected points ignore clock and score. • Live data: ESPN"
)
FOURTH_DOWN_WIN_PROBABILITY_FOOTER = (
    f"{MODEL_LIMITS} {WP_DESCRIPTION} End-half and OT strategy are not calibrated. • Live data: ESPN"
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


def _footer(*parts: str | None) -> str:
    """A footer of whatever metadata applies, always crediting the source."""
    return " • ".join([*(part for part in parts if part), DATA_SOURCE])


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
    A mass cut has no face to fall back to either — the first name ESPN wrote is
    just one of thirty players heading out — so it shows the club's own mark.
    """
    if transactions and all(
        transaction.is_mass_roster_cut for transaction in transactions
    ):
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
        return
    solo = len(transactions) == 1 and len(transactions[0].players) == 1
    _set_player_art(embed, _transaction_art_players(transactions), feature=solo)


def _subject_url(transaction: Transaction) -> str:
    """Where a move's title points: the club's own log of that year's moves.

    The move being posted is the one the Ravens published, so the title opens
    the page it was read from rather than another outlet's version of it. The
    players named in the prose still carry their own links.
    """
    return transaction_log_url(transaction.date.year)


def _trade_players(transaction: Transaction) -> tuple[PlayerRef, ...]:
    """Everyone a trade post could picture, the Ravens' arrival first.

    A trade is news because of who is joining, so they lead; the player leaving
    is next, and anyone named only by a move filed alongside the deal is last.
    """
    trade = transaction.trade
    if trade is None:
        return tuple(transaction.players)
    ordered: list[PlayerRef] = []
    for player in (
        *trade.incoming.players,
        *trade.outgoing.players,
        *transaction.players,
    ):
        if not any(same_player(player, other) for other in ordered):
            ordered.append(player)
    return tuple(ordered)


def _set_trade_art(embed: discord.Embed, transaction: Transaction) -> None:
    """Picture the trade, falling back to the other club rather than the Ravens.

    A deal with nobody to show is still about a specific opponent, so their
    logo says more than the Ravens' own, which every post could carry.
    """
    trade = transaction.trade
    assert trade is not None
    players = _trade_players(transaction)
    # One player changing hands is a portrait; a package of several is not.
    solo = len(trade.incoming.players) + len(trade.outgoing.players) == 1
    if solo and players:
        photo = players[0].photo_url(HEADSHOT_FEATURE_WIDTH)
        if photo:
            embed.set_image(url=photo)
            return
    for player in players:
        photo = player.photo_url()
        if photo:
            embed.set_thumbnail(url=photo)
            return
    logo = trade.partner.logo_url if trade.partner is not None else None
    embed.set_thumbnail(url=logo or team_logo_url(RAVENS_SLUG))


def _trade_blocks(transaction: Transaction) -> list[tuple[str, list[str]]]:
    """The two piles a trade moved, and any move filed alongside it.

    The sides are labelled from Baltimore's end rather than by club name, since
    "Ravens send" reads the same whether the partner is a city or a nickname.
    """
    trade = transaction.trade
    assert trade is not None
    blocks: list[tuple[str, list[str]]] = []
    if not trade.incoming.is_empty:
        blocks.append(("Ravens receive", format_trade_side(trade.incoming)))
    if not trade.outgoing.is_empty:
        blocks.append(("Ravens send", format_trade_side(trade.outgoing)))
    other = format_trade_other_moves(trade, transaction.players)
    if other:
        blocks.append(("Also", [_limit_field(other)]))
    return blocks


def _trade_embed(transaction: Transaction, target_date: date) -> discord.Embed:
    embed = _base_embed(
        _limit_field(transaction.headline, 256), url=_subject_url(transaction)
    )
    _add_field_blocks(embed, _trade_blocks(transaction), reserve=ROSTER_FOOTER_RESERVE)
    _set_trade_art(embed, transaction)
    embed.set_footer(text=_footer(format_long_date(target_date)))
    return embed


def _transaction_field_value(transaction: Transaction) -> str:
    """A move's prose for a list, which always needs something to show.

    A trade keeps its full wording: the trimming below drops the opening a
    headline already stated, and a trade's headline states the direction rather
    than the sentence's first few words.
    """
    if transaction.trade is not None:
        return format_transaction(transaction)
    return format_transaction_detail(transaction) or format_transaction(transaction)


def _move_heading(transaction: Transaction) -> tuple[str, str | None]:
    """A move's title and body, with the same words never said twice.

    A move about one player is titled with their name, so the prose beneath it
    drops the opening that repeats it. A compound move already names everyone it
    touches in its prose, so a title listing them again reads as an echo and a
    plain label leads instead. A trade says everything in its fields, so it
    leads with its own headline and no body at all.
    """
    if transaction.trade is not None:
        return _limit_field(transaction.headline, 256), None
    if transaction.is_mass_roster_cut:
        # The grouped list below names everyone and states what happened to
        # them, so prose above it would say the same thing twice at length.
        return _limit_field(transaction.headline, 256), None
    if transaction.player is None:
        return "Ravens roster move", format_transaction(transaction)
    return (
        _limit_field(transaction.headline, 256),
        format_transaction_detail(transaction) or None,
    )


def transaction_embeds(
    transactions: list[Transaction], target_date: date
) -> list[discord.Embed]:
    title = f"Ravens transactions — {format_long_date(target_date)}"
    if not transactions:
        return [_base_embed(title, format_no_transactions(target_date))]

    if len(transactions) == 1:
        # One move is its own headline, so it leads the post rather than sitting
        # in a field under a title that repeats the date and the verb.
        return [_single_transaction_embed(transactions[0], target_date)]

    embed = _base_embed(title, url=transaction_log_url(target_date.year))
    for transaction in transactions[:MAX_EMBED_FIELDS]:
        embed.add_field(
            name=_limit_field(transaction.headline, 256),
            value=_limit_field(_transaction_field_value(transaction)),
            inline=False,
        )

    _set_transaction_art(embed, transactions)

    if len(transactions) > MAX_EMBED_FIELDS:
        embed.set_footer(
            text=_footer(f"Showing {MAX_EMBED_FIELDS} of {len(transactions)} moves")
        )
    else:
        embed.set_footer(text=_footer())
    return [embed]


def _single_transaction_embed(
    transaction: Transaction, target_date: date
) -> discord.Embed:
    if transaction.trade is not None:
        return _trade_embed(transaction, target_date)
    title, body = _move_heading(transaction)
    embed = _base_embed(title, body, url=_subject_url(transaction))
    if transaction.is_mass_roster_cut:
        _add_field_blocks(
            embed, format_roster_cut_blocks(transaction), reserve=ROSTER_FOOTER_RESERVE
        )
    _set_transaction_art(embed, [transaction])
    embed.set_footer(text=_footer(format_long_date(target_date)))
    return embed


def roster_news_post(
    news: RosterNews, target_date: date
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
    trade = transaction.trade
    title, body = _move_heading(transaction)
    embed = _base_embed(title, body, url=_subject_url(transaction))
    if trade is not None:
        # Added in their own pass so the injury count below stays a count of
        # injury lines; the budget is read off the embed as it grows, so the
        # sides taking room here still shorten what the report can fit.
        _add_field_blocks(
            embed, _trade_blocks(transaction), reserve=ROSTER_FOOTER_RESERVE
        )
    elif transaction.is_mass_roster_cut:
        # The cut list goes in first for the same reason, and because a name
        # dropped from it is news lost for good: an injury update the embed
        # cannot fit is left unannounced and posted on its own next time round.
        _add_field_blocks(
            embed, format_roster_cut_blocks(transaction), reserve=ROSTER_FOOTER_RESERVE
        )
    # A move about one player is titled with their name, so the injury lines
    # underneath report the injury alone rather than naming them again. A trade
    # is titled by direction, so its lines still name who they are about.
    solo = trade is None and transaction.player is not None
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
                [_limit_field(_injury_line(item, solo)) for item in group],
            )
            for status, group in by_status.items()
        ],
        reserve=ROSTER_FOOTER_RESERVE,
    )
    if trade is not None:
        _set_trade_art(embed, transaction)
    else:
        _set_player_art(embed, news.art_players, feature=news.is_one_player)
    hidden = f"Showing {shown} of {len(ordered)} updates" if shown < len(ordered) else None
    embed.set_footer(text=_footer(hidden))
    return [embed], tuple(ordered[:shown])


def _injury_line(update: InjuryUpdate, solo: bool) -> str:
    """An injury line, dropping the name when the title already carries it."""
    if solo:
        return format_injury_detail(update) or format_injury(update)
    return format_injury(update)


INACTIVE_CHART_FILENAME = "ravens-inactives.png"


def inactive_embeds(
    reports: list[InactiveReport],
    target_date: date,
    time_zone: ZoneInfo,
    with_players: bool = True,
) -> list[discord.Embed]:
    """The matchup and, unless a chart carries them, the lists themselves."""
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
        description = "\n".join(
            part
            for part in (format_kickoff(game, time_zone), format_game_state(game))
            if part
        )
        embed = _base_embed(
            f"{format_matchup(game)} — inactives",
            description,
            url=game_url(game.event_id),
        )
        _set_game_art(embed, game)
        if report.players and not with_players:
            embed.set_image(url=f"attachment://{INACTIVE_CHART_FILENAME}")

        if not report.players:
            embed.add_field(
                name="Inactive list", value=format_no_inactives(), inline=False
            )
        elif with_players:
            by_team: dict[str, list[str]] = {}
            for player in report.players:
                team = short_team_name(player.team)
                by_team.setdefault(team, []).append(format_inactive_player(player))
            for team, players in list(by_team.items())[:MAX_EMBED_FIELDS]:
                embed.add_field(
                    name=f"{team} ({len(players)})",
                    value=_limit_field("\n".join(players)),
                    inline=False,
                )

        venue = format_venue(game)
        embed.set_footer(text=_footer(venue))
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


def injury_embeds(report: InjuryReport) -> list[discord.Embed]:
    title = "Ravens injury report"
    updates = report.updates
    if not updates:
        embed = _base_embed(title, format_no_injuries(), url=injuries_url(RAVENS_SLUG))
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
        embed.set_footer(text=_footer())
        return [embed]

    if len(updates) == 1:
        # One player is the post, so their name and status are its title rather
        # than a heading, a count of one, and a line opening with both.
        return [_single_injury_embed(updates[0])]

    embed = _base_embed(title, url=injuries_url(RAVENS_SLUG))
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
    hidden = f"Showing {shown} of {len(updates)} players" if shown < len(updates) else None
    embed.set_footer(text=_footer(hidden))
    return [embed]


def official_injury_embed(report: OfficialInjuryReport) -> discord.Embed:
    embed = _base_embed(
        report.title,
        url=report.url,
    )
    embed.set_image(url="attachment://ravens-injury-report.png")
    embed.set_footer(text=OFFICIAL_INJURY_DATA_SOURCE)
    return embed


def _single_injury_embed(update: InjuryUpdate) -> discord.Embed:
    player = update.player
    embed = _base_embed(
        _limit_field(f"{player.display_name} — {update.status_text}", 256),
        format_injury_detail(update) or None,
        url=player.page_url or injuries_url(RAVENS_SLUG),
    )
    _set_injury_art(embed, (update,))
    embed.set_footer(text=_footer())
    return embed


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


def no_recap_embed(target_date: date | None = None) -> discord.Embed:
    when = f" on {target_date.isoformat()}" if target_date else ""
    embed = _base_embed("Ravens postgame recap", f"No completed Ravens regular-season or playoff game found{when}.")
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=DATA_SOURCE)
    return embed


def recap_embed(report: RecapReport, time_zone: ZoneInfo) -> discord.Embed:
    game = report.game
    context = [f"{game.season} season | {format_game_status(game)}", format_kickoff(game, time_zone)]
    if game.venue:
        context.append(format_venue(game))
    embed = _base_embed(
        ("Postgame recap: " + format_game_title(game))[:256],
        _limit_description("\n".join(context), 700),
        url=game_url(game.event_id),
    )
    _set_game_art(embed, game)
    for name, value in recap_fields(report):
        embed.add_field(name=name, value=_limit_field(value), inline=False)
    fetched = report.source.fetched_at.astimezone(time_zone).strftime("%Y-%m-%d %H:%M %Z")
    modified = report.source.last_modified
    footer = f"Final/context: ESPN | Analytics: NFLverse/nflfastR | Fetched {fetched}"
    if modified:
        footer += f" | Source modified: {modified[:80]}"
    embed.set_footer(text=footer)
    return embed


def fourth_down_embed(
    game: Game, advice: FourthDownAdvice, age_seconds: float | None = None
) -> discord.Embed:
    """The call, the situation behind it, and what each option is worth.

    ``age_seconds`` is set when the down has already been played and the answer
    comes from memory, so the embed says so instead of reading as live.
    """
    description = format_fourth_down_situation(game, advice.situation)
    if age_seconds is not None:
        description = f"{format_recalled_fourth_down(age_seconds)}\n{description}"
    embed = _base_embed(
        format_fourth_down_call(advice),
        description,
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
    embed.set_footer(
        text=FOURTH_DOWN_WIN_PROBABILITY_FOOTER
        if advice.ranked_by_win_probability
        else FOURTH_DOWN_FOOTER
    )
    return embed


def field_goal_embed(
    outlook: FieldGoalOutlook, game: Game | None = None
) -> discord.Embed:
    """The odds on one kick, either a named distance or the current ball spot."""
    lines = []
    if game is not None:
        lines.append(format_fourth_down_matchup(game))
    if outlook.situation is not None:
        lines.append(outlook.situation.summary)
    lines.append(format_field_goal_detail(outlook))
    embed = _base_embed(
        format_field_goal_call(outlook),
        "\n".join(lines),
        url=game_url(game.event_id) if game else None,
    )
    logo = None
    if outlook.situation is not None:
        logo = outlook.situation.possession.logo_url
    if logo is None and game is not None and game.home is not None:
        logo = game.home.team.logo_url
    embed.set_thumbnail(url=logo or team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=FOURTH_DOWN_FOOTER)
    return embed


def no_field_goal_embed(message: str, game: Game | None = None) -> discord.Embed:
    embed = _base_embed(
        "Field goal", message, url=game_url(game.event_id) if game else None
    )
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    embed.set_footer(text=DATA_SOURCE)
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
        value="Game day inactives as a chart, by team, with position and reason, when ESPN publishes them.",
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
        name="/recap [date]",
        value="Postgame final score, Ravens efficiency, passing/rushing leaders, and win-probability swings. Omit the date for the latest completed REG/POST game. NFLverse batch data can lag.",
        inline=False,
    )
    embed.add_field(
        name="/snapcounts [player] [weeks]",
        value=(
            "Pro Football Reference snap counts for the last game, or the last "
            f"1-{MAX_SNAP_GAMES} games. Name a player for their own line and a per-game "
            "breakdown; omit one for separate offense, defense, and special-teams "
            "reports, including every participant in each unit. Share changes "
            "compare with the previous completed game in percentage points."
        ),
        inline=False,
    )
    embed.add_field(
        name="/fourthdown [team]",
        value=(
            "Whether the team with the ball in a live fourth down should go for "
            "it, kick, or punt, with the expected points behind each option. "
            "Defaults to the Ravens game, then the configured second team, then "
            "whatever else is being played. Once the play is over it answers the "
            "last fourth down it saw, saying how long ago that was."
        ),
        inline=False,
    )
    embed.add_field(
        name="/fieldgoal [yards] [team]",
        value=(
            "How often a kick of that length is made, and what attempting it is "
            "worth. Omit the yardage to read the distance off where the ball is "
            "in the Ravens game, or whatever else is on."
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


def _snap_pages(
    template: discord.Embed, blocks: list[tuple[str, list[str]]], extra: str | None = None,
) -> list[discord.Embed]:
    """Keep every row, with each page sent as a separate Discord message."""
    remaining = [(title, lines) for title, lines in blocks if lines]
    available = sum(len(lines) for _, lines in remaining)
    pages = []
    while remaining:
        embed = template.copy()
        shown = _add_field_blocks(embed, remaining)
        if not shown:
            raise ValueError("A snap-count row cannot fit within Discord's embed limits")
        embed.set_footer(text=_snap_footer(shown, available, extra))
        pages.append(embed)
        to_skip = shown
        next_blocks = []
        for title, lines in remaining:
            skip = min(to_skip, len(lines))
            to_skip -= skip
            if skip < len(lines):
                next_blocks.append((title, lines[skip:]))
        remaining = next_blocks
    if not pages:
        template.set_footer(text=_snap_footer(0, 0, extra))
        return [template]
    if len(pages) > 1:
        for index, embed in enumerate(pages, 1):
            embed.set_footer(text=f"Page {index}/{len(pages)} • {embed.footer.text}")
    return pages


def snap_count_embed(report: SnapCountReport, unit: str | None = None) -> discord.Embed:
    """The first page of a single-game report."""
    embed = snap_count_embeds(report, unit)[0]
    if embed.footer.text and embed.footer.text.startswith("Page "):
        embed.set_footer(text=embed.footer.text.split(" • ", 1)[1])
    return embed


def snap_count_embeds(report: SnapCountReport, unit: str | None = None) -> list[discord.Embed]:
    """Every Ravens player's snaps in one game, grouped by unit."""
    if unit is not None and unit not in SNAP_UNITS:
        raise ValueError(f"Unknown snap unit: {unit}")
    game = report.game
    embed = _base_embed(
        f"{format_game_title(game)} — {unit + ' ' if unit else ''}snap counts",
        format_game_status(game),
        url=game_url(game.event_id),
    )
    _set_game_art(embed, game)
    if not report.players:
        embed.description = format_no_snap_counts(game)
        embed.set_footer(text=SNAP_DATA_SOURCE)
        return [embed]

    embed.description = f"{format_game_status(game)}\n{format_snap_comparison(report)}"
    blocks = []
    for selected_unit in (SNAP_UNITS if unit is None else (unit,)):
        entries = report.unit(selected_unit)
        title = f"{selected_unit.capitalize()} ({report.total(selected_unit)} snaps)"
        blocks.append(
            (title, [
                format_snap_row(entry, report, selected_unit, unit_change_only=unit is not None)
                for entry in entries
            ])
        )
    if unit is not None and not report.unit(unit):
        embed.description += "\nNo players have recorded snaps in this unit."
    zero_snap_players = [entry for entry in report.players if entry.total == 0]
    if zero_snap_players and unit in (None, OFFENSE):
        blocks.append((
            "No snaps",
            [f"{entry.name} — 0 snaps{format_snap_changes(entry, report)}"
             for entry in zero_snap_players],
        ))
    if report.previous and report.previous.players and unit in (None, OFFENSE):
        current = {entry.identity for entry in report.players}
        absent = [
            f"{entry.name} — change N/A (not listed this game; not assumed zero)"
            for entry in report.previous.players if entry.identity not in current
        ]
        if absent:
            blocks.append(("Previously listed players", absent))
    return _snap_pages(embed, blocks)


def snap_totals_embed(
    totals: list[PlayerSnapTotals], reports: list[SnapCountReport], weeks: int,
    unit: str | None = None,
) -> discord.Embed:
    """The first page of a multi-game report."""
    embed = snap_totals_embeds(totals, reports, weeks, unit)[0]
    if embed.footer.text and embed.footer.text.startswith("Page "):
        embed.set_footer(text=embed.footer.text.split(" • ", 1)[1])
    return embed


def snap_totals_embeds(
    totals: list[PlayerSnapTotals], reports: list[SnapCountReport], weeks: int,
    unit: str | None = None,
) -> list[discord.Embed]:
    """Team snap totals across several games, without the per-game rows."""
    if unit is not None and unit not in SNAP_UNITS:
        raise ValueError(f"Unknown snap unit: {unit}")
    period = format_snap_period(weeks)
    embed = _base_embed(f"Ravens {unit + ' ' if unit else ''}snap counts — {period}")
    embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    if not reports:
        embed.description = format_no_snap_counts()
        embed.set_footer(text=SNAP_DATA_SOURCE)
        return [embed]

    embed.description = _limit_description(
        "\n".join(
            f"{report.game.season or ''} {format_snap_game_line(report.game)}"
            + ("" if report.players else " — not published")
            for report in reports
        ) + "\nLatest game trends:\n" + format_snap_comparison(reports[-1])
    )
    latest = reports[-1]
    latest_players = {entry.identity: entry for entry in latest.players}

    def row(item: PlayerSnapTotals, selected_unit: str) -> str:
        identity = item.entries[-1][1].identity
        entry = latest_players.get(identity)
        if entry is not None:
            change = format_snap_changes(entry, latest, selected_unit if unit is not None else None)
        elif not latest.players:
            change = " | change N/A (latest game unpublished)"
        else:
            change = " | change N/A (not listed in latest game)"
        return format_snap_totals_row(item, selected_unit) + change

    blocks = []
    for selected_unit in (SNAP_UNITS if unit is None else (unit,)):
        entries = [item for item in totals if item.snaps(selected_unit)]
        entries.sort(key=lambda item: (-item.snaps(selected_unit), item.player.name))
        blocks.append(
            (selected_unit.capitalize(), [row(item, selected_unit) for item in entries])
        )
    if unit is not None and not any(item.snaps(unit) for item in totals):
        embed.description += "\nNo players have recorded snaps in this unit in the published games."
    published = sum(bool(report.players) for report in reports)
    return _snap_pages(embed, blocks, f"{published}/{len(reports)} games published")


def player_snap_embed(entry: PlayerSnaps, report: SnapCountReport) -> discord.Embed:
    """One player's snaps in one game, with their headshot."""
    game = report.game
    embed = _base_embed(
        f"{entry.player.display_name} — snap counts",
        f"{format_game_title(game)}\n{format_game_status(game)}\n{format_snap_comparison(report)}",
        url=game_url(game.event_id),
    )
    embed.add_field(
        name="Snaps", value=_limit_field(format_player_snaps(entry, report)), inline=False
    )
    if report.previous is not None:
        embed.add_field(
            name="Snap-share change",
            value=format_snap_changes(entry, report).removeprefix(" | "),
            inline=False,
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
        f"{period.capitalize()} • {totals.games} listed\n"
        "O/D/ST changes are percentage points (pp) versus each game's previous "
        "completed Ravens game, including across seasons. N/A is not zero.",
    )
    embed.add_field(
        name="Totals", value=_limit_field(format_player_snap_totals(totals)), inline=False
    )
    by_game = {game.event_id: entry for game, entry in totals.entries}
    lines = []
    for report in reports:
        entry = by_game.get(report.game.event_id)
        if entry is not None:
            lines.append(format_snap_breakdown(report.game, entry, report))
        else:
            reason = "not listed; change N/A" if report.players else "not published; change N/A"
            lines.append(
                f"{report.game.season or ''} {format_snap_game_line(report.game)}: {reason}"
            )
    if lines:
        shown = _add_field_blocks(embed, [("By game", lines)])
    else:
        shown = 0
    photo = totals.player.photo_url(HEADSHOT_FEATURE_WIDTH)
    if photo:
        embed.set_image(url=photo)
    else:
        embed.set_thumbnail(url=team_logo_url(RAVENS_SLUG))
    hidden = f"Showing {shown} of {len(lines)} games • " if shown < len(lines) else ""
    embed.set_footer(text=f"{hidden}{SNAP_DATA_SOURCE}")
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
