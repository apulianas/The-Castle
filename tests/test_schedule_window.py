from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ravens_bot.dates import DateWindow
from ravens_bot.espn import EspnApiError, EspnClient
from ravens_bot.models import Game, GameTeam, TeamRef


EASTERN = ZoneInfo("America/New_York")
RAVENS = TeamRef(name="Baltimore Ravens", team_id="33", abbreviation="BAL")
JETS = TeamRef(name="New York Jets", team_id="20", abbreviation="NYJ")


def build_game(
    event_id: str,
    start: datetime | None,
    *,
    season: int | None = 2025,
    completed: bool = False,
) -> Game:
    return Game(
        event_id=event_id,
        name="New York Jets at Baltimore Ravens",
        short_name="NYJ @ BAL",
        start_time=start,
        status="Final" if completed else "Scheduled",
        home=GameTeam(team=RAVENS, is_home=True),
        away=GameTeam(team=JETS, is_home=False),
        completed=completed,
        season=season,
    )


def kickoff(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 18, 0, tzinfo=timezone.utc)


class _ScheduleStub(EspnClient):
    """The client with its schedule calls answered from inline seasons."""

    def __init__(
        self,
        seasons: dict[int | None, list[Game]],
        scoreboard: list[Game] | None = None,
    ) -> None:
        super().__init__(session=None)  # type: ignore[arg-type]
        self._seasons = seasons
        self._scoreboard = scoreboard or []
        self.requested: list[int | None] = []

    async def fetch_season_schedule(self, season: int | None = None) -> list[Game]:
        self.requested.append(season)
        if season not in self._seasons:
            raise EspnApiError("ESPN is unavailable")
        return list(self._seasons[season])

    async def fetch_schedule(self, window: DateWindow) -> list[Game]:
        return list(self._scoreboard)


def test_fetch_upcoming_returns_every_game_in_a_long_window() -> None:
    games = [
        build_game(str(index), kickoff(2025, 9, 7) + timedelta(days=7 * index))
        for index in range(20)
    ]
    client = _ScheduleStub({None: games})

    window = DateWindow(date(2025, 9, 1), date(2026, 8, 31))
    found = asyncio.run(client.fetch_upcoming(window, EASTERN))

    assert len(found) == len(games)


def test_fetch_upcoming_keeps_only_the_games_inside_a_short_window() -> None:
    games = [
        build_game("1", kickoff(2025, 9, 7)),
        build_game("2", kickoff(2025, 9, 14)),
        build_game("3", kickoff(2025, 9, 21)),
    ]
    client = _ScheduleStub({None: games})

    window = DateWindow(date(2025, 9, 10), date(2025, 9, 16))
    found = asyncio.run(client.fetch_upcoming(window, EASTERN))

    assert [game.event_id for game in found] == ["2"]


def test_fetch_upcoming_reaches_into_the_next_season() -> None:
    current = [build_game("1", kickoff(2025, 12, 28), completed=True)]
    following = [build_game("2", kickoff(2026, 9, 13), season=2026)]
    client = _ScheduleStub({None: current, 2026: following})

    window = DateWindow(date(2026, 2, 1), date(2027, 1, 31))
    found = asyncio.run(client.fetch_upcoming(window, EASTERN))

    assert [game.event_id for game in found] == ["2"]
    assert client.requested == [None, 2026]


def test_fetch_upcoming_keeps_undated_games_when_the_window_runs_to_the_end() -> None:
    games = [
        build_game("1", kickoff(2025, 9, 7)),
        build_game("2", None),
    ]
    client = _ScheduleStub({None: games})

    window = DateWindow(date(2025, 9, 1), date(2026, 8, 31))
    found = asyncio.run(client.fetch_upcoming(window, EASTERN))

    assert [game.event_id for game in found] == ["1", "2"]


def test_fetch_upcoming_drops_undated_games_from_a_short_window() -> None:
    games = [
        build_game("1", kickoff(2025, 9, 7)),
        build_game("2", None),
        build_game("3", kickoff(2025, 12, 21)),
    ]
    client = _ScheduleStub({None: games})

    window = DateWindow(date(2025, 9, 1), date(2025, 9, 8))
    found = asyncio.run(client.fetch_upcoming(window, EASTERN))

    assert [game.event_id for game in found] == ["1"]


def test_fetch_upcoming_falls_back_to_the_scoreboard() -> None:
    fallback = [build_game("9", kickoff(2025, 9, 7))]
    client = _ScheduleStub({}, scoreboard=fallback)

    window = DateWindow(date(2025, 9, 1), date(2025, 9, 8))
    found = asyncio.run(client.fetch_upcoming(window, EASTERN))

    assert [game.event_id for game in found] == ["9"]


def test_fetch_recent_games_walks_back_more_than_one_season() -> None:
    seasons = {
        None: [build_game("c1", kickoff(2025, 9, 7), completed=True)],
        2024: [build_game("b1", kickoff(2024, 9, 8), season=2024, completed=True)],
        2023: [build_game("a1", kickoff(2023, 9, 10), season=2023, completed=True)],
    }
    client = _ScheduleStub(seasons)

    games = asyncio.run(client.fetch_recent_games(3, date(2025, 10, 1)))

    assert [game.event_id for game in games] == ["a1", "b1", "c1"]


def test_fetch_recent_games_stops_when_a_season_is_missing() -> None:
    seasons = {None: [build_game("c1", kickoff(2025, 9, 7), completed=True)]}
    client = _ScheduleStub(seasons)

    games = asyncio.run(client.fetch_recent_games(5, date(2025, 10, 1)))

    assert [game.event_id for game in games] == ["c1"]
