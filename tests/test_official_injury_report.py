from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest
from PIL import Image

from ravens_bot.bot import RavensBot, _AnnouncementTarget, _official_injury_slot
from ravens_bot.config import BotConfig
from ravens_bot.injury_report import (
    InjuryTable,
    OfficialInjuryReport,
    _contrasting_text_color,
    _display_header,
    _draw_headshot,
    _font,
    add_matchup,
    is_scheduled_report_date,
    parse_injury_report,
    practice_report_date,
    MAX_IMAGE_WIDTH,
    MIN_IMAGE_WIDTH,
    OUTPUT_SCALE,
    render_injury_report,
)
from ravens_bot.models import Game, GameTeam, InjuryReport, TeamRef


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


def test_report_date_follows_the_latest_column_for_a_sunday_game() -> None:
    report = add_matchup(
        parse_injury_report(PAGE),
        [
            Game(
                "1",
                "Baltimore Ravens at Cleveland Browns",
                "BAL @ CLE",
                datetime(2025, 9, 14, 17, 0, tzinfo=timezone.utc),
                "Scheduled",
                away=GameTeam(TeamRef("Baltimore Ravens", "33", "BAL")),
                home=GameTeam(TeamRef("Cleveland Browns", "5", "CLE"), is_home=True),
                season_type=2,
                week_number=2,
            )
        ],
    )

    assert is_scheduled_report_date(
        report, date(2025, 9, 12), ZoneInfo("America/New_York")
    )
    assert not is_scheduled_report_date(
        report, date(2025, 9, 13), ZoneInfo("America/New_York")
    )


def test_report_date_shifts_with_a_monday_game() -> None:
    report = add_matchup(
        parse_injury_report(
            PAGE.replace("<th>Fri</th>", "<th>Sat</th>")
            .replace("<th>Thu</th>", "<th>Fri</th>")
            .replace("<th>Wed</th>", "<th>Thu</th>")
        ),
        [
            Game(
                "1",
                "Baltimore Ravens at Cleveland Browns",
                "BAL @ CLE",
                datetime(2025, 9, 16, 0, 15, tzinfo=timezone.utc),
                "Scheduled",
                away=GameTeam(TeamRef("Baltimore Ravens", "33", "BAL")),
                home=GameTeam(TeamRef("Cleveland Browns", "5", "CLE"), is_home=True),
                season_type=2,
                week_number=2,
            )
        ],
    )

    assert is_scheduled_report_date(
        report, date(2025, 9, 13), ZoneInfo("America/New_York")
    )


def test_report_date_shifts_with_a_thursday_game() -> None:
    report = add_matchup(
        parse_injury_report(
            PAGE.replace("<th>Wed</th>", "<th>Mon</th>")
            .replace("<th>Thu</th>", "<th>Tue</th>")
            .replace("<th>Fri</th>", "<th>Wed</th>")
        ),
        [
            Game(
                "1",
                "Baltimore Ravens at Cleveland Browns",
                "BAL @ CLE",
                datetime(2025, 9, 19, 0, 15, tzinfo=timezone.utc),
                "Scheduled",
                away=GameTeam(TeamRef("Baltimore Ravens", "33", "BAL")),
                home=GameTeam(TeamRef("Cleveland Browns", "5", "CLE"), is_home=True),
                season_type=2,
                week_number=2,
            )
        ],
    )

    assert is_scheduled_report_date(
        report, date(2025, 9, 17), ZoneInfo("America/New_York")
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
    # The chart is read on a phone, so it is only as wide as its columns need
    # and never wider than a page a phone can show without shrinking the type.
    assert MIN_IMAGE_WIDTH * OUTPUT_SCALE <= image.width <= MAX_IMAGE_WIDTH * OUTPUT_SCALE
    assert image.height > 300 * OUTPUT_SCALE


def test_report_uses_bundled_d_din_fonts() -> None:
    assert _font(24).path.name == "D-DIN.ttf"
    assert _font(24, bold=True).path.name == "D-DIN-Bold.ttf"


def test_headshot_preserves_transparent_background() -> None:
    source = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    source.putpixel((10, 10), (0, 0, 255, 255))
    encoded = io.BytesIO()
    source.save(encoded, format="PNG")
    canvas = Image.new("RGB", (20, 20), (255, 0, 0))

    _draw_headshot(canvas, encoded.getvalue(), 0, 0, 20)

    assert canvas.getpixel((0, 0)) == (255, 0, 0)
    assert canvas.getpixel((10, 10))[2] > canvas.getpixel((10, 10))[0]


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
    assert narrow.width < wide.width <= MAX_IMAGE_WIDTH * OUTPUT_SCALE
    assert narrow.width >= MIN_IMAGE_WIDTH * OUTPUT_SCALE


class _Destination:
    def __init__(self) -> None:
        self.posts: list[tuple[discord.Embed, discord.File]] = []
        self.edits: list[tuple[int, discord.Embed, list[discord.File]]] = []

    async def send(self, *, embed: discord.Embed, file: discord.File):
        self.posts.append((embed, file))
        return SimpleNamespace(id=len(self.posts))

    def get_partial_message(self, message_id: int):
        async def edit(*, embed, attachments):
            self.edits.append((message_id, embed, attachments))
        return SimpleNamespace(edit=edit)


def _bot(tmp_path, destination) -> RavensBot:
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
    bot.get_partial_messageable = lambda _: destination
    bot._render_official_injury_report = AsyncMock(return_value=b"png")
    return bot


def test_automatic_report_edits_changes_and_posts_once_per_report_date(tmp_path) -> None:
    report = parse_injury_report(PAGE)
    correction = parse_injury_report(PAGE.replace("QUESTIONABLE", "OUT"))
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path, destination)
    first_date = date(2025, 9, 10)
    asyncio.run(bot._post_official_injury_report([target], report, first_date))
    asyncio.run(bot._post_official_injury_report([target], report, first_date))
    asyncio.run(bot._post_official_injury_report([target], correction, first_date))
    asyncio.run(
        bot._post_official_injury_report(
            [target], correction, first_date + timedelta(days=1)
        )
    )

    assert len(destination.posts) == 2
    assert len(destination.edits) == 1
    assert destination.edits[0][0] == 1
    assert len(destination.edits[0][2]) == 1
    assert bot._render_official_injury_report.await_count == 3
    embed, file = destination.posts[0]
    assert embed.title == "Ravens Injury Report | Week 2"
    assert embed.url == (
        "https://www.baltimoreravens.com/team/injury-report/week/REG-2"
    )
    assert embed.image.url == "attachment://ravens-injury-report.png"
    assert file.filename == "ravens-injury-report.png"


def _wednesday_report(*, partial: bool = False) -> OfficialInjuryReport:
    page = PAGE.replace("<td>FP</td>", "<td></td>").replace(
        "<td>DNP</td><td>LP</td>", "<td>DNP</td><td></td>"
    )
    if partial:
        page = page[:page.index('<div class="nfl-o-injury-report__title">', page.index("</table>"))]
    return add_matchup(
        parse_injury_report(page),
        [
            Game(
                "1", "Baltimore Ravens at Cleveland Browns", "BAL @ CLE",
                datetime(2026, 9, 20, 17, tzinfo=timezone.utc), "Scheduled",
                away=GameTeam(TeamRef("Baltimore Ravens", "33", "BAL")),
                home=GameTeam(TeamRef("Cleveland Browns", "5", "CLE"), is_home=True),
                season_type=2, week_number=2,
            )
        ],
    )


def _prepare_poll(bot, target, report, monkeypatch, today=date(2026, 9, 16)):
    monkeypatch.setattr("ravens_bot.bot.today_in_zone", lambda _: today)
    bot._announcement_targets = AsyncMock(return_value=[target])
    bot.injury_reports = SimpleNamespace(fetch=AsyncMock(return_value=report))
    bot._add_official_injury_matchup = AsyncMock(side_effect=lambda value: value)
    bot.espn = SimpleNamespace(
        fetch_transactions=AsyncMock(return_value=[]),
        fetch_injuries=AsyncMock(return_value=InjuryReport(())),
    )
    bot.official_transactions = SimpleNamespace(
        fetch_standard_elevations=AsyncMock(return_value=[]),
    )
    bot._post_new_roster_news = AsyncMock()


def test_first_poll_posts_partial_then_immediately_edits_complete_report(tmp_path, monkeypatch):
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path, destination)
    partial = _wednesday_report(partial=True)
    _prepare_poll(bot, target, partial, monkeypatch)

    asyncio.run(bot.poll_updates())
    assert len(destination.posts) == 1
    assert "Partial report" in destination.posts[0][0].description
    assert bot._post_new_roster_news.call_args.kwargs["scheduled_report_date"]

    complete = _wednesday_report()
    bot.injury_reports.fetch.return_value = complete
    asyncio.run(bot.poll_updates())
    assert len(destination.posts) == 1
    assert len(destination.edits) == 1
    assert destination.edits[0][1].description is None
    assert bot.announcement_state.is_current(
        _official_injury_slot(date(2026, 9, 16), "123"), complete.announcement_key
    )


def test_partial_changes_are_edited_before_the_opponent_arrives(tmp_path):
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path, destination)
    report = _wednesday_report(partial=True)
    changed = replace(
        report,
        tables=(replace(
            report.tables[0],
            rows=(("Zay Flowers", "WR", "Knee", "DNP", "-", "-", "-"),),
        ),),
    )
    report_date = date(2026, 9, 16)

    asyncio.run(bot._post_official_injury_report([target], report, report_date))
    asyncio.run(bot._post_official_injury_report([target], changed, report_date))

    assert len(destination.posts) == 1
    assert len(destination.edits) == 1
    assert "Partial report" in destination.edits[0][1].description


def test_first_poll_posts_even_when_only_the_opponent_has_published(tmp_path, monkeypatch):
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path, destination)
    report = _wednesday_report()
    report = replace(report, tables=report.tables[1:])
    _prepare_poll(bot, target, report, monkeypatch)
    asyncio.run(bot.poll_updates())
    assert len(destination.posts) == 1
    assert "Partial report" in destination.posts[0][0].description


def test_new_practice_day_posts_new_message_then_edits_it(tmp_path, monkeypatch):
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path, destination)
    wednesday = _wednesday_report()
    asyncio.run(bot._post_official_injury_report([target], wednesday, date(2026, 9, 16)))
    ravens, opponent = wednesday.tables
    thursday = replace(
        wednesday,
        tables=(replace(
            ravens, rows=(("Zay Flowers", "WR", "Knee", "LP", "DNP", "-", "-"),)
        ), opponent),
    )
    _prepare_poll(bot, target, thursday, monkeypatch, today=date(2026, 9, 17))
    asyncio.run(bot.poll_updates())
    assert len(destination.posts) == 2
    assert destination.edits == []
    assert "Partial report" in destination.posts[1][0].description
    complete = replace(
        thursday,
        tables=(thursday.tables[0], replace(
            opponent, rows=(("Myles Garrett", "DE", "Foot", "DNP", "LP", "-", "-"),)
        )),
    )
    bot.injury_reports.fetch.return_value = complete
    asyncio.run(bot.poll_updates())
    assert len(destination.posts) == 2
    assert destination.edits[0][0] == 2
    assert destination.edits[0][1].description is None


def test_saved_message_is_edited_after_restart(tmp_path):
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    report_date = date(2026, 9, 16)
    bot = _bot(tmp_path, destination)
    partial = _wednesday_report(partial=True)
    asyncio.run(bot._post_official_injury_report([target], partial, report_date))

    restarted = _bot(tmp_path, destination)
    restarted.announcement_state.load()
    asyncio.run(restarted._post_official_injury_report([target], partial, report_date))
    assert restarted._render_official_injury_report.await_count == 0
    asyncio.run(restarted._post_official_injury_report([target], _wednesday_report(), report_date))

    assert len(destination.posts) == 1
    assert destination.edits[0][0] == 1


def test_poll_edits_late_report_but_does_not_post_stale_report_to_new_target(tmp_path, monkeypatch):
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path, destination)
    asyncio.run(bot._post_official_injury_report(
        [target], _wednesday_report(partial=True), date(2026, 9, 16)
    ))
    _prepare_poll(bot, target, _wednesday_report(), monkeypatch, today=date(2026, 9, 17))
    new_destination = _Destination()
    bot._announcement_targets.return_value.append(
        _AnnouncementTarget("456", "channel 456", new_destination)
    )

    asyncio.run(bot.poll_updates())

    assert len(destination.posts) == 1
    assert len(destination.edits) == 1
    assert new_destination.posts == []


def test_poll_does_not_post_an_empty_or_future_practice_report(tmp_path, monkeypatch):
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path, destination)
    report = _wednesday_report(partial=True)
    empty = OfficialInjuryReport(
        report.week,
        (InjuryTable(report.tables[0].team, report.tables[0].headers, ()),),
        report.path, report.matchup,
    )
    _prepare_poll(bot, target, empty, monkeypatch)
    asyncio.run(bot.poll_updates())
    _prepare_poll(bot, target, report, monkeypatch, today=date(2026, 9, 15))
    asyncio.run(bot.poll_updates())
    assert destination.posts == []


def test_report_date_uses_newest_day_not_an_older_opponent_day():
    report = _wednesday_report()
    ravens, opponent = report.tables
    ravens = InjuryTable(
        ravens.team, ravens.headers,
        (("Zay Flowers", "WR", "Knee", "LP", "FP", "-", "-"),),
    )
    report = OfficialInjuryReport(report.week, (ravens, opponent), report.path, report.matchup)
    assert practice_report_date(report, ZoneInfo("America/New_York")) == date(2026, 9, 17)
    assert not is_scheduled_report_date(report, date(2026, 9, 16), ZoneInfo("America/New_York"))


def test_legacy_report_without_message_id_is_not_duplicated(tmp_path):
    destination = _Destination()
    target = _AnnouncementTarget("123", "channel 123", destination)
    bot = _bot(tmp_path, destination)
    report_date = date(2026, 9, 16)
    bot.announcement_state.mark_current(_official_injury_slot(report_date, "123"), "posted")
    asyncio.run(bot._post_official_injury_report([target], _wednesday_report(), report_date))
    assert destination.posts == []
    assert destination.edits == []


@pytest.mark.parametrize("webhook", [False, True])
def test_channel_and_webhook_replace_attachments_and_retry_failed_edits(tmp_path, webhook):
    destination = MagicMock(spec=discord.Webhook if webhook else discord.abc.Messageable)
    destination.send = AsyncMock(return_value=SimpleNamespace(id=987))
    edit = AsyncMock()
    destination.edit_message = edit
    channel = SimpleNamespace(get_partial_message=lambda _: SimpleNamespace(edit=edit))
    bot = _bot(tmp_path, channel)
    target = _AnnouncementTarget("webhook:123" if webhook else "123", "test target", destination)
    report_date = date(2026, 9, 16)
    partial = _wednesday_report(partial=True)
    complete = _wednesday_report()
    slot = _official_injury_slot(report_date, target.key_id)
    asyncio.run(bot._post_official_injury_report([target], partial, report_date))
    assert destination.send.call_args.kwargs.get("wait") is (True if webhook else None)
    assert bot.announcement_state.message_id(slot) == 987

    edit.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": "Missing Permissions"}
    )
    asyncio.run(bot._post_official_injury_report([target], complete, report_date))
    assert bot.announcement_state.is_current(slot, partial.announcement_key)
    assert destination.send.await_count == 1
    edit.side_effect = None
    asyncio.run(bot._post_official_injury_report([target], complete, report_date))
    assert edit.await_count == 2
    assert len(edit.call_args.kwargs["attachments"]) == 1
    assert edit.call_args.kwargs["attachments"][0].filename == "ravens-injury-report.png"
    assert bot.announcement_state.is_current(slot, complete.announcement_key)
    if webhook:
        assert edit.call_args.args == (987,)


@pytest.mark.parametrize("code,posts", [(10008, 2), (10015, 1)])
def test_only_deleted_message_is_replaced_not_a_missing_webhook(tmp_path, code, posts):
    destination = MagicMock(spec=discord.Webhook)
    destination.send = AsyncMock(return_value=SimpleNamespace(id=987))
    destination.edit_message = AsyncMock(side_effect=discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), {"code": code, "message": "Not found"}
    ))
    bot = _bot(tmp_path, destination)
    target = _AnnouncementTarget("webhook:123", "test webhook", destination)
    report_date = date(2026, 9, 16)
    partial = _wednesday_report(partial=True)
    complete = _wednesday_report()
    asyncio.run(bot._post_official_injury_report([target], partial, report_date))
    destination.send.return_value = SimpleNamespace(id=988)
    asyncio.run(bot._post_official_injury_report([target], complete, report_date))
    assert destination.send.await_count == posts
    slot = _official_injury_slot(report_date, target.key_id)
    assert bot.announcement_state.message_id(slot) == (988 if code == 10008 else 987)
    assert bot.announcement_state.is_current(
        slot, complete.announcement_key if code == 10008 else partial.announcement_key
    )


def test_failed_initial_send_is_not_marked_and_is_retried(tmp_path):
    destination = MagicMock(spec=discord.abc.Messageable)
    destination.send = AsyncMock(side_effect=discord.DiscordException("send failed"))
    bot = _bot(tmp_path, destination)
    target = _AnnouncementTarget("123", "test channel", destination)
    report_date = date(2026, 9, 16)
    report = _wednesday_report(partial=True)
    slot = _official_injury_slot(report_date, target.key_id)
    asyncio.run(bot._post_official_injury_report([target], report, report_date))
    assert bot.announcement_state.current_version(slot) is None
    assert bot.announcement_state.message_id(slot) is None
    destination.send.side_effect = None
    destination.send.return_value = SimpleNamespace(id=987)
    asyncio.run(bot._post_official_injury_report([target], report, report_date))
    assert destination.send.await_count == 2
    assert bot.announcement_state.message_id(slot) == 987
