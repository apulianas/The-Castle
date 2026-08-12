from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .espn_urls import headshot_url, player_url, team_logo_url, team_url


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
