from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord

from ravens_bot.bot import (
    INACTIVE_IDLE_TICKS,
    INACTIVE_WATCH_GRACE,
    INACTIVE_WATCH_INTERVAL_SECONDS,
    INACTIVE_WATCH_LEAD,
    RavensBot,
    _AnnouncementTarget,
    watching_inactives,
)
from ravens_bot.config import BotConfig
from ravens_bot.espn import EspnApiError
from ravens_bot.models import (
    Game,
    GameTeam,
    InactivePlayer,
    InactiveReport,
    TeamRef,
)


EASTERN = ZoneInfo("America/New_York")
KICKOFF = datetime(2025, 11, 23, 13, 0, tzinfo=EASTERN)
RAVENS = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
BROWNS = TeamRef("Cleveland Browns", "5", "CLE", "cle")


def _game(
    start: datetime | None = KICKOFF,
    completed: bool = False,
    state: str = "pre",
) -> Game:
    return Game(
        "401",
        "Baltimore Ravens at Cleveland Browns",
        "BAL @ CLE",
        start,
        "Pre-Game",
        home=GameTeam(BROWNS, is_home=True),
        away=GameTeam(RAVENS),
        state=state,
        completed=completed,
    )


def test_the_watch_opens_ninety_minutes_before_kickoff() -> None:
    game = _game()

    assert INACTIVE_WATCH_LEAD == timedelta(minutes=90)
    assert watching_inactives([game], KICKOFF - INACTIVE_WATCH_LEAD)
    assert not watching_inactives(
        [game], KICKOFF - INACTIVE_WATCH_LEAD - timedelta(seconds=1)
    )


def test_the_watch_runs_a_little_past_a_slipped_kickoff() -> None:
    game = _game()

    assert watching_inactives([game], KICKOFF + INACTIVE_WATCH_GRACE)
    assert not watching_inactives(
        [game], KICKOFF + INACTIVE_WATCH_GRACE + timedelta(seconds=1)
    )


def test_a_finished_game_is_no_longer_watched() -> None:
    game = _game(completed=True, state="post")

    assert not watching_inactives([game], KICKOFF)


def test_a_game_without_a_kickoff_time_is_watched_anyway() -> None:
    assert watching_inactives([_game(start=None)], KICKOFF)


def test_no_game_means_no_watch() -> None:
    assert not watching_inactives([], KICKOFF)


class _Destination:
    def __init__(self) -> None:
        self.posts: list[list[discord.Embed]] = []

    async def send(self, *, embeds, file=None) -> None:
        self.posts.append(embeds)


class _Espn:
    def __init__(self, games: list[Game], reports: list[InactiveReport]) -> None:
        self.games = games
        self.reports = reports
        self.schedule_calls = 0
        self.inactive_calls = 0

    async def fetch_schedule(self, window) -> list[Game]:
        self.schedule_calls += 1
        return list(self.games)

    async def fetch_inactives(self, target_date) -> list[InactiveReport]:
        self.inactive_calls += 1
        return list(self.reports)


def _bot(tmp_path, espn: _Espn, destination: _Destination) -> RavensBot:
    bot = RavensBot(
        BotConfig(
            discord_token="token",
            discord_channel_ids=(123,),
            discord_webhook_urls=(),
            poll_interval_seconds=300,
            time_zone=EASTERN,
            state_file=str(tmp_path / "state.json"),
        )
    )
    bot.espn = espn
    target = _AnnouncementTarget("123", "channel 123", destination)

    async def targets() -> list[_AnnouncementTarget]:
        return [target]

    bot._announcement_targets = targets
    return bot


def _report() -> InactiveReport:
    return InactiveReport(
        game=_game(),
        players=(
            InactivePlayer("Zay Flowers", "Baltimore Ravens", "Knee", "4361050", "WR"),
        ),
    )


def test_the_watch_reads_lists_inside_the_window(tmp_path, monkeypatch) -> None:
    espn = _Espn([_game()], [_report()])
    destination = _Destination()
    bot = _bot(tmp_path, espn, destination)
    monkeypatch.setattr(
        "ravens_bot.bot.now_in_zone", lambda zone: KICKOFF - timedelta(minutes=89)
    )

    asyncio.run(bot.watch_inactives())

    assert espn.inactive_calls == 1
    assert len(destination.posts) == 1
    # A list already posted is not posted again on the next look.
    asyncio.run(bot.watch_inactives())
    assert len(destination.posts) == 1


def test_outside_the_window_only_the_schedule_is_read(tmp_path, monkeypatch) -> None:
    espn = _Espn([_game()], [_report()])
    bot = _bot(tmp_path, espn, _Destination())
    monkeypatch.setattr(
        "ravens_bot.bot.now_in_zone", lambda zone: KICKOFF - timedelta(hours=6)
    )

    asyncio.run(bot.watch_inactives())

    assert espn.inactive_calls == 0
    assert bot._idle_inactive_ticks == INACTIVE_IDLE_TICKS

    # The idle ticks are spent before the schedule is read again.
    asyncio.run(bot.watch_inactives())
    assert espn.schedule_calls == 1


def test_the_watch_backs_off_when_espn_cannot_be_reached(tmp_path, monkeypatch) -> None:
    espn = _Espn([_game()], [_report()])

    async def failing(window):
        raise EspnApiError("ESPN did not respond")

    espn.fetch_schedule = failing
    bot = _bot(tmp_path, espn, _Destination())
    monkeypatch.setattr("ravens_bot.bot.now_in_zone", lambda zone: KICKOFF)

    asyncio.run(bot.watch_inactives())

    assert bot._idle_inactive_ticks == INACTIVE_IDLE_TICKS


def test_the_watch_looks_once_a_minute(tmp_path) -> None:
    assert INACTIVE_WATCH_INTERVAL_SECONDS == 60
    # Idling covers the five minutes the general poll used to take.
    assert (INACTIVE_IDLE_TICKS + 1) * INACTIVE_WATCH_INTERVAL_SECONDS == 300
