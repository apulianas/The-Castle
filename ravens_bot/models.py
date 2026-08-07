from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


RAVENS_TEAM_ID = "33"
RAVENS_SLUG = "bal"
RAVENS_NAME = "Baltimore Ravens"


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    date: date
    description: str
    type_text: str | None = None
    athlete: str | None = None


@dataclass(frozen=True)
class Game:
    event_id: str
    name: str
    short_name: str
    start_time: datetime | None
    status: str
    venue: str | None = None


@dataclass(frozen=True)
class InactivePlayer:
    name: str
    team: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class InactiveReport:
    game: Game
    players: tuple[InactivePlayer, ...]


@dataclass(frozen=True)
class Standing:
    team: str
    record: str
    summary: str
    rank: int | None = None
