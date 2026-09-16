from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

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
        season_type=2,
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

    async def fetch_season_schedule(
        self, season: int | None = None, season_type: int | None = None
    ) -> list[Game]:
        self.requested.append(season)
        if season_type == 3:
            return []
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
        2025: [build_game("c1", kickoff(2025, 9, 7), completed=True)],
        2024: [build_game("b1", kickoff(2024, 9, 8), season=2024, completed=True)],
        2023: [build_game("a1", kickoff(2023, 9, 10), season=2023, completed=True)],
    }
    client = _ScheduleStub(seasons)

    games = asyncio.run(client.fetch_recent_games(3, date(2025, 10, 1)))

    assert [game.event_id for game in games] == ["a1", "b1", "c1"]


def test_fetch_recent_games_stops_when_a_season_is_missing() -> None:
    seasons = {2025: [build_game("c1", kickoff(2025, 9, 7), completed=True)]}
    client = _ScheduleStub(seasons)

    games = asyncio.run(client.fetch_recent_games(5, date(2025, 10, 1)))

    assert [game.event_id for game in games] == ["c1"]


class _TypedScheduleStub(EspnClient):
    def __init__(self, seasons):
        super().__init__(session=None)  # type: ignore[arg-type]
        self.seasons = seasons
        self.requested = []

    async def fetch_season_schedule(self, season=None, season_type=None):
        self.requested.append((season, season_type))
        result = self.seasons[(season, season_type)]
        if isinstance(result, Exception):
            raise result
        return result


def test_recent_games_includes_postseason_before_crossing_season_boundary() -> None:
    regular = build_game("week18", kickoff(2025, 1, 4), season=2024, completed=True)
    wild_card = replace(
        build_game("wildcard", kickoff(2025, 1, 12), season=2024, completed=True),
        season_type=3,
    )
    divisional = replace(wild_card, event_id="divisional", start_time=kickoff(2025, 1, 19))
    opening = build_game("opening", kickoff(2025, 9, 7), completed=True)
    client = _TypedScheduleStub({
        (2025, 2): [opening], (2025, 3): [],
        (2024, 2): [regular],
        (2024, 3): [divisional, wild_card, divisional],
    })

    games = asyncio.run(client.fetch_recent_games(3, date(2025, 9, 10)))

    assert [game.event_id for game in games] == ["wildcard", "divisional", "opening"]
    assert (2024, 3) in client.requested


def test_recent_games_in_january_uses_previous_calendar_year_season() -> None:
    regular = build_game("week18", kickoff(2025, 1, 4), season=2024, completed=True)
    playoff = replace(regular, event_id="playoff", start_time=kickoff(2025, 1, 12), season_type=3)
    future = replace(playoff, event_id="future", completed=False, start_time=kickoff(2025, 1, 19))
    client = _TypedScheduleStub({(2024, 2): [regular], (2024, 3): [future, playoff]})

    games = asyncio.run(client.fetch_recent_games(2, date(2025, 1, 14)))

    assert [game.event_id for game in games] == ["week18", "playoff"]


def test_recent_games_does_not_backfill_regular_game_after_postseason_outage() -> None:
    game = build_game("regular", kickoff(2025, 1, 4), season=2024, completed=True)
    client = _TypedScheduleStub({
        (2024, 2): [game], (2024, 3): EspnApiError("postseason unavailable"),
    })

    with pytest.raises(EspnApiError, match="postseason unavailable"):
        asyncio.run(client.fetch_recent_games(1, date(2025, 1, 20)))


def test_recent_games_excludes_preseason_and_incomplete_games() -> None:
    game = build_game("regular", kickoff(2025, 9, 7), completed=True)
    preseason = replace(game, event_id="pre", season_type=1)
    future = replace(game, event_id="future", completed=False)
    client = _TypedScheduleStub({(2025, 2): [future, game, preseason], (2025, 3): []})

    assert asyncio.run(client.fetch_recent_games(1, date(2025, 9, 10))) == [game]


def test_season_type_requests_have_distinct_cache_keys(monkeypatch) -> None:
    client = EspnClient(session=None)  # type: ignore[arg-type]
    request = AsyncMock(return_value={"events": []})
    monkeypatch.setattr(client, "_json", request)

    async def run():
        for _ in range(2):
            await client.fetch_season_schedule()
            await client.fetch_season_schedule(2024)
            await client.fetch_season_schedule(2024, season_type=2)
            await client.fetch_season_schedule(2024, season_type=3)

    asyncio.run(run())

    assert request.await_count == 4
    assert [call.args[1] for call in request.call_args_list] == [
        None,
        {"season": "2024"},
        {"season": "2024", "seasontype": "2"},
        {"season": "2024", "seasontype": "3"},
    ]
