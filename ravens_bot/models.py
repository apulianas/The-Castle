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

    @property
    def has_started(self) -> bool:
        return self.state in {"in", "post"}

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
