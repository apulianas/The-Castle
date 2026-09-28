from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from PIL import Image

from ravens_bot.bot import RavensBot, _fourth_down_game
from ravens_bot.config import BotConfig
from ravens_bot.dates import WeekRequest

from ravens_bot.embeds import fourth_down_chart_embed, fourth_down_play_embed
from ravens_bot.espn import parse_fourth_downs
from ravens_bot.formatting import (
    format_fourth_down_needs_team,
    format_unknown_fourth_down,
)
from ravens_bot.fourthdowns_report import (
    CHART_HEADERS,
    NO_FOURTH_DOWNS_ROW,
    artwork_urls,
    chart_sections,
    find_team,
    recommendation_text,
    render_fourth_down_chart,
    situation_text,
)
from ravens_bot.models import (
    FOURTH_DOWN_FIELD_GOAL,
    FOURTH_DOWN_GO,
    FOURTH_DOWN_PUNT,
    Game,
    GameTeam,
    TeamRef,
)


RAVENS = TeamRef("Baltimore Ravens", "33", "BAL", "bal", logo="https://logo/bal")
BENGALS = TeamRef("Cincinnati Bengals", "4", "CIN", "cin", logo="https://logo/cin")
GAME = Game(
    "401",
    "Baltimore Ravens at Cincinnati Bengals",
    "BAL @ CIN",
    None,
    "Final",
    home=GameTeam(BENGALS, score=17, is_home=True),
    away=GameTeam(RAVENS, score=20),
    state="post",
    completed=True,
)


def _play(
    team: str,
    down: int,
    distance: int,
    yards_to_goal: int,
    play_type: str,
    text: str = "",
    period: int = 2,
    clock: str = "5:43",
    away_score: int = 0,
    home_score: int = 0,
    end: dict[str, Any] | None = None,
    stat_yardage: int | None = None,
    scoring: bool = False,
) -> dict[str, Any]:
    return {
        "start": {
            "down": down,
            "distance": distance,
            "yardsToEndzone": yards_to_goal,
            "team": {"id": team},
            "downDistanceText": f"{down}th & {distance}",
            "possessionText": "BAL 45",
        },
        "end": end or {},
        "period": {"number": period},
        "clock": {"displayValue": clock},
        "type": {"text": play_type},
        "text": text,
        "awayScore": away_score,
        "homeScore": home_score,
        "statYardage": stat_yardage,
        "scoringPlay": scoring,
    }


SUMMARY: dict[str, Any] = {
    "drives": {
        "previous": [
            {
                "team": {"id": "33"},
                "description": "8 plays, 45 yards",
                "displayResult": "Punt",
                "plays": [
                    _play("33", 1, 10, 75, "Pass Reception", "Jackson pass for 5"),
                    _play(
                        "33",
                        4,
                        2,
                        58,
                        "Punt",
                        "Koch punts 44 yards",
                        end={"team": {"id": "4"}, "down": 1},
                    ),
                ],
            },
            {
                "team": {"id": "4"},
                "description": "5 plays, 30 yards",
                "displayResult": "Field Goal Good",
                "plays": [
                    _play(
                        "4",
                        4,
                        6,
                        25,
                        "Field Goal Good",
                        "42 yard field goal is GOOD",
                        period=3,
                        clock="2:11",
                        end={"team": {"id": "33"}, "down": 1},
                    ),
                ],
            },
            {
                "team": {"id": "33"},
                "description": "6 plays, 70 yards",
                "displayResult": "Touchdown",
                "plays": [
                    _play(
                        "33",
                        4,
                        1,
                        40,
                        "Timeout",
                        "Timeout #1 by BAL",
                        period=4,
                        clock="1:12",
                        away_score=13,
                        home_score=17,
                    ),
                    _play(
                        "33",
                        4,
                        1,
                        40,
                        "Rush",
                        "Henry rush for 6 yards",
                        period=4,
                        clock="1:12",
                        away_score=13,
                        home_score=17,
                        end={"team": {"id": "33"}, "down": 1},
                        stat_yardage=6,
                    ),
                    _play(
                        "33",
                        4,
                        3,
                        8,
                        "Passing Touchdown",
                        "Jackson pass to Andrews for 8 yards, TOUCHDOWN",
                        period=4,
                        clock="0:31",
                        away_score=20,
                        home_score=17,
                        scoring=True,
                        end={"team": {"id": "4"}, "down": 1},
                    ),
                ],
            },
        ]
    }
}


def _report():
    return parse_fourth_downs(SUMMARY, GAME)


def test_every_fourth_down_is_read_back_in_the_order_it_was_played() -> None:
    report = _report()

    assert [(play.team.abbreviation, play.instance, play.drive) for play in report.plays] == [
        ("BAL", 1, 1),
        ("CIN", 1, 2),
        ("BAL", 2, 3),
        ("BAL", 3, 3),
    ]


def test_a_timeout_on_fourth_down_is_not_a_decision() -> None:
    assert all(play.choice != "no play" for play in _report().plays)
    assert all("Timeout" not in (play.play_text or "") for play in _report().plays)


def test_what_each_team_did_is_read_from_the_play() -> None:
    plays = _report().plays

    assert plays[0].choice == FOURTH_DOWN_PUNT
    assert plays[1].choice == FOURTH_DOWN_FIELD_GOAL
    assert plays[1].outcome == "good"
    assert plays[2].choice == FOURTH_DOWN_GO
    assert plays[2].outcome == "converted"
    assert plays[3].outcome == "touchdown"
    assert plays[3].actual == "Went for it — touchdown"


def test_the_score_is_the_one_the_down_was_faced_at() -> None:
    plays = _report().plays

    # The touchdown play carries 20-17, but it was snapped trailing by four.
    assert plays[3].situation.score_differential == -4
    assert plays[2].situation.score_differential == -4


def test_a_scoreless_first_quarter_down_is_read_as_tied() -> None:
    assert _report().plays[0].situation.score_differential == 0


def test_each_club_gets_a_section_with_its_own_numbered_rows() -> None:
    sections = chart_sections(_report())

    assert [section.title for section in sections] == [
        "Baltimore Ravens",
        "Cincinnati Bengals",
    ]
    assert all(section.headers == CHART_HEADERS for section in sections)
    assert [row[0] for row in sections[0].rows] == ["1", "2", "3"]
    assert sections[1].rows[0][0] == "1"


def test_a_row_carries_drive_situation_recommendation_and_actual() -> None:
    row = chart_sections(_report())[0].rows[0]
    play = _report().plays[0]

    assert row == (
        "1",
        "1",
        situation_text(play),
        recommendation_text(play),
        "Punt",
    )
    assert "Q2 5:43" in row[2]
    assert "4th & 2" in row[2]


def test_a_club_with_no_fourth_downs_still_gets_a_row() -> None:
    report = parse_fourth_downs({"drives": {"previous": []}}, GAME)
    sections = chart_sections(report)

    assert sections[0].rows == (("-", "-", NO_FOURTH_DOWNS_ROW, "-", "-"),)
    assert not report.has_plays


def test_the_chart_draws_as_an_image() -> None:
    image = render_fourth_down_chart(_report())

    with Image.open(io.BytesIO(image)) as drawn:
        assert drawn.width > 0 and drawn.height > 0
    assert artwork_urls(_report()) == ["https://logo/bal", "https://logo/cin"]


def test_an_instance_is_found_by_team_and_row_number() -> None:
    report = _report()
    ravens = find_team(report, "ravens")

    assert ravens is not None
    assert report.instance(ravens, 3) is report.plays[3]
    assert report.instance(ravens, 9) is None
    assert find_team(report, "chiefs") is None


def test_the_chart_embed_names_the_game_and_the_follow_up() -> None:
    embed = fourth_down_chart_embed(_report())

    assert "Baltimore Ravens at Cincinnati Bengals" in (embed.description or "")
    assert "BAL 3" in (embed.description or "")
    assert "/fourthdowns team" in (embed.description or "")


def test_the_chart_embed_writes_the_rows_out_when_the_chart_is_missing() -> None:
    embed = fourth_down_chart_embed(_report(), with_rows=True)

    assert [field.name for field in embed.fields] == [
        "Baltimore Ravens",
        "Cincinnati Bengals",
    ]
    assert embed.image.url is None


def test_the_detail_embed_shows_the_call_and_what_happened() -> None:
    report = _report()
    embed = fourth_down_play_embed(report, report.plays[3])

    happened = next(field for field in embed.fields if field.name == "What happened")
    assert "TOUCHDOWN" in (happened.value or "")
    assert "Drive result: Touchdown" in (happened.value or "")
    assert "Went for it — touchdown" in (embed.title or "")
    assert "BAL fourth down 3" in (embed.description or "")


def test_an_instance_without_a_team_says_which_teams_there_are() -> None:
    message = format_fourth_down_needs_team(["BAL", "CIN"])

    assert "BAL or CIN" in message


def test_an_instance_out_of_range_says_how_many_there_were() -> None:
    message = format_unknown_fourth_down(GAME, "BAL", 9, 3)

    assert "1-3" in message
    assert "instance 9" in message


def _bot(tmp_path, espn) -> RavensBot:
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
    bot.espn = espn
    return bot


def test_no_flags_take_the_game_being_played(tmp_path) -> None:
    live = replace(GAME, state="in", completed=False, status="Q3 2:11")
    espn = SimpleNamespace(
        fetch_live_games=AsyncMock(return_value=[live]),
        fetch_recent_games=AsyncMock(return_value=[]),
    )

    game = asyncio.run(_fourth_down_game(_bot(tmp_path, espn), None, None))

    assert game is live
    espn.fetch_recent_games.assert_not_awaited()


def test_no_flags_fall_back_to_the_last_completed_game(tmp_path) -> None:
    espn = SimpleNamespace(
        fetch_live_games=AsyncMock(return_value=[]),
        fetch_recent_games=AsyncMock(return_value=[GAME]),
    )

    game = asyncio.run(_fourth_down_game(_bot(tmp_path, espn), None, None))

    assert game is GAME


def test_a_week_is_taken_from_the_schedule_rather_than_the_scoreboard(tmp_path) -> None:
    espn = SimpleNamespace(
        fetch_week_game=AsyncMock(return_value=GAME),
        fetch_live_games=AsyncMock(return_value=[]),
        fetch_recent_games=AsyncMock(return_value=[]),
    )

    game = asyncio.run(
        _fourth_down_game(_bot(tmp_path, espn), WeekRequest(5), None)
    )

    assert game is GAME
    espn.fetch_live_games.assert_not_awaited()
    assert espn.fetch_week_game.await_args.args[0] == WeekRequest(5)
