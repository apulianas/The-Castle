"""The last fourth down each live game showed, kept for a question asked late.

A fourth down lasts about forty seconds, and the person who wants to argue
about it usually types the command once the play is over, by which point ESPN's
scoreboard has moved on to first and ten. Without a memory the honest answer is
"that is not a fourth down", which is useless. So every live look at the
scoreboard records the fourth down it saw, and the command falls back to that
recording, saying plainly how old it is.

The store is in memory only. A restart loses it, which is the right trade: this
is conversation, not a record, and persisting it would mean writing a play by
play to disk for a question that stops being asked once the game ends.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .models import Game, GameSituation


# Long enough to cover a game and the argument after it, short enough that a
# Sunday answer is never served on Monday.
RECALL_TTL_SECONDS = 4 * 60 * 60
# Any more games than a full slate and the oldest are of no interest anyway.
MAX_REMEMBERED_GAMES = 32


@dataclass(frozen=True)
class RememberedSituation:
    """A fourth down that has already been played, and how long ago it was."""

    game: Game
    situation: GameSituation
    age_seconds: float


class FourthDownMemory:
    """The most recent fourth down seen in each game, keyed by ESPN event id."""

    def __init__(
        self,
        ttl_seconds: float = RECALL_TTL_SECONDS,
        max_games: int = MAX_REMEMBERED_GAMES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_games = max_games
        self._clock = clock
        self._entries: dict[str, tuple[float, Game, GameSituation]] = {}

    def remember(self, games: Iterable[Game]) -> None:
        for game in games:
            situation = game.situation
            if situation is None or not situation.is_fourth_down:
                continue
            self._store(game, situation)

    def recall(self, event_id: str) -> RememberedSituation | None:
        entry = self._entries.get(event_id)
        if entry is None:
            return None
        stored_at, game, situation = entry
        age = self._clock() - stored_at
        if age >= self.ttl_seconds:
            self._entries.pop(event_id, None)
            return None
        return RememberedSituation(game=game, situation=situation, age_seconds=age)

    def latest(self, prefer_ravens: bool = True) -> RememberedSituation | None:
        """The freshest fourth down on offer, with the Ravens taking priority.

        The Ravens game is the one people come back to, so it wins even when
        another game showed a fourth down more recently.
        """
        remembered = [
            found
            for event_id in list(self._entries)
            if (found := self.recall(event_id)) is not None
        ]
        if not remembered:
            return None
        if prefer_ravens:
            ravens = [
                found
                for found in remembered
                if any(side.team.is_ravens for side in found.game.teams)
            ]
            if ravens:
                remembered = ravens
        return min(remembered, key=lambda found: found.age_seconds)

    def _store(self, game: Game, situation: GameSituation) -> None:
        existing = self._entries.get(game.event_id)
        # The same down seen twice keeps its first timestamp, so the age reads
        # as how long ago the down came up rather than when it was last polled.
        if existing is not None and existing[2] == situation:
            self._entries[game.event_id] = (existing[0], game, situation)
            return
        if game.event_id not in self._entries and len(self._entries) >= self.max_games:
            self._drop_oldest()
        self._entries[game.event_id] = (self._clock(), game, situation)

    def _drop_oldest(self) -> None:
        oldest = min(self._entries, key=lambda key: self._entries[key][0])
        self._entries.pop(oldest, None)

    def games(self) -> list[Game]:
        """The games still remembered, freshest first, for a team search."""
        remembered = [
            found
            for event_id in list(self._entries)
            if (found := self.recall(event_id)) is not None
        ]
        remembered.sort(key=lambda found: found.age_seconds)
        return [found.game for found in remembered]
