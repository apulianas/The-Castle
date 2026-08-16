from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from .espn_urls import headshot_url, player_url, team_logo_url, team_url
from .winprob import (
    parse_clock_seconds,
    seconds_remaining_in_game,
    seconds_remaining_in_half,
)


RAVENS_TEAM_ID = "33"
RAVENS_SLUG = "bal"
RAVENS_NAME = "Baltimore Ravens"
RAVENS_ABBREVIATION = "BAL"
# ESPN groups the NFL by division; 12 is the AFC North.
AFC_NORTH_GROUP_ID = "12"


@dataclass(frozen=True)
class TeamRef:
    """A team plus the art and links ESPN publishes alongside it."""

    name: str
    team_id: str | None = None
    abbreviation: str | None = None
    slug: str | None = None
    logo: str | None = None
    link: str | None = None

    @property
    def is_ravens(self) -> bool:
        return self.team_id == RAVENS_TEAM_ID

    @property
    def short_name(self) -> str:
        return self.abbreviation or self.name

    @property
    def logo_url(self) -> str | None:
        return self.logo or team_logo_url(self.slug or self.abbreviation)

    @property
    def page_url(self) -> str | None:
        return self.link or team_url(self.slug or self.abbreviation)


RAVENS = TeamRef(
    name=RAVENS_NAME,
    team_id=RAVENS_TEAM_ID,
    abbreviation=RAVENS_ABBREVIATION,
    slug=RAVENS_SLUG,
)


@dataclass(frozen=True)
class PlayerRef:
    """A named player, enriched with an ESPN athlete id when one is known."""

    name: str
    athlete_id: str | None = None
    position: str | None = None
    headshot: str | None = None
    link: str | None = None

    @property
    def page_url(self) -> str | None:
        return self.link or player_url(self.athlete_id)

    def photo_url(self, width: int | None = None) -> str | None:
        if self.headshot and width is None:
            return self.headshot
        if width is None:
            return headshot_url(self.athlete_id)
        return headshot_url(self.athlete_id, width)

    @property
    def display_name(self) -> str:
        return f"{self.position} {self.name}" if self.position else self.name


def normalize_name(value: str) -> str:
    """A comparison key that survives punctuation and accent differences."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9 ]", "", stripped.lower()).strip()


def same_player(left: PlayerRef, right: PlayerRef) -> bool:
    """Whether two references name the same person.

    ESPN fills the athlete id in on one feed and leaves it out on another, so an
    id match settles it when both carry one and the name decides otherwise. Two
    ids that disagree are two people, whatever their names say.
    """
    if left.athlete_id and right.athlete_id:
        return left.athlete_id == right.athlete_id
    return normalize_name(left.name) == normalize_name(right.name) != ""


# ESPN writes a move as a sentence opening with its verb. These are the verbs for
# a move that puts a player on the roster, as opposed to one that takes a player
# off it, which is what decides whose photo a post leads with.
ROSTER_ADD_ACTIONS = frozenset(
    {
        "signed",
        "resigned",
        "activated",
        "claimed",
        "acquired",
        "promoted",
        "elevated",
        "reinstated",
        "drafted",
        "added",
    }
)


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    date: date
    description: str
    type_text: str | None = None
    athlete: str | None = None
    players: tuple[PlayerRef, ...] = ()
    team: TeamRef | None = None

    @property
    def player(self) -> PlayerRef | None:
        """The single subject of this move, when there is exactly one."""
        return self.players[0] if len(self.players) == 1 else None

    @property
    def action_words(self) -> tuple[str, ...]:
        """The verb each of ESPN's spellings of this move opens with.

        A move arrives as a type ("Signed") and as prose ("Signed WR ..."), and
        either can be the one ESPN filled in, so both are read.
        """
        openings = (
            normalize_name(self.type_text or "").split(),
            normalize_name(self.description or "").split(),
        )
        return tuple(words[0] for words in openings if words)

    @property
    def adds_to_roster(self) -> bool:
        """Whether this move brings a player in rather than sending one out.

        A signing or an activation adds; a release or a move to injured reserve
        does not. A post covering both directions is about the arrival, so this
        is what decides which player it pictures.
        """
        return any(word in ROSTER_ADD_ACTIONS for word in self.action_words)

    @property
    def joining_player(self) -> PlayerRef | None:
        """The player this move puts on the roster, when it is that kind of move.

        A description names the arriving player first, so a compound move such
        as "Signed WR A ... placed WR B on injured reserve" still resolves to A.
        """
        return self.players[0] if self.adds_to_roster and self.players else None

    @property
    def headline(self) -> str:
        """The field title for this move, e.g. "Signed — WR Isaiah Bond"."""
        action = self.type_text
        solo = self.player
        if solo is not None:
            return f"{action} — {solo.display_name}" if action else solo.display_name
        if self.players:
            if len(self.players) > 3:
                names = f"{len(self.players)} players"
            else:
                names = ", ".join(player.display_name for player in self.players)
            return f"{action} — {names}" if action else names
        return action or self.athlete or "Roster move"


@dataclass(frozen=True)
class GameTeam:
    team: TeamRef
    score: int | None = None
    is_home: bool = False
    is_winner: bool = False
    record: str | None = None


def down_text(down: int) -> str:
    """"4th" for 4, and so on, for a down and distance line."""
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(down, "th")
    return f"{down}{suffix}"


@dataclass(frozen=True)
class GameSituation:
    """Where the ball is in a game that is being played right now.

    ``yards_to_goal`` is the only field position the decision model wants: the
    distance from the ball to the end zone the team in possession is attacking.
    ESPN states field position from either side of the fifty, so the parser
    resolves it once here rather than leaving every caller to guess.
    """

    possession: TeamRef
    defense: TeamRef | None = None
    down: int | None = None
    distance: int | None = None
    yards_to_goal: int | None = None
    period: int | None = None
    clock: str | None = None
    # Seconds left in the period, read from the display clock by the parser.
    clock_seconds: int | None = None
    # Points the team in possession leads by; negative when they are trailing.
    score_differential: int | None = None
    is_red_zone: bool = False
    spot: str | None = None
    down_distance_text: str | None = None

    @property
    def is_fourth_down(self) -> bool:
        return self.down == 4

    @property
    def is_goal_to_go(self) -> bool:
        if self.distance is None or self.yards_to_goal is None:
            return False
        return self.distance >= self.yards_to_goal

    @property
    def down_distance(self) -> str | None:
        if self.down_distance_text:
            return self.down_distance_text
        if self.down is None or self.distance is None:
            return None
        distance = "Goal" if self.is_goal_to_go else str(self.distance)
        return f"{down_text(self.down)} & {distance}"

    @property
    def clock_text(self) -> str | None:
        parts = []
        if self.period:
            parts.append("OT" if self.period > 4 else f"Q{self.period}")
        if self.clock:
            parts.append(self.clock)
        return " ".join(parts) or None

    @property
    def period_seconds_remaining(self) -> int | None:
        """Seconds left in the period being played, when the clock is readable.

        The parser fills this in, but a situation built by hand — a test, or a
        remembered down — need only carry the display clock, so it is read from
        there when nobody supplied it.
        """
        if self.clock_seconds is not None:
            return self.clock_seconds
        return parse_clock_seconds(self.clock)

    @property
    def seconds_remaining(self) -> int | None:
        """Seconds left in the game, counting the periods still to come."""
        return seconds_remaining_in_game(self.period, self.period_seconds_remaining)

    @property
    def half_seconds_remaining(self) -> int | None:
        """Seconds left before the half ends, which is what a Q2 call turns on."""
        return seconds_remaining_in_half(self.period, self.period_seconds_remaining)

    @property
    def score_text(self) -> str | None:
        """The score from the point of view of the team with the ball."""
        if self.score_differential is None:
            return None
        if self.score_differential > 0:
            return f"leading by {self.score_differential}"
        if self.score_differential < 0:
            return f"trailing by {abs(self.score_differential)}"
        return "tied"

    @property
    def summary(self) -> str:
        """A one-line situation, e.g. "BAL 4th & 3 at the CIN 10 • Q3 5:21"."""
        parts = [self.possession.short_name]
        down_distance = self.down_distance
        if down_distance:
            parts.append(down_distance)
        if self.spot:
            parts.append(f"at the {self.spot}")
        line = " ".join(parts)
        extras = [text for text in (self.clock_text, self.score_text) if text]
        return " • ".join([line, *extras])


@dataclass(frozen=True)
class Game:
    event_id: str
    name: str
    short_name: str
    start_time: datetime | None
    status: str
    venue: str | None = None
    home: GameTeam | None = None
    away: GameTeam | None = None
    state: str = "pre"
    completed: bool = False
    broadcast: str | None = None
    week: str | None = None
    location: str | None = None
    season: int | None = None
    # ESPN's season type: 1 preseason, 2 regular season, 3 postseason.
    season_type: int | None = None
    week_number: int | None = None
    # Only a game in progress has one, and only then when ESPN publishes it.
    situation: GameSituation | None = None

    @property
    def has_started(self) -> bool:
        return self.state in {"in", "post"}

    @property
    def in_progress(self) -> bool:
        return self.state == "in" and not self.completed

    @property
    def teams(self) -> tuple[GameTeam, ...]:
        return tuple(side for side in (self.away, self.home) if side is not None)

    def side_for(self, team: TeamRef | None) -> GameTeam | None:
        """The competitor entry for a team, so its score can be read back."""
        if team is None:
            return None
        for side in self.teams:
            if side.team.team_id is not None and side.team.team_id == team.team_id:
                return side
        return None

    @property
    def ravens(self) -> GameTeam | None:
        for side in (self.home, self.away):
            if side is not None and side.team.is_ravens:
                return side
        return None

    @property
    def opponent(self) -> GameTeam | None:
        for side in (self.home, self.away):
            if side is not None and not side.team.is_ravens:
                return side
        return None


@dataclass(frozen=True)
class InactivePlayer:
    name: str
    team: str | None = None
    reason: str | None = None
    athlete_id: str | None = None
    position: str | None = None
    is_ravens: bool = False

    @property
    def page_url(self) -> str | None:
        return player_url(self.athlete_id)


@dataclass(frozen=True)
class InactiveReport:
    game: Game
    players: tuple[InactivePlayer, ...]


# How an injury report reads down the page: the players who will not play lead,
# then the ones in doubt, then the season-long lists, which change rarely.
INJURY_STATUS_ORDER = (
    "Out",
    "Doubtful",
    "Questionable",
    "Injured Reserve",
    "Physically Unable to Perform",
    "Non Football Injury",
    "Suspension",
    "Day-To-Day",
    "Probable",
    "Active",
)
_INJURY_STATUS_RANK = {
    status.lower(): rank for rank, status in enumerate(INJURY_STATUS_ORDER)
}
UNKNOWN_INJURY_STATUS = "Unknown"


def normalize_key(value: str) -> str:
    return " ".join((value or "").split()).lower()


def injury_status_rank(status: str | None) -> int:
    """Where a status sorts, with anything unrecognised placed last."""
    key = (status or "").strip().lower()
    if key in _INJURY_STATUS_RANK:
        return _INJURY_STATUS_RANK[key]
    for known, rank in _INJURY_STATUS_RANK.items():
        if key.startswith(known):
            return rank
    return len(INJURY_STATUS_ORDER)


@dataclass(frozen=True)
class InjuryUpdate:
    """One player's entry on the team injury report."""

    player: PlayerRef
    status: str | None = None
    detail: str | None = None
    comment: str | None = None
    return_date: str | None = None
    updated: datetime | None = None

    @property
    def status_text(self) -> str:
        return (self.status or "").strip() or UNKNOWN_INJURY_STATUS

    @property
    def announcement_id(self) -> str:
        """Identity of this entry as posted.

        The status and the update stamp are part of the key so a player moving
        from questionable to out is announced again, while an unchanged report
        polled every five minutes is not.
        """
        who = self.player.athlete_id or normalize_key(self.player.name)
        when = self.updated.isoformat() if self.updated else ""
        return f"{who}:{self.status_text}:{when}"


@dataclass(frozen=True)
class InjuryReport:
    updates: tuple[InjuryUpdate, ...] = ()

    @property
    def last_updated(self) -> datetime | None:
        stamps = [update.updated for update in self.updates if update.updated]
        return max(stamps) if stamps else None


@dataclass(frozen=True)
class RosterNews:
    """A roster move together with the injury entries about the same players.

    ESPN publishes an activation twice, as a transaction and as a status change
    on the injury report, so announcing both posts the same news twice. Pairing
    them lets one post carry the move and the status it produced.
    """

    transaction: Transaction
    injuries: tuple[InjuryUpdate, ...] = ()

    @property
    def last_updated(self) -> datetime | None:
        """When ESPN last touched the injury entries this post carries."""
        return InjuryReport(self.injuries).last_updated

    @property
    def art_players(self) -> tuple[PlayerRef, ...]:
        """Everyone this post could picture, the player joining the roster first.

        A post covering an arrival and a departure is about the arrival, so that
        player leads. Each one is followed by the injury report's own reference
        to them, which sometimes carries a headshot the transaction's does not.
        """
        players: list[PlayerRef] = []
        pending = list(self.injuries)
        for player in self._move_players():
            players.append(player)
            matched = [
                update for update in pending if same_player(update.player, player)
            ]
            for update in matched:
                pending.remove(update)
                players.append(update.player)
        players.extend(update.player for update in pending)
        return tuple(players)

    @property
    def is_one_player(self) -> bool:
        """Whether the move and the injury news are about a single person."""
        distinct: list[PlayerRef] = []
        for player in self.art_players:
            if not any(same_player(player, other) for other in distinct):
                distinct.append(player)
        return len(distinct) == 1

    def _move_players(self) -> list[PlayerRef]:
        """The move's own players, the one joining the roster first."""
        joining = self.transaction.joining_player
        if joining is None:
            return list(self.transaction.players)
        return [
            joining,
            *(
                player
                for player in self.transaction.players
                if not same_player(player, joining)
            ),
        ]


@dataclass(frozen=True)
class Standing:
    team: TeamRef
    record: str
    summary: str = ""
    rank: int | None = None
    wins: int | None = None
    losses: int | None = None
    ties: int | None = None
    win_percent: str | None = None
    games_back: str | None = None
    streak: str | None = None
    points_for: str | None = None
    points_against: str | None = None
    differential: int | None = None
    division_record: str | None = None
    home_record: str | None = None
    road_record: str | None = None
    conference_record: str | None = None
    playoff_seed: int | None = None
    clinch: str | None = None

    @property
    def is_ravens(self) -> bool:
        return self.team.is_ravens


OFFENSE = "offense"
DEFENSE = "defense"
SPECIAL_TEAMS = "special teams"
SNAP_UNITS = (OFFENSE, DEFENSE, SPECIAL_TEAMS)


@dataclass(frozen=True)
class PlayerSnaps:
    """One player's snaps in one game, by unit."""

    player: PlayerRef
    offense: int = 0
    defense: int = 0
    special_teams: int = 0

    @property
    def name(self) -> str:
        return self.player.name

    @property
    def position(self) -> str | None:
        return self.player.position

    @property
    def total(self) -> int:
        return self.offense + self.defense + self.special_teams

    def snaps(self, unit: str) -> int:
        if unit == OFFENSE:
            return self.offense
        if unit == DEFENSE:
            return self.defense
        return self.special_teams

    @property
    def primary_unit(self) -> str:
        """The unit a player belongs to, so a report lists them exactly once."""
        return max(SNAP_UNITS, key=lambda unit: (self.snaps(unit), SNAP_UNITS.index(unit) * -1))


@dataclass(frozen=True)
class SnapCountReport:
    """Ravens snap counts for one game, with the team totals used as denominators."""

    game: Game
    players: tuple[PlayerSnaps, ...] = ()
    offense_total: int = 0
    defense_total: int = 0
    special_teams_total: int = 0

    def total(self, unit: str) -> int:
        if unit == OFFENSE:
            return self.offense_total
        if unit == DEFENSE:
            return self.defense_total
        return self.special_teams_total

    def unit(self, unit: str) -> tuple[PlayerSnaps, ...]:
        """Players whose game was mostly this unit, most snaps first."""
        entries = [entry for entry in self.players if entry.primary_unit == unit and entry.snaps(unit)]
        entries.sort(key=lambda entry: (-entry.snaps(unit), entry.name))
        return tuple(entries)


@dataclass(frozen=True)
class PlayerSnapTotals:
    """One player's snaps summed across several games, plus the per-game rows."""

    player: PlayerRef
    entries: tuple[tuple[Game, PlayerSnaps], ...] = ()
    offense: int = 0
    defense: int = 0
    special_teams: int = 0
    offense_total: int = 0
    defense_total: int = 0
    special_teams_total: int = 0

    @property
    def games(self) -> int:
        return len(self.entries)

    def snaps(self, unit: str) -> int:
        if unit == OFFENSE:
            return self.offense
        if unit == DEFENSE:
            return self.defense
        return self.special_teams

    def total(self, unit: str) -> int:
        if unit == OFFENSE:
            return self.offense_total
        if unit == DEFENSE:
            return self.defense_total
        return self.special_teams_total

    @property
    def primary_unit(self) -> str:
        return max(SNAP_UNITS, key=lambda unit: (self.snaps(unit), SNAP_UNITS.index(unit) * -1))


@dataclass(frozen=True)
class LiveSituation:
    """Where a game stands right now: clock, possession, and down and distance.

    Every field is optional because ESPN publishes this block only while a game
    is being played, and drops parts of it between drives and at a break.
    """

    clock: str | None = None
    period: int | None = None
    possession: TeamRef | None = None
    down_distance: str | None = None
    field_position: str | None = None
    is_red_zone: bool = False
    last_play: str | None = None

    @property
    def has_detail(self) -> bool:
        return any(
            (
                self.clock,
                self.period,
                self.possession,
                self.down_distance,
                self.last_play,
            )
        )


@dataclass(frozen=True)
class TeamGameStats:
    """One team's box score totals, kept as ESPN's own label and value pairs."""

    team: TeamRef
    stats: tuple[tuple[str, str], ...] = ()

    @property
    def is_ravens(self) -> bool:
        return self.team.is_ravens

    def value(self, label: str) -> str | None:
        wanted = label.strip().lower()
        for name, value in self.stats:
            if name.strip().lower() == wanted:
                return value
        return None


@dataclass(frozen=True)
class PlayerGameStats:
    """One player's line in one statistical category, e.g. passing."""

    player: PlayerRef
    category: str
    detail: str
    team: TeamRef | None = None

    @property
    def is_ravens(self) -> bool:
        return self.team is not None and self.team.is_ravens


@dataclass(frozen=True)
class LiveGameReport:
    """A snapshot of one game: score, situation, team totals, and leaders."""

    game: Game
    situation: LiveSituation | None = None
    teams: tuple[TeamGameStats, ...] = ()
    leaders: tuple[PlayerGameStats, ...] = ()

    @property
    def is_live(self) -> bool:
        return self.game.state == "in"

    @property
    def ravens_stats(self) -> TeamGameStats | None:
        return next((entry for entry in self.teams if entry.is_ravens), None)

    @property
    def opponent_stats(self) -> TeamGameStats | None:
        return next((entry for entry in self.teams if not entry.is_ravens), None)

    @property
    def stat_labels(self) -> tuple[str, ...]:
        """Every label either team reported, in the order ESPN listed them."""
        labels: list[str] = []
        for entry in self.teams:
            for label, _ in entry.stats:
                if label not in labels:
                    labels.append(label)
        return tuple(labels)

    @property
    def has_details(self) -> bool:
        return bool(self.teams or self.leaders)
