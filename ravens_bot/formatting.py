from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .espn_urls import link
from .models import (
    RAVENS_NAME,
    SNAP_UNITS,
    Game,
    GameSituation,
    InactivePlayer,
    LiveGameReport,
    PlayerGameStats,
    PlayerSnaps,
    PlayerSnapTotals,
    SnapCountReport,
    Standing,
    TeamGameStats,
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


def format_snap_share(snaps: int, total: int) -> str:
    """A count against its unit total, e.g. "42 of 68 (62%)".

    The team total is derived from published shares, so it is stated only when
    it is at least as large as the count it is measuring; a stale or missing
    denominator should not produce a share above 100%.
    """
    if total <= 0 or snaps > total:
        return f"{snaps}"
    return f"{snaps} of {total} ({round(snaps * 100 / total)}%)"


def format_player_snaps(entry: PlayerSnaps, report: SnapCountReport) -> str:
    """Every unit a player took a snap in, for a single player answer."""
    lines = [
        f"{unit.capitalize()}: {format_snap_share(entry.snaps(unit), report.total(unit))} snaps"
        for unit in SNAP_UNITS
        if entry.snaps(unit)
    ]
    if not lines:
        return "Did not play a snap."
    return "\n".join(lines)


def format_player_snap_totals(totals: PlayerSnapTotals) -> str:
    lines = [
        f"{unit.capitalize()}: {format_snap_share(totals.snaps(unit), totals.total(unit))} snaps"
        for unit in SNAP_UNITS
        if totals.snaps(unit)
    ]
    if not lines:
        return "Did not play a snap."
    return "\n".join(lines)


def format_snap_row(entry: PlayerSnaps, report: SnapCountReport, unit: str) -> str:
    name = link(entry.player.name, entry.player.page_url)
    if entry.position:
        name = f"{entry.position} {name}"
    return f"{name} — {format_snap_share(entry.snaps(unit), report.total(unit))}"


def format_snap_totals_row(totals: PlayerSnapTotals, unit: str) -> str:
    name = link(totals.player.name, totals.player.page_url)
    if totals.player.position:
        name = f"{totals.player.position} {name}"
    games = f"{totals.games} game" if totals.games == 1 else f"{totals.games} games"
    return f"{name} — {format_snap_share(totals.snaps(unit), totals.total(unit))} over {games}"


def format_snap_breakdown(game: Game, entry: PlayerSnaps, report: SnapCountReport) -> str:
    """One game's line in a multi-week breakdown."""
    prefix = f"{game.week} — " if game.week else ""
    unit = entry.primary_unit
    return (
        f"{prefix}{format_matchup(game)}: "
        f"{format_snap_share(entry.snaps(unit), report.total(unit))} {unit}"
    )


def format_snap_game_line(game: Game) -> str:
    prefix = f"{game.week} — " if game.week else ""
    return f"{prefix}{format_game_title(game)}"


def format_snap_period(weeks: int) -> str:
    return "last game" if weeks <= 1 else f"last {weeks} games"


def format_no_snap_games() -> str:
    return f"No completed {RAVENS_NAME} game found to report snap counts for."


def format_no_snap_counts(game: Game | None = None) -> str:
    """Snaps trail the game book, so a fresh game is pending rather than broken."""
    if game is None:
        return "Snap counts have not been published for that game yet."
    return f"Snap counts have not been published for {format_matchup(game)} yet."


def format_unknown_snap_player(query: str, suggestions: list[str]) -> str:
    text = f"No snap counts found for “{query}”."
    if suggestions:
        names = ", ".join(suggestions)
        return f"{text} Did you mean: {names}?"
    return text


def format_time_of_day(moment: datetime, time_zone: ZoneInfo) -> str:
    """A wall clock time, for stating when a snapshot was taken."""
    local = moment.astimezone(time_zone)
    hour = local.hour % 12 or 12
    zone = local.tzname()
    stamp = f"{hour}:{local:%M} {local:%p}"
    return f"{stamp} {zone}" if zone else stamp


def format_period(period: int | None) -> str | None:
    """A quarter number as people say it, with overtime counted from the fifth."""
    if period is None or period < 1:
        return None
    if period <= 4:
        return f"Q{period}"
    return "OT" if period == 5 else f"OT{period - 4}"


def format_live_score(game: Game) -> str:
    """The running score, written away side first the way a scoreboard reads."""
    sides = [side for side in (game.away, game.home) if side is not None]
    parts = [
        f"{side.team.short_name} {side.score if side.score is not None else 0}"
        for side in sides
    ]
    return " — ".join(parts) if parts else game.status


def format_situation(situation: GameSituation | None) -> str | None:
    """Clock, quarter, and possession, skipping whatever ESPN left out."""
    if situation is None:
        return None
    parts = []
    period = format_period(situation.period)
    clock = situation.clock
    if clock and period:
        parts.append(f"{clock} {period}")
    elif clock or period:
        parts.append(clock or period or "")
    if situation.possession is not None:
        marker = f"🏈 {situation.possession.short_name}"
        parts.append(f"{marker} (red zone)" if situation.is_red_zone else marker)
    if situation.down_distance:
        parts.append(situation.down_distance)
    elif situation.field_position:
        parts.append(situation.field_position)
    return " • ".join(part for part in parts if part) or None


def format_last_play(situation: GameSituation | None) -> str | None:
    if situation is None or not situation.last_play:
        return None
    return f"Last play: {situation.last_play}"


def format_team_stat_row(label: str, teams: list[TeamGameStats]) -> str:
    values = " | ".join(entry.value(label) or NO_STAT for entry in teams)
    return f"{label}: {values}"


def format_team_stat_lines(report: LiveGameReport) -> list[str]:
    """A compact table of both teams' totals, one line per statistic."""
    teams = list(report.teams)
    if not teams:
        return []
    header = " | ".join(entry.team.short_name for entry in teams)
    lines = [f"({header})"]
    lines.extend(format_team_stat_row(label, teams) for label in report.stat_labels)
    return lines


def format_team_stats(report: LiveGameReport) -> str | None:
    lines = format_team_stat_lines(report)
    return "\n".join(lines) if lines else None


def format_player_stat_line(line: PlayerGameStats) -> str:
    name = link(line.player.name, line.player.page_url)
    team = line.team.short_name if line.team is not None else None
    prefix = f"{team} " if team else ""
    return f"{line.category}: {prefix}{name} — {line.detail}"


def format_no_live_game(target_date: date) -> str:
    """No game today is the normal case six days a week, so it reads plainly."""
    return (
        f"No {RAVENS_NAME} game today ({format_full_date(target_date)}). "
        "Try `/nextgame` for the next matchup."
    )


def format_pregame(game: Game, time_zone: ZoneInfo) -> str:
    return (
        f"{format_matchup(game)} has not kicked off yet.\n"
        f"{format_kickoff(game, time_zone)}"
    )


def format_no_live_stats() -> str:
    return "ESPN has not published stats for this game yet."
