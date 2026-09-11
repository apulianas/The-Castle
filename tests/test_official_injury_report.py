from __future__ import annotations

import asyncio
import io
from zoneinfo import ZoneInfo

import discord
from PIL import Image

from ravens_bot.bot import RavensBot, _AnnouncementTarget
from ravens_bot.config import BotConfig
from ravens_bot.injury_report import (
    parse_injury_report,
    render_injury_report,
)


PAGE = """
<select>
  <option value="/team/injury-report/week/REG-2" selected> WEEK 2 </option>
  <option value="/team/injury-report/week/REG-1"> WEEK 1 </option>
</select>
<div class="nfl-o-injury-report__title">
  <span class="nfl-o-injury-report__club-name">Baltimore Ravens</span>
</div>
<table>
  <thead><tr><th>Player</th><th>Position</th><th>Injury</th><th>Wed</th>
    <th>Thu</th><th>Fri</th><th>Game Status</th></tr></thead>
  <tbody>
    <tr><td><a>Zay Flowers</a></td><td>WR</td><td>Knee</td>
      <td>LP</td><td>FP</td><td></td><td>QUESTIONABLE</td></tr>
  </tbody>
</table>
<div class="nfl-o-injury-report__title">
  <span class="nfl-o-injury-report__club-name">Cleveland Browns</span>
</div>
<table>
  <thead><tr><th>Player</th><th>Position</th><th>Injury</th><th>Wed</th>
    <th>Thu</th><th>Fri</th><th>Game Status</th></tr></thead>
  <tbody>
    <tr><td>Myles Garrett</td><td>DE</td><td>Foot</td>
      <td>DNP</td><td>LP</td><td>FP</td><td>-</td></tr>
  </tbody>
</table>
"""


def test_parse_injury_report_reads_week_teams_and_rows() -> None:
    report = parse_injury_report(PAGE)

    assert report.week == "WEEK 2"
    assert [table.team for table in report.tables] == [
        "Baltimore Ravens",
        "Cleveland Browns",
    ]
    assert report.tables[0].rows[0] == (
        "Zay Flowers",
        "WR",
        "Knee",
        "LP",
        "FP",
        "-",
        "QUESTIONABLE",
    )


def test_report_key_changes_when_any_cell_or_week_changes() -> None:
    original = parse_injury_report(PAGE)
    changed_cell = parse_injury_report(PAGE.replace("QUESTIONABLE", "OUT"))
    changed_week = parse_injury_report(PAGE.replace("WEEK 2", "WEEK 3", 1))

    assert original.announcement_key != changed_cell.announcement_key
    assert original.announcement_key != changed_week.announcement_key


def test_render_injury_report_produces_a_shareable_png() -> None:
    rendered = render_injury_report(parse_injury_report(PAGE))

    image = Image.open(io.BytesIO(rendered))

    assert image.format == "PNG"
    assert image.width == 1400
    assert image.height > 300


class _Destination:
    def __init__(self) -> None:
        self.posts: list[tuple[discord.Embed, discord.File]] = []

    async def send(self, *, embed: discord.Embed, file: discord.File) -> None:
        self.posts.append((embed, file))


def test_automatic_report_posts_once_per_chart_version(tmp_path) -> None:
    report = parse_injury_report(PAGE)
    correction = parse_injury_report(PAGE.replace("QUESTIONABLE", "OUT"))
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = RavensBot(
        BotConfig(
            discord_token="token",
            discord_channel_ids=(123,),
            discord_webhook_urls=(),
            poll_interval_seconds=300,
            time_zone=ZoneInfo("America/New_York"),
            state_file=str(tmp_path / "state.json"),
        )
    )

    asyncio.run(bot._post_official_injury_report([target], report))
    asyncio.run(bot._post_official_injury_report([target], report))
    asyncio.run(bot._post_official_injury_report([target], correction))
    asyncio.run(bot._post_official_injury_report([target], report))

    # A correction that restores a previously seen chart is still an update.
    assert len(destination.posts) == 3
    embed, file = destination.posts[0]
    assert embed.title == "Ravens injury report — Week 2"
    assert embed.image.url == "attachment://ravens-injury-report.png"
    assert file.filename == "ravens-injury-report.png"
