from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from ravens_bot.embeds import fourth_down_embed
from ravens_bot.formatting import format_elapsed, format_recalled_fourth_down
from ravens_bot.fourthdown import advise
from ravens_bot.models import Game, GameSituation, GameTeam, TeamRef
from ravens_bot.recall import RECALL_TTL_SECONDS, FourthDownMemory


RAVENS = TeamRef(team_id="33", name="Baltimore Ravens", abbreviation="BAL", slug="bal")
BROWNS = TeamRef(team_id="5", name="Cleveland Browns", abbreviation="CLE", slug="cle")
JETS = TeamRef(team_id="20", name="New York Jets", abbreviation="NYJ", slug="nyj")
BILLS = TeamRef(team_id="2", name="Buffalo Bills", abbreviation="BUF", slug="buf")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def situation(down: int = 4, distance: int = 2, yards_to_goal: int = 45) -> GameSituation:
    return GameSituation(
        possession=RAVENS,
        defense=BROWNS,
        down=down,
        distance=distance,
        yards_to_goal=yards_to_goal,
        period=2,
        clock="4:31",
    )


def build_game(
    event_id: str = "401",
    home: TeamRef = RAVENS,
    away: TeamRef = BROWNS,
    game_situation: GameSituation | None = None,
) -> Game:
    return Game(
        event_id=event_id,
        name=f"{away.name} at {home.name}",
        short_name=f"{away.abbreviation} @ {home.abbreviation}",
        start_time=datetime(2025, 11, 23, 18, 0, tzinfo=timezone.utc),
        status="2nd Quarter",
        home=GameTeam(team=home, is_home=True, score=10),
        away=GameTeam(team=away, score=7),
        state="in",
        situation=game_situation,
    )


def test_a_fourth_down_is_remembered_after_the_play() -> None:
    clock = FakeClock()
    memory = FourthDownMemory(clock=clock)
    memory.remember([build_game(game_situation=situation())])

    clock.advance(120)
    memory.remember([build_game(game_situation=situation(down=1, distance=10))])

    remembered = memory.recall("401")
    assert remembered is not None
    assert remembered.situation.down == 4
    assert remembered.age_seconds == 120


def test_a_repeated_down_keeps_the_time_it_first_appeared() -> None:
    clock = FakeClock()
    memory = FourthDownMemory(clock=clock)
    memory.remember([build_game(game_situation=situation())])

    clock.advance(30)
    memory.remember([build_game(game_situation=situation())])

    remembered = memory.recall("401")
    assert remembered is not None
    assert remembered.age_seconds == 30


def test_a_new_fourth_down_replaces_the_last_one() -> None:
    clock = FakeClock()
    memory = FourthDownMemory(clock=clock)
    memory.remember([build_game(game_situation=situation(distance=2))])

    clock.advance(300)
    memory.remember([build_game(game_situation=situation(distance=9))])

    remembered = memory.recall("401")
    assert remembered is not None
    assert remembered.situation.distance == 9
    assert remembered.age_seconds == 0


def test_nothing_is_remembered_from_a_down_that_is_not_fourth() -> None:
    memory = FourthDownMemory()
    memory.remember([build_game(game_situation=situation(down=3))])
    memory.remember([build_game(event_id="402")])

    assert memory.recall("401") is None
    assert memory.recall("402") is None
    assert memory.latest() is None


def test_a_stale_down_is_forgotten() -> None:
    clock = FakeClock()
    memory = FourthDownMemory(clock=clock)
    memory.remember([build_game(game_situation=situation())])

    clock.advance(RECALL_TTL_SECONDS)

    assert memory.recall("401") is None
    assert memory.games() == []


def test_the_ravens_win_the_latest_recall() -> None:
    clock = FakeClock()
    memory = FourthDownMemory(clock=clock)
    memory.remember([build_game(game_situation=situation())])

    clock.advance(60)
    other = build_game(
        event_id="402",
        home=JETS,
        away=BILLS,
        game_situation=replace(situation(), possession=JETS, defense=BILLS),
    )
    memory.remember([other])

    latest = memory.latest()
    assert latest is not None
    assert latest.game.event_id == "401"
    assert memory.latest(prefer_ravens=False) is not None
    assert memory.latest(prefer_ravens=False).game.event_id == "402"


def test_remembered_games_are_freshest_first() -> None:
    clock = FakeClock()
    memory = FourthDownMemory(clock=clock)
    memory.remember([build_game(game_situation=situation())])
    clock.advance(60)
    memory.remember(
        [
            build_game(
                event_id="402",
                home=JETS,
                away=BILLS,
                game_situation=replace(situation(), possession=JETS, defense=BILLS),
            )
        ]
    )

    assert [game.event_id for game in memory.games()] == ["402", "401"]


def test_only_so_many_games_are_kept() -> None:
    clock = FakeClock()
    memory = FourthDownMemory(max_games=2, clock=clock)
    for index in range(3):
        clock.advance(10)
        memory.remember([build_game(event_id=str(index), game_situation=situation())])

    assert memory.recall("0") is None
    assert memory.recall("1") is not None
    assert memory.recall("2") is not None


def test_elapsed_reads_as_a_person_would_say_it() -> None:
    assert format_elapsed(20) == "moments ago"
    assert format_elapsed(60) == "1 minute ago"
    assert format_elapsed(605) == "10 minutes ago"
    assert format_elapsed(3600) == "1 hour ago"
    assert format_elapsed(7500) == "2 hours 5 min ago"


def test_a_recalled_embed_says_the_play_is_over() -> None:
    game = build_game(game_situation=situation())
    advice = advise(situation())

    embed = fourth_down_embed(game, advice, age_seconds=90)

    assert embed.description is not None
    assert format_recalled_fourth_down(90) in embed.description
    assert "1 minute ago" in embed.description
    assert fourth_down_embed(game, advice).description is not None
    assert "The play is over" not in (fourth_down_embed(game, advice).description or "")
