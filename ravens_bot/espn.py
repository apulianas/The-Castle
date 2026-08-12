from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .cache import AsyncTtlCache
from .dates import DateWindow, espn_dates
from .espn_urls import player_url
from .models import (
    AFC_NORTH_GROUP_ID,
    RAVENS_NAME,
    RAVENS_SLUG,
    RAVENS_TEAM_ID,
    Game,
    GameSituation,
    GameTeam,
    InactivePlayer,
    InactiveReport,
    PlayerRef,
    Standing,
    TeamRef,
    Transaction,
)


SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
# The site/v2 standings route now returns only a "full standings" link, so the
# grouped table lives on the older apis/v2 route.
SITE_V2_BASE = "https://site.api.espn.com/apis/v2/sports/football/nfl"
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
# ESPN timestamps roster moves at midnight Pacific on the day *after* the move,
# so Pacific is the reference zone for turning a stamp back into a date.
ESPN_TIME_ZONE = ZoneInfo("America/Los_Angeles")

STANDINGS_TTL_SECONDS = 300.0
# A live down and distance is stale within a play, so this cache exists only to
# collapse a burst of commands, not to spare ESPN a request a minute.
LIVE_TTL_SECONDS = 12.0
SCHEDULE_TTL_SECONDS = 180.0
ROSTER_TTL_SECONDS = 3600.0

# Position codes ESPN uses inside transaction prose, e.g. "Waived TE Jordan Murray."
# Descriptions also pluralize them for a group, as in "Waived CBs A and B".
POSITION_CODES = (
    "QB", "RB", "FB", "HB", "WR", "TE", "OL", "OT", "OG", "OC", "C", "G", "T",
    "DL", "DE", "DT", "NT", "EDGE", "LB", "ILB", "OLB", "MLB", "DB", "CB", "S",
    "FS", "SS", "K", "PK", "P", "LS", "KR", "PR", "ATH", "SAF",
)
_POSITION_ALT = "|".join(sorted(POSITION_CODES, key=len, reverse=True))
# A name part is either an initial group like "C.J." or a plain word. Trailing
# sentence periods are deliberately excluded so a name cannot run past the end
# of its sentence into the next one, as in "... Kaimon Rucker. Placed WR ...".
_NAME_PART = r"[A-Z](?:\.[A-Z])*\.|[A-Z][A-Za-z'\u2019\-]+"
# A code only introduces players when a capitalized word follows it, which keeps
# "C.J. Okoye" from reading as the center position.
_POSITION_RE = re.compile(rf"\b(?P<position>{_POSITION_ALT})s?(?=\s+[A-Z])")
_NAME_RE = re.compile(rf"(?:{_NAME_PART})(?:\s+(?:{_NAME_PART}))+")
_SEPARATOR_RE = re.compile(r"\s*(?:,\s*and\s+|,\s*|\s+and\s+)")
_ACTION_RE = re.compile(r"^\s*([A-Z][a-z]+)")


class EspnApiError(RuntimeError):
    pass


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _display_name(value: Any) -> str | None:
    data = _as_dict(value)
    for key in ("displayName", "shortDisplayName", "name", "fullName", "text"):
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _display_name(value)


def _status_text(value: Any) -> str:
    data = _as_dict(value)
    status_type = _as_dict(data.get("type"))
    for key in ("description", "detail", "shortDetail"):
        raw = status_type.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for item in (status_type, data):
        text = _display_name(item)
        if text:
            return text
    return "Scheduled"


def _status_state(value: Any) -> tuple[str, bool]:
    status_type = _as_dict(_as_dict(value).get("type"))
    state = status_type.get("state")
    completed = bool(status_type.get("completed"))
    return (state if isinstance(state, str) and state else "pre"), completed


def _team_id(team: Any) -> str | None:
    data = _as_dict(team)
    raw = data.get("id")
    if raw is not None:
        return str(raw)
    ref = data.get("$ref")
    if isinstance(ref, str):
        parts = ref.rstrip("/").split("/")
        if parts:
            return parts[-1].split("?")[0]
    return None


def _team_name(team: Any) -> str | None:
    data = _as_dict(team)
    return _display_name(data) or _display_name(data.get("team"))


def _logo_href(team: dict[str, Any]) -> str | None:
    """ESPN sends either a logo string or a list of variants; prefer the default."""
    logo = team.get("logo")
    if isinstance(logo, str) and logo.strip():
        return logo.strip()
    logos = _as_list(team.get("logos"))
    fallback: str | None = None
    for item in logos:
        entry = _as_dict(item)
        href = entry.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        rels = {str(rel) for rel in _as_list(entry.get("rel"))}
        if "dark" in rels or "grayscale" in rels:
            continue
        if "default" in rels:
            return href.strip()
        fallback = fallback or href.strip()
    return fallback


def _clubhouse_link(team: dict[str, Any]) -> str | None:
    clubhouse = team.get("clubhouse")
    if isinstance(clubhouse, str) and clubhouse.strip():
        return clubhouse.strip()
    for item in _as_list(team.get("links")):
        entry = _as_dict(item)
        rels = {str(rel) for rel in _as_list(entry.get("rel"))}
        href = entry.get("href")
        if "clubhouse" in rels and isinstance(href, str) and href.strip():
            return href.strip()
    return None


def _slug_from_link(link: str | None) -> str | None:
    if not link:
        return None
    match = re.search(r"/name/([a-z]{2,4})\b", link)
    return match.group(1) if match else None


def team_ref(value: Any) -> TeamRef:
    data = _as_dict(value)
    if "team" in data and not data.get("displayName") and not data.get("abbreviation"):
        data = _as_dict(data.get("team")) or data
    abbreviation = data.get("abbreviation")
    abbreviation = abbreviation.strip() if isinstance(abbreviation, str) else None
    link = _clubhouse_link(data)
    team_identifier = _team_id(data)
    name = (
        _display_name(data)
        or (RAVENS_NAME if team_identifier == RAVENS_TEAM_ID else None)
        or abbreviation
        or "NFL team"
    )
    return TeamRef(
        name=name,
        team_id=team_identifier,
        abbreviation=abbreviation,
        slug=_slug_from_link(link)
        or (abbreviation.lower() if abbreviation else None)
        or (RAVENS_SLUG if team_identifier == RAVENS_TEAM_ID else None),
        logo=_logo_href(data),
        link=link,
    )


def _score(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("value", value.get("displayValue"))
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _record_summary(competitor: dict[str, Any]) -> str | None:
    for item in _as_list(competitor.get("records")):
        entry = _as_dict(item)
        if str(entry.get("type") or "").lower() in {"total", "overall"}:
            summary = entry.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
    return None


def _game_team(competitor: Any) -> GameTeam:
    data = _as_dict(competitor)
    return GameTeam(
        team=team_ref(data.get("team")),
        score=_score(data.get("score")),
        is_home=str(data.get("homeAway") or "").lower() == "home",
        is_winner=bool(data.get("winner")),
        record=_record_summary(data),
    )


def _venue_location(venue: dict[str, Any]) -> str | None:
    address = _as_dict(venue.get("address"))
    parts = [
        str(address[key]).strip()
        for key in ("city", "state")
        if isinstance(address.get(key), str) and str(address[key]).strip()
    ]
    return ", ".join(parts) or None


def _broadcast(competition: dict[str, Any]) -> str | None:
    names: list[str] = []
    for item in _as_list(competition.get("broadcasts")):
        entry = _as_dict(item)
        for name in _as_list(entry.get("names")):
            if isinstance(name, str) and name.strip() and name.strip() not in names:
                names.append(name.strip())
        media = _display_name(entry.get("media"))
        if media and media not in names:
            names.append(media)
    return ", ".join(names) or None


def _week_text(event: dict[str, Any]) -> str | None:
    week = _as_dict(event.get("week"))
    text = week.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    number = week.get("number")
    season_type = _display_name(event.get("seasonType"))
    if number is None:
        return season_type
    label = f"Week {number}"
    if season_type and "regular" not in season_type.lower():
        return f"{season_type} Week {number}"
    return label


def _season_year(event: dict[str, Any]) -> int | None:
    """The season a game belongs to, which is not always its calendar year."""
    for source in (event.get("season"), event.get("league")):
        year = _as_int(_as_dict(source).get("year"))
        if year:
            return year
    return None


def _season_type(event: dict[str, Any]) -> int | None:
    """ESPN's season type: 1 preseason, 2 regular season, 3 postseason."""
    for source in (event.get("seasonType"), event.get("season")):
        data = _as_dict(source)
        for key in ("type", "id"):
            value = _as_int(data.get(key))
            if value:
                return value
    return None


def _week_number(event: dict[str, Any]) -> int | None:
    return _as_int(_as_dict(event.get("week")).get("number"))


def _situation_teams(
    competition: dict[str, Any], possession_id: str | None
) -> tuple[TeamRef | None, TeamRef | None, int | None]:
    """The team with the ball, the team defending, and the score difference."""
    offense = defense = None
    offense_score = defense_score = None
    for competitor in _as_list(competition.get("competitors")):
        data = _as_dict(competitor)
        team = team_ref(data.get("team"))
        score = _score(data.get("score"))
        if possession_id is not None and _team_id(data.get("team")) == possession_id:
            offense, offense_score = team, score
        else:
            defense, defense_score = team, score
    differential = None
    if offense_score is not None and defense_score is not None:
        differential = offense_score - defense_score
    return offense, defense, differential


def _yards_to_goal_from_spot(spot: str, offense: TeamRef) -> int | None:
    """Read "BAL 45" as a distance to the end zone the offence is attacking.

    The marker alone is ambiguous — every yard line but the fifty exists twice —
    so the abbreviation in front of it is what says which half of the field the
    ball is on. On the offence's own side the distance is the complement.
    """
    match = re.match(r"\s*([A-Za-z]{2,4})\s+(\d{1,2})\s*$", spot)
    if match is None:
        return None
    marker = int(match.group(2))
    if not 1 <= marker <= 50:
        return None
    side = match.group(1).upper()
    own = {
        value.upper()
        for value in (offense.abbreviation, offense.slug)
        if value
    }
    if marker == 50:
        return 50
    return 100 - marker if side in own else marker


def _situation_yards_to_goal(
    situation: dict[str, Any], offense: TeamRef
) -> tuple[int | None, str | None]:
    """Yards to the goal line, plus the spot as ESPN words it.

    ESPN publishes the spot three ways and they do not always all appear:
    ``yardsToEndzone`` says outright what is wanted, ``possessionText`` names the
    half of the field, and ``yardLine`` is counted from the offence's own goal
    line. They are read in that order of directness, and a ``yardLine`` that
    contradicts the named spot is discarded rather than averaged in.
    """
    spot = _text(situation.get("possessionText"))
    from_spot = _yards_to_goal_from_spot(spot, offense) if spot else None

    direct = _as_int(situation.get("yardsToEndzone"))
    if direct is not None and 0 <= direct <= 100:
        return direct, spot

    if from_spot is not None:
        return from_spot, spot

    yard_line = _as_int(situation.get("yardLine"))
    if yard_line is not None and 0 <= yard_line <= 100:
        return 100 - yard_line, spot
    return None, spot


def _parse_situation(
    competition: dict[str, Any], state: str
) -> GameSituation | None:
    """The live down and distance, or None when the game is not being played."""
    if state != "in":
        return None
    situation = _as_dict(competition.get("situation"))
    if not situation:
        return None
    possession_id = situation.get("possession")
    possession_id = str(possession_id) if possession_id is not None else None
    offense, defense, differential = _situation_teams(competition, possession_id)
    if offense is None:
        return None
    yards_to_goal, spot = _situation_yards_to_goal(situation, offense)
    status = _as_dict(competition.get("status"))
    return GameSituation(
        possession=offense,
        defense=defense,
        down=_as_int(situation.get("down")),
        distance=_as_int(situation.get("distance")),
        yards_to_goal=yards_to_goal,
        period=_as_int(status.get("period")),
        clock=_text(status.get("displayClock")),
        score_differential=differential,
        is_red_zone=bool(situation.get("isRedZone")),
        spot=spot,
        down_distance_text=_text(situation.get("downDistanceText"))
        or _text(situation.get("shortDownDistanceText")),
    )


def _game_from_event(event: dict[str, Any]) -> Game:
    competitions = _as_list(event.get("competitions"))
    competition = _as_dict(competitions[0]) if competitions else {}
    venue = _as_dict(competition.get("venue"))
    status = competition.get("status") or event.get("status")
    state, completed = _status_state(status)

    home = away = None
    for competitor in _as_list(competition.get("competitors")):
        side = _game_team(competitor)
        if side.is_home:
            home = side
        else:
            away = side

    return Game(
        event_id=str(event.get("id", "")),
        name=str(event.get("name") or event.get("shortName") or "NFL game"),
        short_name=str(event.get("shortName") or event.get("name") or "NFL"),
        start_time=parse_datetime(event.get("date") or competition.get("date")),
        status=_status_text(status),
        venue=_display_name(venue) or _text(venue.get("fullName")),
        home=home,
        away=away,
        state=state,
        completed=completed,
        broadcast=_broadcast(competition),
        week=_week_text(event),
        location=_venue_location(venue),
        season=_season_year(event),
        season_type=_season_type(event),
        week_number=_week_number(event),
        situation=_parse_situation(competition, state),
    )


def event_has_ravens(event: dict[str, Any]) -> bool:
    for competition in _as_list(event.get("competitions")):
        for competitor in _as_list(_as_dict(competition).get("competitors")):
            team = _as_dict(competitor).get("team")
            if _team_id(team) == RAVENS_TEAM_ID:
                return True
    return False


def _parse_events(
    payload: dict[str, Any], keep: Callable[[dict[str, Any]], bool]
) -> list[Game]:
    # A scoreboard states the season and week once for the whole payload, while
    # a team schedule states them per event, so payload level values are used
    # only as a fallback.
    defaults = {
        key: payload[key] for key in ("season", "week") if isinstance(payload.get(key), dict)
    }
    games = []
    for event in _as_list(payload.get("events")):
        event_data = _as_dict(event)
        if keep(event_data):
            games.append(_game_from_event({**defaults, **event_data}))
    games.sort(key=lambda game: (game.start_time is None, game.start_time or datetime.min))
    return games


def parse_schedule(payload: dict[str, Any]) -> list[Game]:
    """Ravens games from a scoreboard or team schedule payload."""
    return _parse_events(payload, event_has_ravens)


def parse_scoreboard(payload: dict[str, Any]) -> list[Game]:
    """Every game in a scoreboard payload, whoever is playing.

    The insight commands answer for any team, so unlike the Ravens-only feeds
    this keeps the whole slate and leaves filtering to the caller.
    """
    return _parse_events(payload, lambda event: True)


def _team_keys(team: TeamRef) -> set[str]:
    """Every spelling of a team a person might type."""
    keys = {
        normalize_name(value)
        for value in (team.name, team.abbreviation, team.slug, team.short_name)
        if value
    }
    # "Ravens" should find Baltimore without spelling out the city.
    name = normalize_name(team.name)
    if " " in name:
        keys.add(name.rsplit(" ", 1)[-1])
    return {key for key in keys if key}


def team_matches(team: TeamRef, query: str) -> bool:
    wanted = normalize_name(query)
    return bool(wanted) and wanted in _team_keys(team)


def match_team_games(games: Iterable[Game], query: str) -> list[Game]:
    """Games featuring a team named by abbreviation, city, or nickname."""
    wanted = normalize_name(query)
    if not wanted:
        return []
    exact = [
        game
        for game in games
        if any(team_matches(side.team, query) for side in game.teams)
    ]
    if exact:
        return exact
    return [
        game
        for game in games
        if any(
            any(wanted in key for key in _team_keys(side.team))
            for side in game.teams
        )
    ]


def team_names(games: Iterable[Game]) -> list[str]:
    """The teams on offer, for a search that found nothing."""
    names: list[str] = []
    for game in games:
        for side in game.teams:
            if side.team.name not in names:
                names.append(side.team.name)
    return sorted(names)


def select_insight_game(
    games: Iterable[Game],
    preferred_team: str | None = None,
    now: datetime | None = None,
) -> Game | None:
    """The live game a live question is most likely about.

    Discord tells the bot nothing about where a person is sitting, and ESPN
    publishes no regional broadcast map, so "the game on near me" is answered by
    a stated order of preference instead of a guess: the Ravens, then whichever
    second team the deployment configured, then whatever kicked off most
    recently, which on a Sunday afternoon is the game still being played.
    """
    live = [game for game in games if game.in_progress]
    if not live:
        return None
    ravens = next((game for game in live if any(side.team.is_ravens for side in game.teams)), None)
    if ravens is not None:
        return ravens
    if preferred_team:
        preferred = match_team_games(live, preferred_team)
        if preferred:
            return preferred[0]
    moment = now or datetime.now(tz=ESPN_TIME_ZONE)

    def started_ago(game: Game) -> tuple[int, float]:
        if game.start_time is None:
            return (1, 0.0)
        return (0, abs((moment - game.start_time).total_seconds()))

    return min(live, key=started_ago)


def _date_from_keys(payload: dict[str, Any], keys: tuple[str, ...]) -> date | None:
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, str):
            parsed = parse_datetime(raw)
            if parsed:
                return _espn_calendar_date(parsed)
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                pass
    return None


def _espn_calendar_date(moment: datetime) -> date:
    """The calendar day a timestamp belongs to in ESPN's own reckoning.

    ESPN stamps roster moves at midnight Pacific, so Pacific is the reference
    zone. Reading a stamp in UTC instead shifts it onto the following day and
    drops the move from that day's results entirely.
    """
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(ESPN_TIME_ZONE).date()


def _transaction_date(payload: dict[str, Any]) -> date | None:
    """Prefer immutable date fields; lastModified is a last resort because ESPN bumps it."""
    return _date_from_keys(payload, ("date", "createDate", "lastModified"))


def _stable_transaction_date(payload: dict[str, Any]) -> date | None:
    """Only dates that never change, so they are safe to build an identity from."""
    return _date_from_keys(payload, ("date", "createDate"))


def _transaction_team_ids(payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("team", "teams"):
        value = payload.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            team_id = _team_id(item)
            if team_id:
                ids.add(team_id)
    return ids


def _transaction_id(payload: dict[str, Any], description: str) -> str:
    """Identify a transaction the same way on every poll, so it is only announced once."""
    raw_id = payload.get("id")
    if raw_id is not None and str(raw_id).strip():
        return str(raw_id).strip()
    stable_date = _stable_transaction_date(payload)
    stamp = stable_date.isoformat() if stable_date else "undated"
    return f"{stamp}:{description}"


def normalize_name(value: str) -> str:
    """A comparison key that survives punctuation and accent differences."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9 ]", "", stripped.lower()).strip()


def extract_players(description: str) -> tuple[PlayerRef, ...]:
    """Players named in a transaction description.

    ESPN's NFL transaction feed carries no athlete records, only prose such as
    "Signed DT Phidarian Mathis to the active roster." Pulling positions and
    names out of the text is what makes player links and photos possible at all.
    One position code can introduce a whole list, as in "Waived CBs A and B",
    so each code is followed as far as its comma-separated run of names goes.
    """
    text = description or ""
    players: list[PlayerRef] = []
    seen: set[str] = set()

    for match in _POSITION_RE.finditer(text):
        position = match.group("position")
        cursor = match.end()
        while True:
            whitespace = re.match(r"\s+", text[cursor:])
            if whitespace:
                cursor += whitespace.end()
            # A new position code ends the current list rather than reading as a name.
            if _POSITION_RE.match(text, cursor):
                break
            found = _NAME_RE.match(text, cursor)
            if found is None:
                break
            name = found.group(0).strip().rstrip(".").strip()
            cursor = found.end()
            key = normalize_name(name)
            if key and key not in seen and len(name.split()) >= 2:
                seen.add(key)
                players.append(PlayerRef(name=name, position=position))
            separator = _SEPARATOR_RE.match(text, cursor)
            if separator is None:
                break
            cursor = separator.end()

    return tuple(players)


def transaction_action(description: str) -> str | None:
    match = _ACTION_RE.match(description or "")
    return match.group(1).strip() if match else None


def parse_transactions(payload: dict[str, Any], target_date: date) -> list[Transaction]:
    """Ravens moves from a date-scoped transactions payload.

    ESPN's own date filter is inclusive of both midnights bounding the day, so a
    query for one date also returns the next day's moves. Each item is stamped at
    midnight Pacific on the day it happened, so the guard keeps only the date that
    was asked for; accepting the extra day would report the same move twice, on
    two consecutive dates.
    """
    raw_items = _as_list(payload.get("items")) or _as_list(payload.get("transactions"))
    transactions: list[Transaction] = []
    for raw in raw_items:
        item = _as_dict(raw)
        item_date = _transaction_date(item)
        if item_date is not None and item_date != target_date:
            continue
        team_ids = _transaction_team_ids(item)
        # ESPN ignores the teams query parameter, so this filter is what keeps
        # the feed to Ravens moves.
        if team_ids and RAVENS_TEAM_ID not in team_ids:
            continue
        athlete = _display_name(item.get("athlete")) or _display_name(item.get("player"))
        type_text = _display_name(item.get("type"))
        description = str(
            item.get("description")
            or item.get("text")
            or " ".join(part for part in (type_text, athlete) if part)
            or "Ravens roster transaction"
        )
        players = extract_players(description)
        if not players and athlete:
            players = (PlayerRef(name=athlete),)
        transactions.append(
            Transaction(
                transaction_id=_transaction_id(item, description),
                date=target_date,
                description=description,
                type_text=type_text or transaction_action(description),
                athlete=athlete or (players[0].name if players else None),
                players=players,
                team=team_ref(item.get("team")) if item.get("team") else None,
            )
        )
    return transactions


def parse_roster(payload: dict[str, Any]) -> dict[str, PlayerRef]:
    """An index of the active roster keyed by normalized full name."""
    index: dict[str, PlayerRef] = {}
    for group in _as_list(payload.get("athletes")):
        group_data = _as_dict(group)
        items = _as_list(group_data.get("items")) or [group_data]
        for raw in items:
            item = _as_dict(raw)
            name = _text(item.get("fullName")) or _text(item.get("displayName"))
            athlete_id = item.get("id")
            if not name or athlete_id is None:
                continue
            position = _as_dict(item.get("position"))
            headshot = _as_dict(item.get("headshot")).get("href")
            player = PlayerRef(
                name=name,
                athlete_id=str(athlete_id),
                position=_text(position.get("abbreviation"))
                or _text(position.get("displayName")),
                headshot=headshot if isinstance(headshot, str) and headshot else None,
                link=player_url(athlete_id),
            )
            index.setdefault(normalize_name(name), player)
    return index


def apply_roster(
    transaction: Transaction, roster: dict[str, PlayerRef]
) -> Transaction:
    """Fill in athlete ids for players the Ravens roster knows about."""
    if not transaction.players:
        return transaction
    resolved: list[PlayerRef] = []
    for player in transaction.players:
        match = roster.get(normalize_name(player.name))
        if match is None:
            resolved.append(player)
            continue
        resolved.append(
            PlayerRef(
                # Keep the description's spelling: format_transaction locates the
                # name in the prose to link it, and the roster spells some names
                # differently ("CJ Okoye" for a description's "C.J. Okoye").
                name=player.name,
                athlete_id=match.athlete_id,
                position=player.position or match.position,
                headshot=match.headshot,
                link=match.link,
            )
        )
    return replace_players(transaction, tuple(resolved))


def replace_players(
    transaction: Transaction, players: tuple[PlayerRef, ...]
) -> Transaction:
    return Transaction(
        transaction_id=transaction.transaction_id,
        date=transaction.date,
        description=transaction.description,
        type_text=transaction.type_text,
        athlete=transaction.athlete,
        players=players,
        team=transaction.team,
    )


def _athlete_id(value: Any) -> str | None:
    data = _as_dict(value)
    raw = data.get("id")
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    for item in _as_list(data.get("links")):
        href = _as_dict(item).get("href")
        if isinstance(href, str):
            match = re.search(r"/id/(\d+)", href)
            if match:
                return match.group(1)
    return None


def _position_text(value: Any) -> str | None:
    """Prefer the abbreviation, since "WR" reads better than "Wide Receiver"."""
    data = _as_dict(value)
    return _text(data.get("abbreviation")) or _display_name(data)


def _player_from_item(
    item: dict[str, Any], fallback_team: str | None = None, is_ravens: bool = False
) -> InactivePlayer | None:
    athlete = _as_dict(item.get("athlete")) or _as_dict(item.get("player"))
    name = _display_name(athlete) or _display_name(item)
    if not name:
        return None
    team = _team_name(item.get("team")) or fallback_team
    reason = _display_name(item.get("reason")) or _display_name(item.get("status"))
    position = _position_text(athlete.get("position")) or _position_text(
        item.get("position")
    )
    return InactivePlayer(
        name=name,
        team=team,
        reason=reason,
        athlete_id=_athlete_id(athlete) or _athlete_id(item),
        position=position,
        is_ravens=is_ravens or (team == RAVENS_NAME),
    )


def _collect_inactives(
    value: Any, players: list[InactivePlayer], team: str | None = None
) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_inactives(item, players, team)
        return
    if not isinstance(value, dict):
        return

    current_team = _team_name(value.get("team")) or team
    for key, nested in value.items():
        lowered = key.lower()
        if lowered in {"inactives", "inactiveplayers", "inactive_players"}:
            for item in _as_list(nested):
                player = _player_from_item(_as_dict(item), current_team)
                if player:
                    players.append(player)
            continue
        if lowered == "status":
            status = (_display_name(nested) or str(nested)).lower()
            if "inactive" in status:
                player = _player_from_item(value, current_team)
                if player:
                    players.append(player)
    for nested in value.values():
        _collect_inactives(nested, players, current_team)


def parse_inactive_report(summary: dict[str, Any], game: Game) -> InactiveReport:
    players: list[InactivePlayer] = []
    _collect_inactives(summary, players)
    seen: set[tuple[str, str | None]] = set()
    unique: list[InactivePlayer] = []
    for player in players:
        key = (player.name, player.team)
        if key not in seen:
            seen.add(key)
            unique.append(player)
    return InactiveReport(game=game, players=tuple(unique))


def _stat_values(entry: dict[str, Any]) -> dict[str, Any]:
    """Flatten an entry's stats into name/type keyed display values."""
    values: dict[str, Any] = {}
    for raw in _as_list(entry.get("stats")):
        stat = _as_dict(raw)
        display = stat.get("displayValue")
        summary = stat.get("summary")
        value = display if display is not None else summary
        if value is None:
            value = stat.get("value")
        for key in (stat.get("name"), stat.get("type")):
            if isinstance(key, str) and key.strip():
                values.setdefault(key.strip().lower(), value)
    return values


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _entry_to_standing(entry: dict[str, Any], fallback_rank: int) -> Standing:
    stats = _stat_values(entry)
    team = team_ref(entry.get("team"))
    wins = _as_int(stats.get("wins"))
    losses = _as_int(stats.get("losses"))
    ties = _as_int(stats.get("ties"))
    record = _text(stats.get("total")) or _text(stats.get("overall"))
    if not record:
        parts = [part for part in (wins, losses, ties) if part is not None]
        record = "-".join(str(part) for part in parts)
    if not record:
        record = str(entry.get("displayName") or entry.get("summary") or "—")

    seed = _as_int(stats.get("playoffseed"))
    # Not the playoff seed: that is a conference-wide 1-16 ranking, and this is
    # the position within the division. ESPN returns division entries in order,
    # so the index is the reliable source when no explicit rank is present.
    rank = _as_int(entry.get("rank")) or fallback_rank
    details = []
    if stats.get("winpercent") is not None:
        details.append(f"Pct {_text(stats.get('winpercent'))}")
    if stats.get("gamesbehind") is not None:
        details.append(f"GB {_text(stats.get('gamesbehind'))}")
    if stats.get("streak") is not None:
        details.append(f"Streak {_text(stats.get('streak'))}")

    return Standing(
        team=team,
        record=record,
        summary=" • ".join(details),
        rank=rank,
        wins=wins,
        losses=losses,
        ties=ties,
        win_percent=_text(stats.get("winpercent")),
        games_back=_text(stats.get("gamesbehind")),
        streak=_text(stats.get("streak")),
        points_for=_text(stats.get("pointsfor")),
        points_against=_text(stats.get("pointsagainst")),
        differential=_as_int(stats.get("pointdifferential") or stats.get("differential")),
        division_record=_text(stats.get("vsdiv")) or _text(stats.get("divisionrecord")),
        home_record=_text(stats.get("home")),
        road_record=_text(stats.get("road")),
        conference_record=_text(stats.get("vsconf")),
        playoff_seed=seed,
        clinch=_text(entry.get("clincher"))
        or _text(_as_dict(entry.get("note")).get("description")),
    )


def _find_group(payload: Any, group_id: str) -> dict[str, Any] | None:
    data = _as_dict(payload)
    if not data:
        return None
    if str(data.get("id") or "") == group_id and data.get("standings"):
        return data
    for child in _as_list(data.get("children")):
        found = _find_group(child, group_id)
        if found is not None:
            return found
    return None


def _collect_entries(payload: Any, entries: list[Any]) -> None:
    data = _as_dict(payload)
    if not data:
        return
    entries.extend(_as_list(_as_dict(data.get("standings")).get("entries")))
    entries.extend(_as_list(data.get("entries")))
    for key in ("children", "standings"):
        for child in _as_list(data.get(key)):
            _collect_entries(child, entries)


def parse_standings(
    payload: dict[str, Any], group_id: str | None = None
) -> list[Standing]:
    """Standings rows, optionally narrowed to one ESPN division group."""
    scope: Any = payload
    if group_id is not None:
        group = _find_group(payload, group_id)
        if group is not None:
            scope = group

    entries: list[Any] = []
    _collect_entries(scope, entries)

    standings: list[Standing] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        data = _as_dict(entry)
        if not data:
            continue
        standing = _entry_to_standing(data, index)
        key = standing.team.team_id or standing.team.name
        if key in seen:
            continue
        seen.add(key)
        standings.append(standing)
    standings.sort(key=lambda item: (item.rank is None, item.rank or 0))
    return standings


class EspnClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._standings_cache: AsyncTtlCache[str, list[Standing]] = AsyncTtlCache(
            STANDINGS_TTL_SECONDS
        )
        self._schedule_cache: AsyncTtlCache[str, list[Game]] = AsyncTtlCache(
            SCHEDULE_TTL_SECONDS
        )
        self._roster_cache: AsyncTtlCache[str, dict[str, PlayerRef]] = AsyncTtlCache(
            ROSTER_TTL_SECONDS
        )
        self._live_cache: AsyncTtlCache[str, list[Game]] = AsyncTtlCache(
            LIVE_TTL_SECONDS, max_entries=4
        )

    async def _json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with self.session.get(url, params=params, timeout=20) as response:
                if response.status >= 400:
                    raise EspnApiError(f"ESPN API returned HTTP {response.status}")
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise EspnApiError(f"Could not reach ESPN API: {exc}") from exc
        if not isinstance(data, dict):
            raise EspnApiError("ESPN API returned an unexpected response")
        return data

    async def _resolve_refs(self, payload: dict[str, Any]) -> dict[str, Any]:
        resolved: list[dict[str, Any]] = []
        for item in _as_list(payload.get("items")):
            item_data = _as_dict(item)
            ref = item_data.get("$ref")
            if isinstance(ref, str):
                try:
                    item_data = await self._json(ref)
                except EspnApiError:
                    continue
            resolved.append(item_data)
        return {**payload, "items": resolved}

    async def fetch_roster(self) -> dict[str, PlayerRef]:
        async def load() -> dict[str, PlayerRef]:
            payload = await self._json(f"{SITE_BASE}/teams/{RAVENS_SLUG}/roster")
            return parse_roster(payload)

        return await self._roster_cache.get_or_fetch("roster", load)

    async def fetch_transactions(self, target_date: date) -> list[Transaction]:
        payload = await self._json(
            f"{CORE_BASE}/transactions",
            {
                # ESPN ignores a teams filter here and returns the whole league,
                # so the page has to be large enough to hold a busy cut day.
                "limit": "500",
                "dates": f"{target_date:%Y%m%d}",
            },
        )
        if any("$ref" in _as_dict(item) for item in _as_list(payload.get("items"))):
            payload = await self._resolve_refs(payload)
        transactions = parse_transactions(payload, target_date)
        if not transactions:
            return transactions
        try:
            roster = await self.fetch_roster()
        except EspnApiError:
            # Player art is a bonus; a roster outage should not drop the move.
            return transactions
        return [apply_roster(transaction, roster) for transaction in transactions]

    async def fetch_schedule(self, window: DateWindow) -> list[Game]:
        key = espn_dates(window)

        async def load() -> list[Game]:
            payload = await self._json(
                f"{SITE_BASE}/scoreboard", {"dates": key, "limit": "100"}
            )
            return parse_schedule(payload)

        games = await self._schedule_cache.get_or_fetch(key, load)
        return list(games)

    async def fetch_season_schedule(self, season: int | None = None) -> list[Game]:
        key = "season" if season is None else f"season:{season}"

        async def load() -> list[Game]:
            params = None if season is None else {"season": str(season)}
            payload = await self._json(
                f"{SITE_BASE}/teams/{RAVENS_SLUG}/schedule", params
            )
            return parse_schedule(payload)

        games = await self._schedule_cache.get_or_fetch(key, load)
        return list(games)

    async def fetch_recent_games(self, count: int, today: date) -> list[Game]:
        """The most recent completed Ravens games, oldest first.

        Completion comes from ESPN's status rather than from comparing dates,
        so a game in progress is not reported as played. Early in a season, and
        all through the offseason, the games asked for are in the season before
        the one the schedule endpoint defaults to.
        """
        schedule = await self.fetch_season_schedule()
        games = [game for game in schedule if game.completed]
        if len(games) < count:
            season = next(
                (game.season for game in reversed(schedule) if game.season), None
            )
            if season is None:
                # The NFL season is named for the year it kicks off in.
                season = today.year if today.month >= 3 else today.year - 1
            try:
                earlier = await self.fetch_season_schedule(season - 1)
            except EspnApiError:
                earlier = []
            games = [game for game in earlier if game.completed] + games
        return games[-count:]

    async def fetch_inactives(self, target_date: date) -> list[InactiveReport]:
        games = await self.fetch_schedule(DateWindow(target_date, target_date))
        reports: list[InactiveReport] = []
        for game in games:
            summary = await self._json(f"{SITE_BASE}/summary", {"event": game.event_id})
            reports.append(parse_inactive_report(summary, game))
        return reports

    async def fetch_live_games(self) -> list[Game]:
        """Every NFL game being played right now, league wide.

        The default scoreboard is the current slate, which is what a live
        question is about; anything not in progress is dropped here so callers
        never have to re-check a status.
        """

        async def load() -> list[Game]:
            payload = await self._json(f"{SITE_BASE}/scoreboard")
            return [game for game in parse_scoreboard(payload) if game.in_progress]

        games = await self._live_cache.get_or_fetch("live", load)
        return list(games)

    async def fetch_standings(self) -> list[Standing]:
        async def load() -> list[Standing]:
            payload = await self._json(f"{SITE_V2_BASE}/standings", {"level": "3"})
            return parse_standings(payload, AFC_NORTH_GROUP_ID)

        standings = await self._standings_cache.get_or_fetch("afc-north", load)
        return list(standings)

    async def fetch_next_game(self, today: date) -> Game | None:
        """The next Ravens game, from the season schedule rather than a long scan."""
        try:
            games = await self.fetch_season_schedule()
        except EspnApiError:
            games = await self.fetch_schedule(
                DateWindow(today, today + timedelta(days=30))
            )
        for game in games:
            if game.completed:
                continue
            if game.start_time is None or game.start_time.date() >= today:
                return game
        return None
