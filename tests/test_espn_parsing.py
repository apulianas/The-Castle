from __future__ import annotations

from datetime import date

from ravens_bot.espn import (
    apply_roster,
    extract_players,
    parse_inactive_report,
    parse_roster,
    parse_schedule,
    parse_standings,
    parse_transactions,
)
from ravens_bot.formatting import format_transaction
from ravens_bot.models import AFC_NORTH_GROUP_ID, Game


def test_parse_schedule_filters_to_ravens_games() -> None:
    payload = {
        "events": [
            {
                "id": "1",
                "name": "Baltimore Ravens at Cincinnati Bengals",
                "shortName": "BAL @ CIN",
                "date": "2026-09-13T17:00Z",
                "status": {"type": {"description": "Scheduled"}},
                "competitions": [{"competitors": [{"team": {"id": "33"}}]}],
            },
            {
                "id": "2",
                "name": "Other game",
                "competitions": [{"competitors": [{"team": {"id": "1"}}]}],
            },
        ]
    }

    games = parse_schedule(payload)

    assert [game.event_id for game in games] == ["1"]
    assert games[0].status == "Scheduled"


def test_parse_schedule_reads_teams_scores_and_broadcast() -> None:
    payload = {
        "events": [
            {
                "id": "9",
                "name": "New York Jets at Baltimore Ravens",
                "shortName": "NYJ @ BAL",
                "date": "2025-11-23T18:00Z",
                "week": {"number": 12},
                "competitions": [
                    {
                        "venue": {
                            "fullName": "M&T Bank Stadium",
                            "address": {"city": "Baltimore", "state": "MD"},
                        },
                        "broadcasts": [{"market": "national", "names": ["CBS"]}],
                        "status": {
                            "type": {
                                "state": "post",
                                "completed": True,
                                "description": "Final",
                            }
                        },
                        "competitors": [
                            {
                                "homeAway": "home",
                                "winner": True,
                                "score": "23",
                                "team": {
                                    "id": "33",
                                    "abbreviation": "BAL",
                                    "displayName": "Baltimore Ravens",
                                },
                                "records": [{"type": "total", "summary": "6-5"}],
                            },
                            {
                                "homeAway": "away",
                                "winner": False,
                                "score": "10",
                                "team": {
                                    "id": "20",
                                    "abbreviation": "NYJ",
                                    "displayName": "New York Jets",
                                },
                                "records": [{"type": "total", "summary": "2-9"}],
                            },
                        ],
                    }
                ],
            }
        ]
    }

    game = parse_schedule(payload)[0]

    assert game.completed is True
    assert game.state == "post"
    assert game.broadcast == "CBS"
    assert game.week == "Week 12"
    assert game.location == "Baltimore, MD"
    assert game.ravens is not None and game.ravens.score == 23
    assert game.opponent is not None and game.opponent.team.abbreviation == "NYJ"
    assert game.ravens.is_home is True


def test_parse_transactions_filters_ravens_and_formats_description() -> None:
    payload = {
        "items": [
            {
                "id": "tx1",
                "date": "2026-08-07T14:00Z",
                "team": {"id": "33"},
                "type": {"displayName": "Signed"},
                "athlete": {"displayName": "Example Player"},
            },
            {
                "id": "tx2",
                "date": "2026-08-07T14:00Z",
                "team": {"id": "10"},
                "description": "Wrong team",
            },
        ]
    }

    transactions = parse_transactions(payload, date(2026, 8, 7))

    assert len(transactions) == 1
    assert transactions[0].description == "Signed Example Player"


def test_transaction_stamped_on_the_next_day_belongs_to_that_day() -> None:
    """ESPN's date filter returns the following day's moves too.

    Each item is stamped at midnight Pacific on the day it happened, so keeping
    the extra day would report the same move on two consecutive dates.
    """
    payload = {
        "items": [
            {
                "date": "2025-11-05T08:00Z",
                "team": {
                    "$ref": "http://sports.core.api.espn.com/v2/sports/football/"
                    "leagues/nfl/seasons/2025/teams/33?lang=en&region=us"
                },
                "description": "Signed S Keondre Jackson to the active roster.",
            }
        ]
    }

    assert parse_transactions(payload, date(2025, 11, 4)) == []
    assert parse_transactions(payload, date(2025, 11, 5))[0].date == date(2025, 11, 5)


def test_transaction_team_is_read_from_a_reference_url() -> None:
    payload = {
        "items": [
            {
                "date": "2025-11-04T08:00Z",
                "team": {
                    "$ref": "http://sports.core.api.espn.com/v2/sports/football/"
                    "leagues/nfl/seasons/2025/teams/2?lang=en&region=us"
                },
                "description": "Signed someone else.",
            },
            {
                "date": "2025-11-04T08:00Z",
                "team": {
                    "$ref": "http://sports.core.api.espn.com/v2/sports/football/"
                    "leagues/nfl/seasons/2025/teams/33?lang=en&region=us"
                },
                "description": "Signed a Raven.",
            },
        ]
    }

    transactions = parse_transactions(payload, date(2025, 11, 4))

    assert [item.description for item in transactions] == ["Signed a Raven."]


def test_undated_transaction_keeps_same_id_across_days() -> None:
    payload = {
        "items": [
            {
                "team": {"id": "33"},
                "type": {"displayName": "Waived"},
                "athlete": {"displayName": "Marquise McDoom"},
            }
        ]
    }

    day_one = parse_transactions(payload, date(2026, 8, 8))
    day_two = parse_transactions(payload, date(2026, 8, 9))

    assert day_one[0].transaction_id == day_two[0].transaction_id


def test_transaction_id_ignores_last_modified_churn() -> None:
    def payload(last_modified: str) -> dict:
        return {
            "items": [
                {
                    "lastModified": last_modified,
                    "team": {"id": "33"},
                    "description": "Waived Marquise McDoom",
                }
            ]
        }

    day_one = parse_transactions(payload("2026-08-08T14:00Z"), date(2026, 8, 8))
    day_two = parse_transactions(payload("2026-08-09T14:00Z"), date(2026, 8, 9))

    assert day_one[0].transaction_id == day_two[0].transaction_id


def test_transaction_outside_the_espn_window_is_dropped() -> None:
    payload = {
        "items": [
            {
                "id": "tx9",
                "date": "2026-08-07T14:00Z",
                "lastModified": "2026-08-09T14:00Z",
                "team": {"id": "33"},
                "description": "Waived Marquise McDoom",
            }
        ]
    }

    assert parse_transactions(payload, date(2026, 8, 7))[0].date == date(2026, 8, 7)
    assert parse_transactions(payload, date(2026, 8, 9)) == []


def test_extract_players_reads_position_and_name() -> None:
    players = extract_players("Waived TE Jordan Murray.")

    assert [(player.position, player.name) for player in players] == [
        ("TE", "Jordan Murray")
    ]


def test_extract_players_handles_plural_codes_and_name_lists() -> None:
    description = (
        "Waived CBs Jalyn Armour-Davis and Marquise Robinson, OLs Gerad "
        "Lichtenhan, Darrian Dalcourt and Nick Samac. Placed WR Dayton Wade "
        "on injured reserve."
    )

    players = extract_players(description)

    assert [(player.position, player.name) for player in players] == [
        ("CB", "Jalyn Armour-Davis"),
        ("CB", "Marquise Robinson"),
        ("OL", "Gerad Lichtenhan"),
        ("OL", "Darrian Dalcourt"),
        ("OL", "Nick Samac"),
        ("WR", "Dayton Wade"),
    ]


def test_extract_players_stops_at_a_sentence_boundary() -> None:
    players = extract_players("Waived LB Kaimon Rucker. Placed WR Dayton Wade on IR.")

    assert [player.name for player in players] == ["Kaimon Rucker", "Dayton Wade"]


def test_extract_players_keeps_initials_out_of_the_position_code() -> None:
    players = extract_players("Released DL Brent Urban and C.J. Okoye.")

    assert [player.name for player in players] == ["Brent Urban", "C.J. Okoye"]


def test_apply_roster_attaches_athlete_ids() -> None:
    roster = parse_roster(
        {
            "athletes": [
                {
                    "items": [
                        {
                            "id": "4878287",
                            "fullName": "Keondre Jackson",
                            "position": {"abbreviation": "S"},
                            "headshot": {"href": "https://example.test/k.png"},
                        }
                    ]
                }
            ]
        }
    )
    payload = {
        "items": [
            {
                "date": "2025-11-04T08:00Z",
                "team": {"id": "33"},
                "description": "Signed S Keondre Jackson to the active roster.",
            }
        ]
    }

    transaction = apply_roster(parse_transactions(payload, date(2025, 11, 4))[0], roster)

    assert transaction.player is not None
    assert transaction.player.athlete_id == "4878287"
    assert transaction.player.page_url == "https://www.espn.com/nfl/player/_/id/4878287"


def test_roster_resolution_keeps_the_spelling_used_in_the_description() -> None:
    """The link is injected by finding the name in the prose, so it has to match.

    ESPN's roster writes "CJ Okoye" where a transaction says "C.J. Okoye"; taking
    the roster spelling would leave the link with nothing to attach to.
    """
    roster = parse_roster(
        {"athletes": [{"items": [{"id": "5144942", "fullName": "CJ Okoye"}]}]}
    )
    payload = {
        "items": [
            {
                "date": "2025-11-04T08:00Z",
                "team": {"id": "33"},
                "description": "Waived DL C.J. Okoye.",
            }
        ]
    }

    transaction = apply_roster(parse_transactions(payload, date(2025, 11, 4))[0], roster)

    assert transaction.player is not None
    assert transaction.player.athlete_id == "5144942"
    assert transaction.player.name == "C.J. Okoye"
    assert format_transaction(transaction) == (
        "Waived DL [C.J. Okoye](https://www.espn.com/nfl/player/_/id/5144942)."
    )


def test_roster_match_ignores_punctuation_and_accents() -> None:
    roster = parse_roster(
        {"athletes": [{"items": [{"id": "1", "fullName": "D\u2019Ernest Johnson"}]}]}
    )
    payload = {
        "items": [
            {
                "date": "2025-11-04T08:00Z",
                "team": {"id": "33"},
                "description": "Signed RB D'Ernest Johnson to the practice squad.",
            }
        ]
    }

    transaction = apply_roster(parse_transactions(payload, date(2025, 11, 4))[0], roster)

    assert transaction.player is not None
    assert transaction.player.athlete_id == "1"
    assert "https://www.espn.com/nfl/player/_/id/1" in format_transaction(transaction)


def test_parse_inactive_report_finds_nested_inactive_players() -> None:
    game = Game(
        "401", "Baltimore Ravens at Cleveland Browns", "BAL @ CLE", None, "Pre-Game"
    )
    summary = {
        "boxscore": {
            "teams": [
                {
                    "team": {"displayName": "Baltimore Ravens"},
                    "inactives": [
                        {
                            "athlete": {
                                "id": "77",
                                "displayName": "Raven One",
                                "position": {"abbreviation": "WR"},
                            },
                            "reason": {"displayName": "Healthy scratch"},
                        }
                    ],
                },
                {
                    "team": {"displayName": "Cleveland Browns"},
                    "players": [
                        {
                            "displayName": "Brown One",
                            "status": {"displayName": "Inactive"},
                        }
                    ],
                },
            ]
        }
    }

    report = parse_inactive_report(summary, game)

    assert [(player.name, player.team) for player in report.players] == [
        ("Raven One", "Baltimore Ravens"),
        ("Brown One", "Cleveland Browns"),
    ]
    assert report.players[0].athlete_id == "77"
    assert report.players[0].position == "WR"


def test_parse_standings_reads_records() -> None:
    payload = {
        "standings": [
            {
                "entries": [
                    {
                        "team": {"id": "33", "displayName": "Baltimore Ravens"},
                        "rank": 1,
                        "stats": [
                            {"name": "wins", "displayValue": "12"},
                            {"name": "losses", "displayValue": "5"},
                            {"name": "streak", "displayValue": "W2"},
                        ],
                    }
                ]
            }
        ]
    }

    standings = parse_standings(payload)

    assert standings[0].team.name == "Baltimore Ravens"
    assert standings[0].record == "12-5"
    assert standings[0].rank == 1
    assert standings[0].is_ravens is True


def test_parse_standings_narrows_to_the_afc_north_group() -> None:
    """ESPN nests divisions two levels deep, so the walk has to recurse."""
    payload = {
        "children": [
            {
                "id": "8",
                "name": "American Football Conference",
                "children": [
                    {
                        "id": "4",
                        "name": "AFC East",
                        "standings": {
                            "entries": [
                                {
                                    "team": {"id": "2", "displayName": "Buffalo Bills"},
                                    "stats": [{"name": "wins", "displayValue": "9"}],
                                }
                            ]
                        },
                    },
                    {
                        "id": AFC_NORTH_GROUP_ID,
                        "name": "AFC North",
                        "standings": {
                            "entries": [
                                {
                                    "team": {
                                        "id": "33",
                                        "abbreviation": "BAL",
                                        "displayName": "Baltimore Ravens",
                                        "logos": [
                                            {
                                                "href": "https://example.test/bal.png",
                                                "rel": ["full", "default"],
                                            }
                                        ],
                                    },
                                    "stats": [
                                        {"name": "wins", "displayValue": "12"},
                                        {"name": "losses", "displayValue": "5"},
                                        {"name": "gamesBehind", "displayValue": "-"},
                                        {"name": "playoffSeed", "displayValue": "3"},
                                        {"type": "vsdiv", "summary": "4-2"},
                                    ],
                                }
                            ]
                        },
                    },
                ],
            }
        ]
    }

    standings = parse_standings(payload, AFC_NORTH_GROUP_ID)

    assert [item.team.name for item in standings] == ["Baltimore Ravens"]
    assert standings[0].playoff_seed == 3
    assert standings[0].division_record == "4-2"
    assert standings[0].team.logo == "https://example.test/bal.png"


def test_division_rank_is_the_standings_order_not_the_playoff_seed() -> None:
    """The seed is a conference-wide 1-16 ranking, so it is not a division place.

    A division winner can hold seed 3, and reading the seed as the rank would
    report the Ravens as third in a division they led.
    """
    payload = {
        "standings": {
            "entries": [
                {
                    "team": {"id": "33", "displayName": "Baltimore Ravens"},
                    "stats": [
                        {"name": "wins", "displayValue": "12"},
                        {"name": "losses", "displayValue": "5"},
                        {"name": "playoffSeed", "displayValue": "3"},
                    ],
                },
                {
                    "team": {"id": "23", "displayName": "Pittsburgh Steelers"},
                    "stats": [
                        {"name": "wins", "displayValue": "10"},
                        {"name": "losses", "displayValue": "7"},
                        {"name": "playoffSeed", "displayValue": "6"},
                    ],
                },
            ]
        }
    }

    standings = parse_standings(payload)

    assert [item.rank for item in standings] == [1, 2]
    assert [item.playoff_seed for item in standings] == [3, 6]


def test_parse_schedule_reads_season_metadata_from_the_event() -> None:
    payload = {
        "events": [
            {
                "id": "3",
                "name": "Baltimore Ravens at Buffalo Bills",
                "season": {"year": 2025, "type": 3},
                "week": {"number": 2},
                "competitions": [{"competitors": [{"team": {"id": "33"}}]}],
            }
        ]
    }

    game = parse_schedule(payload)[0]

    assert game.season == 2025
    assert game.season_type == 3
    assert game.week_number == 2


def test_parse_schedule_falls_back_to_payload_season_and_week() -> None:
    payload = {
        "season": {"year": 2024, "type": 2},
        "week": {"number": 7},
        "events": [
            {
                "id": "4",
                "name": "Baltimore Ravens at Tampa Bay Buccaneers",
                "competitions": [{"competitors": [{"team": {"id": "33"}}]}],
            }
        ],
    }

    game = parse_schedule(payload)[0]

    assert game.season == 2024
    assert game.season_type == 2
    assert game.week_number == 7
