from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .espn_urls import link
from .fourthdown import (
    FIELD_GOAL_OVERHEAD,
    MAX_FIELD_GOAL_YARDS,
    FieldGoalOutlook,
    FourthDownAdvice,
    Option,
)
from .models import (
    RAVENS_NAME,
    SNAP_UNITS,
    Game,
    GameSituation,
    InactivePlayer,
    InjuryUpdate,
    LiveGameReport,
    LiveSituation,
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


def format_game_state(game: Game) -> str:
    """Status and week, minus a status the kickoff line already implies.

    A post that leads with "Sun, Nov 9 at 1:00 PM EST" does not also need to say
    "Scheduled". Once a game is under way its status is news, so it stays.
    """
    parts = [] if not game.has_started else [game.status]
    if game.week:
        parts.append(game.week)
    return " • ".join(part for part in parts if part)


def short_team_name(name: str | None) -> str:
    """The nickname a team is known by, e.g. "Ravens" for the Baltimore Ravens.

    Every NFL team is a place plus a nickname, and a post that has already named
    the matchup only needs the nickname to say whose list is whose.
    """
    words = (name or "").split()
    return words[-1] if words else "Team"


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


def format_schedule_line(game: Game, time_zone: ZoneInfo) -> str:
    """One game on one line, for a schedule too long to give each its own field."""
    detail = format_score(game) or format_game_time(game, time_zone)
    return f"{format_schedule_day(game, time_zone)} — {detail}"


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


# Leftover punctuation once the opening is removed, e.g. the full stop closing
# "Re-signed WR Tylan Wallace."
_LEADING_PUNCTUATION_RE = re.compile(r"^[\s.,;:—–-]+")


def format_transaction_detail(transaction: Transaction) -> str:
    """What the move's prose adds once the headline has been read.

    A headline already reads "Signed — WR Devontez Walker", so repeating the
    verb and the name in the prose underneath it says the same thing twice. Only
    a move about one player can be trimmed this way, because a compound move
    names players the headline's opening does not cover. An empty string means
    the headline said all there was to say.
    """
    player = transaction.player
    action = transaction.type_text
    if player is None or not action:
        return format_transaction(transaction)
    position = rf"(?:{re.escape(player.position)}\s+)?" if player.position else ""
    opening = re.compile(
        rf"^\s*{re.escape(action)}\s+{position}{re.escape(player.name)}\b",
        re.IGNORECASE,
    )
    match = opening.match(transaction.description or "")
    if match is None:
        return format_transaction(transaction)
    remainder = _LEADING_PUNCTUATION_RE.sub(
        "", (transaction.description or "")[match.end() :]
    ).strip()
    if not remainder:
        return ""
    return remainder[0].upper() + remainder[1:]


def format_inactive_player(player: InactivePlayer) -> str:
    name = link(player.name, player.page_url)
    if player.position:
        name = f"{player.position} {name}"
    return f"{name} — {player.reason}" if player.reason else name


def format_injury_detail(update: InjuryUpdate) -> str:
    """What is wrong and when they are due back, without naming the player.

    A post whose title is already the player's name and status has no use for a
    line that opens by repeating both.
    """
    parts = [part for part in (update.detail, update.comment) if part]
    expected = format_return_date(update.return_date)
    if expected:
        parts.append(f"expected back {expected}")
    return " · ".join(parts)


def format_injury(update: InjuryUpdate) -> str:
    """One injury line: who, what, and when they are expected back."""
    player = update.player
    name = link(player.name, player.page_url)
    if player.position:
        name = f"{player.position} {name}"
    detail = format_injury_detail(update)
    return f"{name} — {detail}" if detail else name


def format_return_date(value: str | None) -> str | None:
    """ESPN dates a return as a timestamp; a report only needs the day."""
    text = (value or "").strip()
    if not text:
        return None
    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    return format_long_date(parsed.date()) if parsed else text


def format_no_injuries() -> str:
    return "ESPN lists no Ravens players on the injury report right now."


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


def format_situation(situation: LiveSituation | None) -> str | None:
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


def format_last_play(situation: LiveSituation | None) -> str | None:
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


def format_no_game_today(target_date: date) -> str:
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


def format_expected_points(value: float) -> str:
    """Expected points to one decimal, with the sign always stated."""
    if value == float("-inf"):
        return NO_STAT
    rounded = round(value, 1) + 0.0
    return f"+{rounded:.1f}" if rounded > 0 else f"{rounded:.1f}"


def format_fourth_down_option(option: Option, best: bool = False) -> str:
    """One option's worth and why, e.g. "+0.9 expected points • 68% convert"."""
    parts = [f"{format_expected_points(option.expected_points)} expected points"]
    if option.detail:
        parts.append(option.detail)
    line = " • ".join(parts)
    return f"**{line}**" if best else line


def format_fourth_down_matchup(game: Game) -> str:
    """The teams, in the order ESPN lists them, for any game not just Ravens."""
    away, home = game.away, game.home
    if away is None or home is None:
        return game.name
    return f"{away.team.name} at {home.team.name}"


def format_fourth_down_situation(game: Game, situation: GameSituation) -> str:
    lines = [situation.summary, format_fourth_down_matchup(game)]
    scores = [
        f"{side.team.short_name} {side.score}"
        for side in game.teams
        if side.score is not None
    ]
    if scores:
        lines.append(" – ".join(scores))
    return "\n".join(lines)


def format_fourth_down_call(advice: FourthDownAdvice) -> str:
    """The headline call, hedged when the top two options are a coin flip."""
    best = advice.best
    if best is None:
        return "No recommendation"
    if advice.is_close:
        runner_up = advice.options[1]
        return f"Too close to call: {best.label} or {runner_up.label.lower()}"
    return best.label


def format_fourth_down(game: Game, advice: FourthDownAdvice) -> str:
    """The whole recommendation as plain text, for a log line or a test."""
    lines = [
        format_fourth_down_call(advice),
        format_fourth_down_situation(game, advice.situation),
    ]
    for index, option in enumerate(advice.options):
        lines.append(
            f"{option.label}: {format_fourth_down_option(option, best=index == 0)}"
        )
    lines.extend(advice.caveats)
    return "\n".join(lines)


def format_no_live_game() -> str:
    return (
        "No NFL game is being played right now. Ask again during a game, or name "
        "a team that is playing."
    )


def format_not_fourth_down(game: Game, reason: str) -> str:
    return f"{format_fourth_down_matchup(game)}: {reason}"


def format_elapsed(seconds: float) -> str:
    """How long ago something happened, in the words a person would use."""
    if seconds < 60:
        return "moments ago"
    minutes = int(seconds // 60)
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago"
    hours = int(minutes // 60)
    minutes -= hours * 60
    unit = "hour" if hours == 1 else "hours"
    if minutes:
        return f"{hours} {unit} {minutes} min ago"
    return f"{hours} {unit} ago"


def format_recalled_fourth_down(age_seconds: float) -> str:
    """The line that keeps a remembered answer from reading as a live one."""
    return (
        f"The play is over. This is the last fourth down seen, from "
        f"{format_elapsed(age_seconds)}."
    )


def format_field_goal_call(outlook: FieldGoalOutlook) -> str:
    """The headline, e.g. "52-yard field goal • 68% good"."""
    kick = f"{outlook.kick_distance}-yard field goal"
    if not outlook.in_range:
        return f"{kick} • out of range"
    return f"{kick} • {round(outlook.make_rate * 100)}% good"


def format_field_goal_detail(outlook: FieldGoalOutlook) -> str:
    """Why the headline says what it does, and what the attempt is worth."""
    lines: list[str] = []
    if outlook.in_range:
        lines.append(
            f"League average from {outlook.kick_distance} yards: "
            f"{round(outlook.make_rate * 100)}% made."
        )
    else:
        lines.append(
            f"Past {MAX_FIELD_GOAL_YARDS} yards the model has no rate to quote, so a "
            f"{outlook.kick_distance}-yard try is treated as out of range."
        )
    if outlook.situation is not None and outlook.yards_to_goal is not None:
        spot = outlook.situation.spot or f"{outlook.yards_to_goal} yard line"
        lines.append(
            f"Ball at the {spot}, which is {outlook.yards_to_goal} yards out; the "
            f"snap and the spot add the other {FIELD_GOAL_OVERHEAD}."
        )
    if outlook.expected_points is not None:
        lines.append(
            f"{format_expected_points(outlook.expected_points)} expected points, "
            "counting where a miss hands the ball over."
        )
    return "\n".join(lines)


def format_field_goal(outlook: FieldGoalOutlook, game: Game | None = None) -> str:
    """The whole answer as plain text, for a log line or a test."""
    lines = [format_field_goal_call(outlook)]
    if game is not None:
        lines.append(format_fourth_down_matchup(game))
    if outlook.situation is not None:
        lines.append(outlook.situation.summary)
    lines.append(format_field_goal_detail(outlook))
    return "\n".join(lines)


def format_no_field_goal_spot() -> str:
    return (
        "No live game to read a ball spot from. Give a kick distance, as in "
        "`/fieldgoal yards:52`."
    )


def format_no_ball_spot(game: Game) -> str:
    return (
        f"{format_fourth_down_matchup(game)}: ESPN has not published a ball spot "
        "for this game yet. Give a kick distance instead."
    )


def format_unknown_team(query: str, suggestions: list[str]) -> str:
    text = f"No live game found for “{query}”."
    if suggestions:
        return f"{text} Playing right now: {', '.join(suggestions)}."
    return text
