from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ravens_bot.bot import RavensBot
from ravens_bot.models import Game, InactivePlayer, InactiveReport
from ravens_bot.official_inactives import (
    INACTIVES_URL,
    OfficialInactivesClient,
    OfficialInactivesError,
    article_matches,
    merge_official_inactives,
    parse_inactive_names,
)


DATA = Path(__file__).parent / "data"
LISTING = (DATA / "ravens_inactives_listing.html").read_text()
ARTICLE = (DATA / "ravens_inactives_article.html").read_text()
EXPECTED = (
    "Zay Flowers",
    "Nnamdi Madubuike",
    "Teddye Buchanan",
    "Andrew Vorhees",
    "Joe Fagnano",
    "Gerad Lichtenhan",
    "T.J. Tampa",
)


class _Response:
    def __init__(self, page: str) -> None:
        self._page = page

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def text(self) -> str:
        return self._page


class _Session:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> _Response:
        self.requested.append(url)
        return _Response(self.pages[url])


ARTICLE_URL = "https://www.baltimoreravens.com/news/ravens-inactives-week-3-at-browns-1234"


def test_the_article_names_every_inactive_with_its_position() -> None:
    players = parse_inactive_names(ARTICLE)

    assert tuple(player.name for player in players) == EXPECTED
    assert players[0].position == "WR"
    assert all(player.is_ravens for player in players)
    assert all(player.reason is None for player in players)


def test_prose_around_the_list_does_not_become_a_player() -> None:
    assert "John Harbaugh" not in {
        player.name for player in parse_inactive_names(ARTICLE)
    }


def test_the_game_day_post_is_the_one_read() -> None:
    session = _Session({INACTIVES_URL: LISTING, ARTICLE_URL: ARTICLE})
    client = OfficialInactivesClient(session)  # type: ignore[arg-type]

    players = asyncio.run(client.fetch_inactives(date(2026, 9, 13), "Week 3"))

    assert tuple(player.name for player in players) == EXPECTED
    assert session.requested == [INACTIVES_URL, ARTICLE_URL]


def test_a_day_without_a_post_finds_nobody() -> None:
    session = _Session({INACTIVES_URL: LISTING})
    client = OfficialInactivesClient(session)  # type: ignore[arg-type]

    assert asyncio.run(client.fetch_inactives(date(2026, 10, 25), "Week 8")) == ()


def test_a_page_that_does_not_parse_is_an_error_not_a_crash() -> None:
    session = _Session({INACTIVES_URL: "<html><body>maintenance</body></html>"})
    client = OfficialInactivesClient(session)  # type: ignore[arg-type]

    assert asyncio.run(client.fetch_inactives(date(2026, 9, 13), "Week 3")) == ()


def test_a_headline_week_stands_in_for_a_missing_stamp() -> None:
    assert article_matches(
        "Ravens Inactives: Week 3 at Browns", None, date(2026, 9, 13), "Week 3"
    )
    assert not article_matches(
        "Ravens Inactives: Week 4 at Chiefs", None, date(2026, 9, 13), "Week 3"
    )


def test_an_unrelated_day_is_not_close_enough_to_match() -> None:
    assert not article_matches(
        "Ravens Inactives", date(2026, 9, 6), date(2026, 9, 13), "Week 3"
    )


GAME = Game(
    "401872939",
    "Baltimore Ravens at Cleveland Browns",
    "BAL @ CLE",
    datetime(2026, 9, 13, 17, tzinfo=timezone.utc),
    "Final",
    completed=True,
    week="Week 3",
)


def test_merging_keeps_espn_s_reasons_and_adds_the_names_it_missed() -> None:
    espn = InactiveReport(
        game=GAME,
        players=(InactivePlayer(name="Zay Flowers", reason="Hamstring", is_ravens=True),),
    )

    merged = merge_official_inactives(espn, parse_inactive_names(ARTICLE))

    assert merged.players[0].reason == "Hamstring"
    assert len(merged.players) == len(EXPECTED)
    assert [player.name for player in merged.players].count("Zay Flowers") == 1


def _bot() -> RavensBot:
    return RavensBot.__new__(RavensBot)


def test_the_club_page_is_only_read_when_espn_reports_nothing() -> None:
    bot = _bot()
    calls: list[date] = []

    class _Client:
        async def fetch_inactives(self, target_date, week=None):
            calls.append(target_date)
            return parse_inactive_names(ARTICLE)

    bot.official_inactives = _Client()  # type: ignore[assignment]
    bot.config = type("C", (), {"time_zone": timezone.utc})()
    full = InactiveReport(
        game=GAME, players=(InactivePlayer(name="Zay Flowers", is_ravens=True),)
    )

    assert asyncio.run(bot._with_official_inactives(full)) is full
    assert calls == []

    empty = InactiveReport(game=GAME, players=())
    filled = asyncio.run(bot._with_official_inactives(empty))

    assert calls == [date(2026, 9, 13)]
    assert tuple(player.name for player in filled.players) == EXPECTED


def test_a_club_page_outage_leaves_espn_s_answer_alone(caplog) -> None:
    bot = _bot()

    class _Client:
        async def fetch_inactives(self, target_date, week=None):
            raise OfficialInactivesError("down")

    bot.official_inactives = _Client()  # type: ignore[assignment]
    bot.config = type("C", (), {"time_zone": timezone.utc})()
    empty = InactiveReport(game=GAME, players=())

    assert asyncio.run(bot._with_official_inactives(empty)) == empty
    assert "Official inactives unavailable" in caplog.text
