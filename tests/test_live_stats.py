from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ravens_bot.embeds import live_game_embed, no_live_game_embed
from ravens_bot.espn import (
    EspnClient,
    parse_leaders,
    parse_live_game,
    parse_live_situation,
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
    assert [field.name for field in embed.fields] == ["Team stats", "Leaders"]
    assert "Total Yards: 180 | 291" in embed.fields[0].value
    assert "Lamar Jackson" in embed.fields[1].value
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
