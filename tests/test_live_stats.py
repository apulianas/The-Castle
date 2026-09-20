from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from ravens_bot.bot import RavensBot, _live_command
from ravens_bot.chart import MAX_IMAGE_WIDTH, OUTPUT_SCALE, _font, _wrap_text
from ravens_bot.config import BotConfig
from ravens_bot.embeds import live_game_embed, no_live_game_embed
from ravens_bot.espn import (
    EspnClient,
    parse_leaders,
    parse_live_game,
    parse_live_situation,
    parse_player_stats,
    parse_team_stats,
)
from ravens_bot.formatting import (
    format_live_score,
    format_period,
    format_player_stat_line,
    format_situation,
    format_team_stats,
)
from ravens_bot.models import (
    Game,
    LiveSituation,
    GameTeam,
    LiveGameReport,
    PlayerGameStats,
    PlayerRef,
    TeamGameStats,
    TeamRef,
)
from ravens_bot.live_report import (
    LIVE_CHART_FILENAME,
    ROWS_PER_PAGE,
    artwork_urls,
    chart_pages,
    chart_sections,
    expanded_stats_text,
    render_live_pages,
    render_live_report,
)


EASTERN = ZoneInfo("America/New_York")

RAVENS_TEAM = TeamRef(team_id="33", name="Baltimore Ravens", abbreviation="BAL", slug="bal")
BROWNS_TEAM = TeamRef(team_id="5", name="Cleveland Browns", abbreviation="CLE", slug="cle")


def build_game(state: str = "in", completed: bool = False) -> Game:
    return Game(
        event_id="401",
        name="Cleveland Browns at Baltimore Ravens",
        short_name="CLE @ BAL",
        start_time=datetime(2025, 11, 23, 18, 0, tzinfo=timezone.utc),
        status="1st Quarter",
        home=GameTeam(team=RAVENS_TEAM, is_home=True, score=0, record="6-5"),
        away=GameTeam(team=BROWNS_TEAM, is_home=False, score=0, record="2-9"),
        state=state,
        completed=completed,
        week="Week 12",
        venue="M&T Bank Stadium",
    )


def live_summary(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "header": {
            "id": "401",
            "competitions": [
                {
                    "status": {
                        "displayClock": "7:21",
                        "period": 3,
                        "type": {
                            "state": "in",
                            "completed": False,
                            "description": "3rd Quarter",
                        },
                    },
                    "situation": {
                        "possession": "33",
                        "downDistanceText": "2nd & 7 at CLE 41",
                        "possessionText": "CLE 41",
                        "isRedZone": False,
                        "lastPlay": {"text": "Derrick Henry run for 3 yards"},
                    },
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": "21",
                            "team": {"id": "33", "abbreviation": "BAL"},
                            "record": [{"type": "total", "summary": "7-5"}],
                        },
                        {
                            "homeAway": "away",
                            "score": "13",
                            "team": {"id": "5", "abbreviation": "CLE"},
                        },
                    ],
                }
            ],
        },
        "boxscore": {
            "teams": [
                {
                    "team": {"id": "5", "abbreviation": "CLE"},
                    "statistics": [
                        {"name": "firstDowns", "label": "1st Downs", "displayValue": "9"},
                        {"name": "totalYards", "label": "Total Yards", "displayValue": "180"},
                    ],
                },
                {
                    "team": {"id": "33", "abbreviation": "BAL"},
                    "statistics": [
                        {"name": "firstDowns", "label": "1st Downs", "displayValue": "15"},
                        {"name": "totalYards", "label": "Total Yards", "displayValue": "291"},
                    ],
                },
            ]
        },
        "leaders": [
            {
                "team": {"id": "33", "abbreviation": "BAL"},
                "leaders": [
                    {
                        "name": "passingYards",
                        "shortDisplayName": "PASS",
                        "leaders": [
                            {
                                "displayValue": "18/24, 212 YDS, 2 TD",
                                "athlete": {"id": "3916387", "displayName": "Lamar Jackson"},
                            }
                        ],
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_parse_live_game_reads_score_situation_stats_and_leaders() -> None:
    report = parse_live_game(live_summary(), build_game())

    assert report.is_live
    assert report.game.home is not None and report.game.home.score == 21
    assert report.game.away is not None and report.game.away.score == 13
    assert report.game.status == "3rd Quarter"
    assert report.situation is not None
    assert report.situation.clock == "7:21"
    assert report.situation.period == 3
    assert report.situation.possession == RAVENS_TEAM
    assert report.situation.down_distance == "2nd & 7 at CLE 41"
    assert report.situation.last_play == "Derrick Henry run for 3 yards"
    assert [entry.team.short_name for entry in report.teams] == ["CLE", "BAL"]
    assert report.ravens_stats is not None
    assert report.ravens_stats.value("Total Yards") == "291"
    assert report.stat_labels == ("1st Downs", "Total Yards")
    assert [line.player.name for line in report.leaders] == ["Lamar Jackson"]
    assert report.leaders[0].detail == "18/24, 212 YDS, 2 TD"
    assert report.leaders[0].is_ravens


def test_parse_live_game_keeps_scoreboard_records_when_the_summary_omits_them() -> None:
    report = parse_live_game(live_summary(), build_game())

    assert report.game.home is not None and report.game.home.record == "7-5"
    assert report.game.away is not None and report.game.away.record == "2-9"


def test_parse_live_game_degrades_when_espn_publishes_nothing_but_a_header() -> None:
    payload = {
        "header": {
            "competitions": [
                {
                    "status": {"type": {"state": "in", "description": "1st Quarter"}},
                    "competitors": [
                        {"homeAway": "home", "score": "3", "team": {"id": "33"}},
                        {"homeAway": "away", "score": "0", "team": {"id": "5"}},
                    ],
                }
            ]
        }
    }

    report = parse_live_game(payload, build_game())

    assert report.situation is None
    assert report.teams == ()
    assert report.leaders == ()
    assert not report.has_details
    assert report.game.home is not None and report.game.home.score == 3


def test_parse_live_game_survives_an_empty_payload() -> None:
    game = build_game()

    report = parse_live_game({}, game)

    assert report.game == game
    assert not report.has_details


def test_parse_situation_is_none_at_halftime_without_a_drive() -> None:
    payload = {
        "header": {
            "competitions": [
                {"status": {"type": {"state": "in", "description": "Halftime"}}}
            ]
        }
    }

    assert parse_live_situation(payload, build_game()) is None


def test_parse_situation_reads_a_current_drive_when_the_header_has_none() -> None:
    payload = {
        "header": {"competitions": [{"status": {"period": 2, "displayClock": "0:35"}}]},
        "drives": {
            "current": {
                "possession": {"id": "5"},
                "shortDownDistanceText": "3rd & 2",
            }
        },
    }

    situation = parse_live_situation(payload, build_game())

    assert situation is not None
    assert situation.possession == BROWNS_TEAM
    assert situation.down_distance == "3rd & 2"
    assert situation.clock == "0:35"


def test_parse_team_stats_skips_a_team_without_any_numbers() -> None:
    payload = {
        "boxscore": {
            "teams": [
                {"team": {"id": "33", "abbreviation": "BAL"}, "statistics": []},
                {
                    "team": {"id": "5", "abbreviation": "CLE"},
                    "statistics": [
                        {"label": "Penalties", "displayValue": "4-30"},
                        {"label": "Penalties", "displayValue": "ignored duplicate"},
                        {"label": "Turnovers"},
                    ],
                },
            ]
        }
    }

    teams = parse_team_stats(payload)

    assert len(teams) == 1
    assert teams[0].stats == (("Penalties", "4-30"),)
    assert teams[0].value("turnovers") is None


def test_parse_leaders_falls_back_to_the_player_box_score() -> None:
    payload = {
        "boxscore": {
            "players": [
                {
                    "team": {"id": "33", "abbreviation": "BAL"},
                    "statistics": [
                        {
                            "name": "rushing",
                            "labels": ["CAR", "YDS", "TD"],
                            "athletes": [
                                {
                                    "athlete": {"id": "1", "displayName": "Derrick Henry"},
                                    "stats": ["17", "94", "1"],
                                },
                                {
                                    "athlete": {"id": "2", "displayName": "Backup Back"},
                                    "stats": ["2", "6", "0"],
                                },
                            ],
                        },
                        {
                            "name": "kicking",
                            "labels": ["FG"],
                            "athletes": [
                                {"athlete": {"displayName": "Kicker"}, "stats": ["1/1"]}
                            ],
                        },
                    ],
                }
            ]
        }
    }

    leaders = parse_leaders(payload)

    assert [line.player.name for line in leaders] == ["Derrick Henry"]
    assert leaders[0].category == "Rushing"
    assert leaders[0].detail == "17 CAR, 94 YDS, 1 TD"


def test_parse_leaders_keeps_a_blank_column_from_shifting_labels() -> None:
    payload = {
        "boxscore": {
            "players": [
                {
                    "team": {"id": "33", "abbreviation": "BAL"},
                    "statistics": [
                        {
                            "name": "passing",
                            "labels": ["C/ATT", "YDS", "TD", "INT", "SACKS", "RTG"],
                            "athletes": [
                                {
                                    "athlete": {"displayName": "Lamar Jackson"},
                                    "stats": ["18/24", "212", "2", "", "0-0", "130.2"],
                                },
                                "not a dictionary",
                            ],
                        }
                    ],
                }
            ]
        }
    }

    leaders = parse_leaders(payload)

    assert leaders[0].detail == "18/24 C/ATT, 212 YDS, 2 TD, 0-0 SACKS, 130.2 RTG"


def test_parse_leaders_lists_the_ravens_first() -> None:
    payload = {
        "leaders": [
            {
                "team": {"id": "5", "abbreviation": "CLE"},
                "leaders": [
                    {
                        "shortDisplayName": "PASS",
                        "leaders": [
                            {
                                "displayValue": "12/20, 140 YDS",
                                "athlete": {"displayName": "Browns QB"},
                            }
                        ],
                    }
                ],
            },
            {
                "team": {"id": "33", "abbreviation": "BAL"},
                "leaders": [
                    {
                        "shortDisplayName": "RUSH",
                        "leaders": [
                            {
                                "displayValue": "94 YDS",
                                "athlete": {"displayName": "Derrick Henry"},
                            }
                        ],
                    }
                ],
            },
        ]
    }

    leaders = parse_leaders(payload)

    assert [line.player.name for line in leaders] == ["Derrick Henry", "Browns QB"]


def test_format_period_names_quarters_and_overtime() -> None:
    assert format_period(1) == "Q1"
    assert format_period(4) == "Q4"
    assert format_period(5) == "OT"
    assert format_period(6) == "OT2"
    assert format_period(None) is None
    assert format_period(0) is None


def test_format_live_score_reads_away_side_first() -> None:
    assert format_live_score(build_game()) == "CLE 0 — BAL 0"


def test_format_situation_skips_missing_parts() -> None:
    situation = LiveSituation(clock="7:21", period=3, possession=RAVENS_TEAM)

    assert format_situation(situation) == "7:21 Q3 • 🏈 BAL"
    assert format_situation(None) is None
    assert format_situation(LiveSituation()) is None


def test_format_situation_marks_the_red_zone() -> None:
    situation = LiveSituation(
        clock="0:41",
        period=4,
        possession=RAVENS_TEAM,
        is_red_zone=True,
        down_distance="1st & Goal at CLE 8",
    )

    text = format_situation(situation)

    assert text is not None
    assert "(red zone)" in text
    assert text.endswith("1st & Goal at CLE 8")


def test_format_team_stats_marks_a_statistic_only_one_team_reported() -> None:
    report = LiveGameReport(
        game=build_game(),
        teams=(
            TeamGameStats(team=BROWNS_TEAM, stats=(("Total Yards", "180"),)),
            TeamGameStats(
                team=RAVENS_TEAM, stats=(("Total Yards", "291"), ("Sacks", "3"))
            ),
        ),
    )

    text = format_team_stats(report)

    assert text is not None
    assert text.splitlines()[0] == "(CLE | BAL)"
    assert "Total Yards: 180 | 291" in text
    assert "Sacks: — | 3" in text


def test_format_player_stat_line_links_a_known_athlete() -> None:
    line = PlayerGameStats(
        player=PlayerRef(name="Lamar Jackson", athlete_id="3916387"),
        category="PASS",
        detail="212 YDS, 2 TD",
        team=RAVENS_TEAM,
    )

    assert format_player_stat_line(line) == (
        "PASS: BAL [Lamar Jackson](https://www.espn.com/nfl/player/_/id/3916387) "
        "— 212 YDS, 2 TD"
    )


def test_live_embed_shows_score_situation_and_stats() -> None:
    report = parse_live_game(live_summary(), build_game())

    embed = live_game_embed(
        report, EASTERN, as_of=datetime(2025, 11, 23, 20, 5, tzinfo=timezone.utc)
    )

    assert embed.title == "Baltimore Ravens vs Cleveland Browns — live"
    assert embed.url == "https://www.espn.com/nfl/game/_/gameId/401"
    assert "CLE 13 — BAL 21" in embed.description
    assert "7:21 Q3" in embed.description
    assert "Last play: Derrick Henry run for 3 yards" in embed.description
    assert [field.name for field in embed.fields] == ["Leaders", "Team stats"]
    assert "Lamar Jackson" in embed.fields[0].value
    assert "Total Yards: 180 | 291" in embed.fields[1].value
    assert embed.footer.text == "As of 3:05 PM EST • Data: ESPN"


def test_live_embed_points_at_nextgame_before_kickoff() -> None:
    report = LiveGameReport(game=build_game(state="pre"))

    embed = live_game_embed(report, EASTERN)

    assert embed.title == "Baltimore Ravens vs Cleveland Browns — pregame"
    assert "has not kicked off yet" in embed.description
    assert "/nextgame" in embed.description
    assert embed.fields == []


def test_live_embed_shows_a_final_box_score() -> None:
    game = replace(
        build_game(state="post", completed=True),
        status="Final",
        home=GameTeam(team=RAVENS_TEAM, is_home=True, score=24, is_winner=True),
        away=GameTeam(team=BROWNS_TEAM, is_home=False, score=13),
    )
    report = LiveGameReport(
        game=game,
        teams=(TeamGameStats(team=RAVENS_TEAM, stats=(("Total Yards", "391"),)),),
    )

    embed = live_game_embed(report, EASTERN)

    assert embed.title.endswith("— final")
    assert "W 24-13" in embed.title
    assert "CLE 13 — BAL 24" in embed.description
    assert [field.name for field in embed.fields] == ["Team stats"]


def test_live_embed_says_so_when_stats_are_not_published_yet() -> None:
    report = LiveGameReport(game=build_game())

    embed = live_game_embed(report, EASTERN)

    assert "ESPN has not published stats for this game yet." in embed.description
    assert embed.fields == []


def test_no_live_game_embed_points_at_the_next_matchup() -> None:
    embed = no_live_game_embed(date(2025, 11, 25))

    assert embed.title == "Ravens live stats"
    assert "No Baltimore Ravens game today" in embed.description
    assert "/nextgame" in embed.description


class _StubClient(EspnClient):
    """The client with its two network calls replaced by inline payloads."""

    def __init__(self, games: list[Game], summary: dict[str, Any]) -> None:
        super().__init__(session=None)  # type: ignore[arg-type]
        self._games = games
        self._summary = summary
        self.requested: list[str] = []

    async def fetch_schedule(self, window: Any) -> list[Game]:
        return list(self._games)

    async def fetch_game_summary(self, event_id: str) -> dict[str, Any]:
        self.requested.append(event_id)
        return self._summary


def test_fetch_live_game_is_none_when_the_ravens_do_not_play_today() -> None:
    client = _StubClient([], live_summary())

    assert asyncio.run(client.fetch_live_game(date(2025, 11, 25))) is None


def test_fetch_live_game_prefers_a_game_in_progress() -> None:
    finished = replace(build_game(state="post", completed=True), event_id="1")
    playing = replace(build_game(), event_id="2")
    client = _StubClient([finished, playing], live_summary())

    report = asyncio.run(client.fetch_live_game(date(2025, 11, 23)))

    assert report is not None
    assert client.requested == ["2"]


def test_fetch_live_game_falls_back_to_a_finished_game() -> None:
    finished = replace(build_game(state="post", completed=True), event_id="1")
    upcoming = replace(build_game(state="pre"), event_id="3")
    client = _StubClient([upcoming, finished], live_summary())

    report = asyncio.run(client.fetch_live_game(date(2025, 11, 23)))

    assert report is not None
    assert client.requested == ["1"]


def test_live_chart_prioritizes_ravens_players_over_opponents_and_teams() -> None:
    report = parse_live_game(live_summary(), build_game())
    opponent = PlayerGameStats(PlayerRef("Browns QB"), "PASS", "140 YDS", BROWNS_TEAM)
    report = replace(report, leaders=(opponent, *report.leaders))

    sections = chart_sections(report)

    assert [section.title for section in sections] == [
        "BAL player leaders", "CLE player leaders", "Team snapshot"
    ]
    assert sections[0].rows == (("Lamar Jackson", "PASS: 18/24, 212 YDS, 2 TD"),)
    assert sections[-1].headers == ("Stat", "BAL | CLE")
    assert ("Total Yards", "291 | 180") in sections[-1].rows
    assert all(section.wrap_cells for section in sections)
    urls = artwork_urls(report)
    assert len(urls) == len(set(urls))
    assert report.leaders[1].player.photo_url() in urls


def test_live_chart_keeps_team_totals_compact_and_marks_missing_values() -> None:
    report = LiveGameReport(
        game=build_game(),
        teams=(
            TeamGameStats(RAVENS_TEAM, (
                ("1st Downs", "15"), ("Total Yards", "291"), ("Passing", "212"),
                ("Turnovers", "0"), ("3rd down efficiency", "4-8"),
                ("Possession", "21:30"),
            )),
            TeamGameStats(BROWNS_TEAM, (("Total Yards", "180"),)),
        ),
    )

    assert chart_sections(report)[0].rows == (
        ("Total Yards", "291 | 180"), ("Turnovers", "0 | -"),
        ("3rd down efficiency", "4-8 | -"), ("Possession", "21:30 | -"),
    )


def test_live_chart_handles_players_without_a_team_or_artwork() -> None:
    report = LiveGameReport(
        game=build_game(),
        leaders=(PlayerGameStats(PlayerRef("Player"), "PASS", "212 YDS"),),
    )
    assert chart_sections(report)[0].title == "Player leaders"
    assert artwork_urls(report) == []
    with Image.open(io.BytesIO(render_live_report(report))) as image:
        assert image.format == "PNG"
        assert 0 < image.width <= MAX_IMAGE_WIDTH * OUTPUT_SCALE


def test_live_chart_wraps_full_stat_lines_without_losing_values() -> None:
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font = _font(24)
    text = "Passing: 18/24 C/ATT, 212 YDS, 2 TD, 0 INT, 0-0 SACKS, 130.2 RTG"
    lines = _wrap_text(draw, text, font, 280)
    assert len(lines) > 1
    assert " ".join(lines) == text
    assert all(draw.textlength(line, font=font) <= 280 for line in lines)
    long_name = "ExtremelyLongUnbrokenPlayerName"
    assert "".join(_wrap_text(draw, long_name, font, 100)) == long_name

    report = parse_live_game(live_summary(), build_game())
    longer = replace(report, leaders=(replace(report.leaders[0], detail=text * 4),))
    with Image.open(io.BytesIO(render_live_report(report))) as short_image:
        with Image.open(io.BytesIO(render_live_report(longer))) as long_image:
            assert long_image.height > short_image.height
            assert long_image.width <= MAX_IMAGE_WIDTH * OUTPUT_SCALE


def test_live_embed_uses_attachment_without_repeating_stats() -> None:
    report = parse_live_game(live_summary(), build_game())
    embed = live_game_embed(report, EASTERN, with_chart=True)
    assert embed.image.url == f"attachment://{LIVE_CHART_FILENAME}"
    assert embed.fields == []
    assert "7:21 Q3" in embed.description
    for empty in (
        LiveGameReport(build_game()),
        replace(report, game=build_game(state="pre")),
    ):
        assert not live_game_embed(empty, EASTERN, with_chart=True).image.url


@pytest.mark.parametrize("state", ["in", "post", "pre", "missing", "empty"])
@pytest.mark.parametrize("all_stats", [False, True])
def test_live_command_only_attaches_available_in_game_stats(
    tmp_path, monkeypatch, state, all_stats
) -> None:
    bot = RavensBot(BotConfig(
        discord_token="token", discord_channel_ids=(123,), discord_webhook_urls=(),
        poll_interval_seconds=300, time_zone=EASTERN,
        state_file=str(tmp_path / "state.json"),
    ))
    report = parse_live_game(live_summary(), build_game())
    if state == "missing":
        report = None
    elif state == "empty":
        report = LiveGameReport(build_game())
    else:
        report = replace(report, game=build_game(state=state, completed=state == "post"))
    fetch = AsyncMock(return_value=report)
    monkeypatch.setattr(EspnClient, "fetch_live_game", fetch)
    bot.espn = EspnClient(session=None)  # type: ignore[arg-type]
    interaction = AsyncMock()

    asyncio.run(_live_command(bot).callback(interaction, all_stats=all_stats))

    sent = interaction.followup.send.call_args.kwargs
    assert ("file" in sent) == (state in {"in", "post"})
    if "file" in sent:
        assert sent["file"].filename == LIVE_CHART_FILENAME
        assert sent["embed"].image.url == f"attachment://{LIVE_CHART_FILENAME}"
        assert sent["embed"].fields == []


@pytest.mark.parametrize("failure", ["render", "size"])
def test_live_command_logs_chart_failure_and_sends_player_first_text(
    tmp_path, monkeypatch, caplog, failure
) -> None:
    bot = RavensBot(BotConfig(
        discord_token="token", discord_channel_ids=(123,), discord_webhook_urls=(),
        poll_interval_seconds=300, time_zone=EASTERN,
        state_file=str(tmp_path / "state.json"),
    ))
    report = parse_live_game(live_summary(), build_game())
    monkeypatch.setattr(EspnClient, "fetch_live_game", AsyncMock(return_value=report))
    bot.espn = EspnClient(session=None)  # type: ignore[arg-type]

    def render(*args):
        if failure == "render":
            raise OSError("no fonts")
        return b"oversized"

    monkeypatch.setattr("ravens_bot.bot.render_live_report", render)
    monkeypatch.setattr("ravens_bot.bot.MAX_ATTACHMENT_BYTES", 1)
    interaction = AsyncMock()
    with caplog.at_level(logging.WARNING):
        asyncio.run(_live_command(bot).callback(interaction))
    sent = interaction.followup.send.call_args.kwargs
    assert "file" not in sent
    assert not sent["embed"].image.url
    assert [field.name for field in sent["embed"].fields] == ["Leaders", "Team stats"]
    assert "Live stats chart" in caplog.text


def full_summary() -> dict[str, Any]:
    def category(name, labels, athletes):
        return {
            "name": name,
            "labels": labels,
            "athletes": [
                {"athlete": {"displayName": player}, "stats": stats}
                for player, stats in athletes
            ],
        }

    payload = live_summary()
    payload["boxscore"]["players"] = [
        {
            "team": {"id": "5", "displayName": "Cleveland Browns", "abbreviation": "CLE"},
            "statistics": [
                category("defensive", ["TOT", "SACKS", "PD"], [("Browns LB", ["7", "0.5", "1"])]),
            ],
        },
        {
            "team": {"id": "33", "displayName": "Baltimore Ravens", "abbreviation": "BAL"},
            "statistics": [
                category("rushing", ["CAR", "YDS", "TD"], [
                    ("Derrick Henry", ["17", "94", "1"]),
                    ("Backup Back", ["2", "6", "0"]),
                ]),
                category("defensive", ["TOT", "SOLO", "SACKS", "TFL", "PD"], [
                    (f"Ravens Defender {index}", ["8", None, "1.5", "", "2"])
                    for index in range(23)
                ]),
                category("interceptions", ["INT", "YDS", "TD"], [
                    ("Ravens Safety", ["1", "12", "0"]),
                ]),
                category("fumbles", ["FUM", "LOST", "REC"], [
                    ("Ravens LB", ["0", "0", "1"]),
                ]),
                category("kicking", ["FG", "PCT", "XP"], [("Kicker", ["1/1", "100", "3/3"])]),
                category("kickReturns", ["NO", "YDS", "TD"], [("Returner", ["2", "55", "0"])]),
                category("punting", ["NO", "YDS"], [("Punter", ["3", "144"])]),
            ],
        },
    ]
    return payload


def test_full_box_score_keeps_every_player_and_defensive_and_special_teams_category() -> None:
    payload = full_summary()
    report = parse_live_game(payload, build_game())

    assert report.players == parse_player_stats(payload)
    assert len(report.players) == 31
    assert report.players[0].player.name == "Derrick Henry"
    assert report.players[1].player.name == "Backup Back"
    assert report.players[-1].player.name == "Browns LB"
    assert report.leaders[0].player.name == "Lamar Jackson"
    assert report.players[2].detail == "8 TOT, 1.5 SACKS, 2 PD"
    assert {line.category.lower() for line in report.players} == {
        "rushing", "defensive", "interceptions", "fumbles", "kicking", "kickreturns", "punting",
    }
    assert parse_player_stats({}) == ()


def test_expanded_pages_keep_all_rows_and_repeat_headings() -> None:
    report = parse_live_game(full_summary(), build_game())
    sections = chart_sections(report, all_stats=True)
    pages = chart_pages(report)

    assert len(pages) > 1
    assert sections[0].title == "BAL Rushing"
    assert sections[-1].title == "Team snapshot"
    assert [row for page in pages for section in page for row in section.rows] == [
        row for section in sections for row in section.rows
    ]
    assert all(
        sum(len(section.rows) + 2 for section in page) <= ROWS_PER_PAGE for page in pages
    )
    assert any(section.title.endswith("(cont.)") for page in pages for section in page)
    assert all(section.headers for page in pages for section in page)
    assert all(not section.headshots or len(section.headshots) == len(section.rows)
               for page in pages for section in page)
    text = expanded_stats_text(report)
    assert "Ravens Defender 22 | Defensive: 8 TOT, 1.5 SACKS, 2 PD" in text
    assert "Ravens Safety | Interceptions: 1 INT, 12 YDS, 0 TD" in text
    assert "Ravens LB | Fumbles: 0 FUM, 0 LOST, 1 REC" in text


def test_expanded_takeaways_are_opponents_turnovers_not_forced_fumbles() -> None:
    report = LiveGameReport(
        game=build_game(),
        teams=(
            TeamGameStats(BROWNS_TEAM, (("Turnovers", "3"), ("Forced Fumbles", "5"))),
            TeamGameStats(RAVENS_TEAM, (("Turnovers", "1"), ("Forced Fumbles", "4"))),
        ),
    )
    section = chart_sections(report, all_stats=True)[0]
    assert section.headers == ("Stat", "BAL | CLE")
    assert ("Takeaways", "3 | 1") in section.rows
    missing = replace(report, teams=(
        TeamGameStats(BROWNS_TEAM, (("Total Yards", "123"),)), report.teams[1],
    ))
    assert ("Takeaways", "- | 1") in chart_sections(missing, all_stats=True)[0].rows
    assert not any(row[0] == "Takeaways" for row in chart_sections(report)[0].rows)


def test_expanded_missing_box_score_is_explicit_and_does_not_change_default() -> None:
    report = parse_live_game(live_summary(), build_game())
    assert "full player box score" in live_game_embed(report, EASTERN, all_stats=True).description
    assert "full player box score" not in live_game_embed(report, EASTERN).description
    assert chart_sections(report, all_stats=True)[0].rows[0][0] == "Lamar Jackson"


def test_expanded_only_defensive_stats_are_not_treated_as_empty() -> None:
    report = LiveGameReport(
        game=build_game(),
        players=(PlayerGameStats(PlayerRef("Defender"), "Defensive", "2 SACKS", RAVENS_TEAM),),
    )
    embed = live_game_embed(report, EASTERN, all_stats=True, with_chart=True)
    assert embed.image.url == f"attachment://{LIVE_CHART_FILENAME}"
    assert "not published" not in embed.description
    assert len(chart_pages(report)) == 1


def test_expanded_renders_multiple_bounded_pngs() -> None:
    report = parse_live_game(full_summary(), build_game())
    images = render_live_pages(report)
    assert len(images) == len(chart_pages(report))
    for data in images:
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "PNG"
            assert image.width <= MAX_IMAGE_WIDTH * OUTPUT_SCALE
            assert image.height <= 1800


@pytest.mark.parametrize("failure", [None, "render", "size"])
def test_expanded_command_sends_every_page_or_complete_text(
    tmp_path, monkeypatch, caplog, failure
) -> None:
    bot = RavensBot(BotConfig(
        discord_token="token", discord_channel_ids=(123,), discord_webhook_urls=(),
        poll_interval_seconds=300, time_zone=EASTERN,
        state_file=str(tmp_path / "state.json"),
    ))
    report = parse_live_game(full_summary(), build_game())
    monkeypatch.setattr(EspnClient, "fetch_live_game", AsyncMock(return_value=report))
    bot.espn = EspnClient(session=None)  # type: ignore[arg-type]
    count = len(chart_pages(report))

    def render(*args):
        if failure == "render":
            raise OSError("no fonts")
        return tuple(b"chart" for _ in range(count))

    monkeypatch.setattr("ravens_bot.bot.render_live_pages", render)
    if failure == "size":
        monkeypatch.setattr("ravens_bot.bot.MAX_ATTACHMENT_BYTES", 1)
    interaction = AsyncMock()
    command = _live_command(bot)
    assert command.parameters[0].name == "all_stats"
    assert command.parameters[0].default is False
    with caplog.at_level(logging.WARNING):
        asyncio.run(command.callback(interaction, all_stats=True))
    sent = [call.kwargs for call in interaction.followup.send.call_args_list]
    assert all(post["ephemeral"] for post in sent)
    if failure:
        assert len(sent) == 1
        assert sent[0]["file"].filename == "ravens-all-player-stats.txt"
        assert sent[0]["file"].fp.read().decode("utf-8") == expanded_stats_text(report)
        assert "Expanded live stats chart" in caplog.text
    else:
        assert len(sent) == count
        for index, post in enumerate(sent, 1):
            assert post["file"].filename == LIVE_CHART_FILENAME
            assert post["embed"].image.url == f"attachment://{LIVE_CHART_FILENAME}"
            assert post["embed"].footer.text.startswith(f"Page {index}/{count}")
