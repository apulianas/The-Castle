from __future__ import annotations

import asyncio
import io
from datetime import date
from zoneinfo import ZoneInfo

import discord
from PIL import Image

from ravens_bot.bot import RavensBot, _AnnouncementTarget
from ravens_bot.config import BotConfig
from ravens_bot.embeds import INACTIVE_CHART_FILENAME

from ravens_bot.inactives_report import (
    NO_INACTIVES_ROW,
    artwork_urls,
    chart_sections,
    render_inactive_report,
)
from ravens_bot.models import (
    Game,
    GameTeam,
    InactivePlayer,
    InactiveReport,
    TeamRef,
)


RAVENS = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
BROWNS = TeamRef("Cleveland Browns", "5", "CLE", "cle")


def _report(players: tuple[InactivePlayer, ...]) -> InactiveReport:
    game = Game(
        "401",
        "Baltimore Ravens at Cleveland Browns",
        "BAL @ CLE",
        None,
        "Pre-Game",
        home=GameTeam(BROWNS, is_home=True),
        away=GameTeam(RAVENS),
    )
    return InactiveReport(game=game, players=players)


PLAYERS = (
    InactivePlayer("Zay Flowers", "Baltimore Ravens", "Knee", "4361050", "WR", True),
    InactivePlayer("Myles Garrett", "CLE", "Healthy scratch", "3121023", "DE"),
)


def test_sections_follow_the_matchup_with_no_headings() -> None:
    sections = chart_sections(_report(PLAYERS))

    assert [section.title for section in sections] == [
        "Baltimore Ravens",
        "Cleveland Browns",
    ]
    assert [section.headers for section in sections] == [(), ()]


def test_rows_put_position_and_name_in_one_column() -> None:
    sections = chart_sections(_report(PLAYERS))

    assert sections[0].rows == (("WR Zay Flowers", "Knee"),)
    assert sections[1].rows == (("DE Myles Garrett", "Healthy scratch"),)


def test_a_missing_reason_reads_as_a_dash() -> None:
    report = _report(
        (InactivePlayer("Zay Flowers", "Baltimore Ravens", None, None, "WR", True),)
    )

    assert chart_sections(report)[0].rows == (("WR Zay Flowers", "-"),)


def test_a_team_without_inactives_says_so_rather_than_showing_nothing() -> None:
    sections = chart_sections(_report(PLAYERS[:1]))

    assert sections[1].rows == ((NO_INACTIVES_ROW, "-"),)
    assert sections[1].headshots == (None,)


def test_players_from_an_unnamed_club_keep_their_own_section() -> None:
    report = _report(
        (*PLAYERS, InactivePlayer("Someone Else", "Chicago Bears", None, None, "LB"))
    )

    sections = chart_sections(report)

    assert [section.title for section in sections][-1] == "Chicago Bears"
    assert sections[-1].rows == (("LB Someone Else", "-"),)


def test_artwork_covers_logos_and_headshots_without_repeats() -> None:
    urls = artwork_urls(_report(PLAYERS))

    assert len(urls) == len(set(urls))
    assert any("teamlogos/nfl/500/bal.png" in url for url in urls)
    assert any("4361050" in url for url in urls)


def test_render_produces_a_shareable_png_without_artwork() -> None:
    rendered = render_inactive_report(_report(PLAYERS))

    image = Image.open(io.BytesIO(rendered))
    assert image.format == "PNG"
    assert image.width > 0 and image.height > 0


def test_render_tolerates_artwork_that_is_not_an_image() -> None:
    report = _report(PLAYERS)
    artwork = {url: b"not an image" for url in artwork_urls(report)}

    assert render_inactive_report(report, artwork)


class _Destination:
    def __init__(self) -> None:
        self.posts: list[tuple[list[discord.Embed], discord.File | None]] = []

    async def send(
        self,
        *,
        embeds: list[discord.Embed],
        file: discord.File | None = None,
    ) -> None:
        self.posts.append((embeds, file))


def _bot(tmp_path) -> RavensBot:
    return RavensBot(
        BotConfig(
            discord_token="token",
            discord_channel_ids=(123,),
            discord_webhook_urls=(),
            poll_interval_seconds=300,
            time_zone=ZoneInfo("America/New_York"),
            state_file=str(tmp_path / "state.json"),
        )
    )


def test_announced_inactives_carry_the_chart_once(tmp_path) -> None:
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path)
    report = _report(PLAYERS)

    asyncio.run(bot._post_new_inactives([target], [report], date(2025, 11, 23)))
    asyncio.run(bot._post_new_inactives([target], [report], date(2025, 11, 23)))

    assert len(destination.posts) == 1
    embeds, file = destination.posts[0]
    assert file is not None
    assert file.filename == INACTIVE_CHART_FILENAME
    assert embeds[0].image.url == f"attachment://{INACTIVE_CHART_FILENAME}"
    assert embeds[0].fields == []


def test_a_chart_that_cannot_be_drawn_still_posts_the_written_list(
    tmp_path, monkeypatch
) -> None:
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path)

    monkeypatch.setattr(
        "ravens_bot.bot.render_inactive_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no fonts")),
    )
    asyncio.run(
        bot._post_new_inactives([target], [_report(PLAYERS)], date(2025, 11, 23))
    )

    embeds, file = destination.posts[0]
    assert file is None
    assert [field.name for field in embeds[0].fields] == ["Ravens (1)", "CLE (1)"]
