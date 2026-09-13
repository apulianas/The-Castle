from __future__ import annotations

import asyncio
import io
from zoneinfo import ZoneInfo

import discord
from PIL import Image

from ravens_bot.bot import RavensBot, _AnnouncementTarget
from ravens_bot.config import BotConfig
from ravens_bot.injury_report import (
    InjuryTable,
    OfficialInjuryReport,
    OfficialReportGate,
    _contrasting_text_color,
    _display_header,
    _font,
    add_matchup,
    parse_injury_report,
    MAX_IMAGE_WIDTH,
    MIN_IMAGE_WIDTH,
    render_injury_report,
)
from ravens_bot.models import Game, GameTeam, TeamRef


PAGE = """
<select>
  <option value="/team/injury-report/week/REG-2" selected> WEEK 2 </option>
  <option value="/team/injury-report/week/REG-1"> WEEK 1 </option>
</select>
<div class="nfl-o-injury-report__title">
  <source data-srcset="https://images.example/ravens-logo.png">
  <span class="nfl-o-injury-report__club-name">Baltimore Ravens</span>
</div>
<table>
  <thead><tr><th>Player</th><th>Position</th><th>Injury</th><th>Wed</th>
    <th>Thu</th><th>Fri</th><th>Game Status</th></tr></thead>
  <tbody>
    <tr><td><a><source data-srcset="https://images.example/zay.png 1x,
      https://images.example/zay-3x.png 3x">
      Zay Flowers</a></td><td>WR</td><td>Knee</td>
      <td>LP</td><td>FP</td><td>FP</td><td>QUESTIONABLE</td></tr>
  </tbody>
</table>
<div class="nfl-o-injury-report__title">
  <source data-srcset="https://images.example/browns-logo.png">
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
        "FP",
        "QUESTIONABLE",
    )
    assert report.tables[0].headshots == ("https://images.example/zay-3x.png",)
    assert report.tables[0].logo_url == "https://images.example/ravens-logo.png"
    assert report.teams_are_synchronized


def test_report_is_not_synchronized_until_both_teams_publish_the_same_day() -> None:
    delayed = parse_injury_report(PAGE.replace("<td>FP</td><td>-</td></tr>", "<td></td><td>-</td></tr>"))

    assert not delayed.teams_are_synchronized


def test_report_gate_waits_five_quiet_minutes_after_both_teams_update() -> None:
    now = [1000.0]
    gate = OfficialReportGate(clock=lambda: now[0])
    report = parse_injury_report(PAGE)

    assert not gate.ready(report)
    now[0] += 299
    assert not gate.ready(report)
    now[0] += 1
    assert gate.ready(report)


def test_report_gate_restarts_wait_when_the_chart_changes() -> None:
    now = [1000.0]
    gate = OfficialReportGate(clock=lambda: now[0])
    report = parse_injury_report(PAGE)
    changed = parse_injury_report(PAGE.replace("QUESTIONABLE", "OUT"))

    assert not gate.ready(report)
    now[0] += 240
    assert not gate.ready(changed)
    now[0] += 60
    assert not gate.ready(changed)
    now[0] += 240
    assert gate.ready(changed)


def test_add_matchup_uses_schedule_order_and_team_colors() -> None:
    report = parse_injury_report(PAGE)
    game = Game(
        event_id="1",
        name="Baltimore Ravens at New Orleans Saints",
        short_name="BAL @ NO",
        start_time=None,
        status="Scheduled",
        away=GameTeam(
            TeamRef(
                name="New Orleans Saints",
                team_id="18",
                abbreviation="NO",
                color="#d3bc8d",
            )
        ),
        home=GameTeam(
            TeamRef(
                name="Baltimore Ravens",
                team_id="33",
                abbreviation="BAL",
                color="#000000",
            ),
            is_home=True,
        ),
        season_type=2,
        week_number=2,
    )

    resolved = add_matchup(report, [game])

    assert resolved.title == "Saints @ Ravens Injury Report | Week 2"
    assert resolved.matchup is not None
    assert resolved.matchup.away_color == "#d3bc8d"
    assert resolved.matchup.home_color == "#24125f"
    assert resolved.matchup.away_team == "New Orleans Saints"
    assert resolved.matchup.home_team == "Baltimore Ravens"


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
    # The chart is read on a phone, so it is only as wide as its columns need
    # and never wider than a page a phone can show without shrinking the type.
    assert MIN_IMAGE_WIDTH <= image.width <= MAX_IMAGE_WIDTH
    assert image.height > 300


def test_report_uses_bundled_d_din_fonts() -> None:
    assert _font(24).path.name == "D-DIN.ttf"
    assert _font(24, bold=True).path.name == "D-DIN-Bold.ttf"


def test_report_display_abbreviates_position_without_changing_data() -> None:
    report = parse_injury_report(PAGE)
    table = report.tables[0]

    assert _display_header("Position") == "Pos"
    assert table.headers[1] == "Position"
    assert table.rows[0][-1] == "QUESTIONABLE"


def test_report_header_text_contrasts_with_team_color() -> None:
    assert _contrasting_text_color("#24125f") == "#ffffff"
    assert _contrasting_text_color("#d3bc8d") == "#111111"


def test_render_injury_report_fits_columns_to_their_contents() -> None:
    """A practice-status column holding "LP" should not be as wide as a name."""
    long_names = InjuryTable(
        team="Baltimore Ravens",
        headers=("Player", "Position", "Injury", "Wed", "Thu", "Fri", "Game Status"),
        rows=tuple(
            (
                f"Bartholomew Widereceiverson {index}",
                "WR",
                "Hamstring / Shoulder / Knee",
                "LP",
                "FP",
                "FP",
                "QUESTIONABLE",
            )
            for index in range(3)
        ),
    )
    short_names = InjuryTable(
        team="Baltimore Ravens",
        headers=long_names.headers,
        rows=(("Zay Flowers", "WR", "Knee", "LP", "FP", "FP", "QUESTIONABLE"),),
    )

    wide = Image.open(io.BytesIO(render_injury_report(
        OfficialInjuryReport(week="Week 2", tables=(long_names,))
    )))
    narrow = Image.open(io.BytesIO(render_injury_report(
        OfficialInjuryReport(week="Week 2", tables=(short_names,))
    )))

    # Longer entries need more room, but never more than a phone can show.
    assert narrow.width < wide.width <= MAX_IMAGE_WIDTH
    assert narrow.width >= MIN_IMAGE_WIDTH


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
    assert embed.title == "Ravens Injury Report | Week 2"
    assert embed.url == (
        "https://www.baltimoreravens.com/team/injury-report/week/REG-2"
    )
    assert embed.image.url == "attachment://ravens-injury-report.png"
    assert file.filename == "ravens-injury-report.png"
