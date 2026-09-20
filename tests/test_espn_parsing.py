from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from ravens_bot.espn import (
    CORE_BASE,
    MAX_INACTIVES_PER_TEAM,
    SITE_BASE,
    EspnApiError,
    EspnClient,
    apply_roster,
    extract_players,
    match_team_games,
    parse_event_inactive_roster,
    parse_inactive_report,
    parse_roster,
    parse_schedule,
    parse_scoreboard,
    parse_standings,
    parse_transactions,
    select_insight_game,
    team_names,
)
from ravens_bot.formatting import format_transaction
from ravens_bot.models import AFC_NORTH_GROUP_ID, Game, GameTeam, TeamRef


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


def test_parse_inactive_report_reads_injury_fantasy_status() -> None:
    game = Game(
        "401", "Baltimore Ravens at Indianapolis Colts", "BAL @ IND", None, "Pre-Game"
    )
    summary = {
        "injuries": [
            {
                "team": {"displayName": "Baltimore Ravens"},
                "injuries": [
                    {
                        "status": "Out",
                        "athlete": {
                            "id": "77",
                            "displayName": "Inactive Raven",
                            "position": {"abbreviation": "WR"},
                        },
                        "details": {
                            "fantasyStatus": {
                                "description": "INACTIVE",
                                "displayDescription": "Inactive",
                            },
                            "type": "Coach's Decision",
                        },
                    },
                    {
                        "status": "Questionable",
                        "athlete": {"displayName": "Active Raven"},
                        "details": {
                            "fantasyStatus": {
                                "description": "QUESTIONABLE",
                                "displayDescription": "Questionable",
                            }
                        },
                    },
                ],
            }
        ]
    }

    report = parse_inactive_report(summary, game)

    assert [(player.name, player.reason) for player in report.players] == [
        ("Inactive Raven", "Coach's Decision")
    ]
    assert report.players[0].team == "Baltimore Ravens"
    assert report.players[0].is_ravens


def test_parse_event_roster_keeps_every_declared_inactive() -> None:
    ravens = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
    roster = {
        "entries": [
            {
                "playerId": 101,
                "displayName": "Fantasy Player",
                "active": False,
                "didNotPlay": True,
                "athlete": {"$ref": "http://example.test/athletes/101"},
            },
            {
                "playerId": 102,
                "displayName": "Lineman",
                "status": {"displayName": "Inactive"},
                "active": False,
                "didNotPlay": True,
                "athlete": {"$ref": "http://example.test/athletes/102"},
            },
            {
                "playerId": 103,
                "displayName": "Dressed Player",
                "active": True,
                "didNotPlay": True,
                "athlete": {"$ref": "http://example.test/athletes/103"},
            },
        ]
    }
    athletes = {
        "101": {
            "id": "101",
            "fullName": "Fantasy Player",
            "position": {"abbreviation": "WR"},
            "injuries": [
                {
                    "details": {
                        "fantasyStatus": {"description": "INACTIVE"},
                        "type": "Hamstring",
                    }
                }
            ],
        },
        "102": {
            "id": "102",
            "fullName": "Complete Name",
            "position": {"abbreviation": "G"},
        },
    }

    players = parse_event_inactive_roster(roster, ravens, athletes)

    assert [(player.name, player.position) for player in players] == [
        ("Fantasy Player", "WR"),
        ("Complete Name", "G"),
    ]
    assert players[0].reason == "Hamstring"
    assert all(player.is_ravens for player in players)


def test_fetch_inactives_resolves_core_athletes_and_normalizes_refs(monkeypatch) -> None:
    ravens = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
    browns = TeamRef("Cleveland Browns", "5", "CLE", "cle")
    game = Game(
        "401",
        "Baltimore Ravens at Cleveland Browns",
        "BAL @ CLE",
        None,
        "Pre-Game",
        home=GameTeam(browns, is_home=True),
        away=GameTeam(ravens),
    )
    client = EspnClient(None)  # type: ignore[arg-type]
    calls: list[tuple[str, dict[str, str] | None]] = []

    async def schedule(window):
        return [game]

    async def json(url, params=None):
        calls.append((url, params))
        if "/competitors/33/roster" in url:
            return {
                "entries": [
                    {
                        "playerId": 77,
                        "displayName": "Raven",
                        "active": False,
                        "didNotPlay": True,
                        "athlete": {"$ref": "http://example.test/athletes/77"},
                    }
                ]
            }
        if "/competitors/5/roster" in url:
            return {"entries": []}
        if url == "https://example.test/athletes/77":
            return {
                "id": "77",
                "fullName": "Raven One",
                "position": {"abbreviation": "DT"},
                "injuries": [
                    {"details": {"fantasyStatus": {"description": "INACTIVE"}}}
                ],
            }
        raise AssertionError(url)

    monkeypatch.setattr(client, "fetch_schedule", schedule)
    monkeypatch.setattr(client, "_json", json)

    reports = asyncio.run(client.fetch_inactives(date(2025, 11, 23)))

    assert [(player.name, player.position) for player in reports[0].players] == [
        ("Raven One", "DT")
    ]
    assert ("https://example.test/athletes/77", None) in calls
    assert all(url != f"{SITE_BASE}/summary" for url, _ in calls)


def test_fetch_inactives_falls_back_to_summary_when_core_fails(monkeypatch) -> None:
    ravens = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
    browns = TeamRef("Cleveland Browns", "5", "CLE", "cle")
    game = Game(
        "401",
        "Baltimore Ravens at Cleveland Browns",
        "BAL @ CLE",
        None,
        "Pre-Game",
        home=GameTeam(browns, is_home=True),
        away=GameTeam(ravens),
    )
    client = EspnClient(None)  # type: ignore[arg-type]

    async def schedule(window):
        return [game]

    async def json(url, params=None):
        if url.startswith(f"{CORE_BASE}/events/"):
            raise EspnApiError("core unavailable")
        assert url == f"{SITE_BASE}/summary"
        assert params == {"event": "401"}
        return {
            "injuries": [
                {
                    "team": {"displayName": "Baltimore Ravens"},
                    "injuries": [
                        {
                            "athlete": {"id": "77", "displayName": "Fallback Raven"},
                            "details": {
                                "fantasyStatus": {"description": "INACTIVE"}
                            },
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(client, "fetch_schedule", schedule)
    monkeypatch.setattr(client, "_json", json)

    reports = asyncio.run(client.fetch_inactives(date(2025, 11, 23)))

    assert [player.name for player in reports[0].players] == ["Fallback Raven"]


def test_parse_event_roster_reads_a_real_game_day_list() -> None:
    """A club declares six or seven, not a squad's worth of unused players."""
    ravens = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
    declared = [
        ("4429615", "Zay Flowers", "WR"),
        ("4429582", "Joe Fagnano", "QB"),
        ("4243220", "Andrew Vorhees", "G"),
        ("4686768", "Gerad Lichtenhan", "OT"),
        ("4035245", "Nnamdi Madubuike", "DT"),
        ("4698244", "Teddye Buchanan", "LB"),
    ]
    roster = {
        "entries": [
            {
                "playerId": int(athlete_id),
                "displayName": name,
                "active": False,
                "didNotPlay": True,
                "athlete": {"$ref": f"http://example.test/athletes/{athlete_id}"},
            }
            for athlete_id, name, _ in declared
        ]
        + [
            {
                "playerId": 8,
                "displayName": "Lamar Jackson",
                "active": False,
                "didNotPlay": False,
            },
            {
                "playerId": 9,
                "displayName": "Dressed Reserve",
                "active": False,
                "didNotPlay": True,
            },
        ]
    }
    athletes = {
        athlete_id: {
            "id": athlete_id,
            "fullName": name,
            "position": {"abbreviation": position},
            "injuries": [
                {"details": {"fantasyStatus": {"description": "INACTIVE"}}}
            ],
        }
        for athlete_id, name, position in declared
    }

    players = parse_event_inactive_roster(roster, ravens, athletes)

    assert [(player.position, player.name) for player in players] == [
        (position, name) for _, name, position in declared
    ]
    assert all(player.is_ravens for player in players)


def test_fetch_inactives_falls_back_when_the_roster_lists_no_inactives(
    monkeypatch,
) -> None:
    """Mid game every unused player reads as "did not play", so the flag lies."""
    ravens = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
    browns = TeamRef("Cleveland Browns", "5", "CLE", "cle")
    game = Game(
        "401",
        "Baltimore Ravens at Cleveland Browns",
        "BAL @ CLE",
        None,
        "In Progress",
        home=GameTeam(browns, is_home=True),
        away=GameTeam(ravens),
        state="in",
    )
    client = EspnClient(None)  # type: ignore[arg-type]
    summaries = 0

    async def schedule(window):
        return [game]

    async def json(url, params=None):
        nonlocal summaries
        if url.startswith("https://example.test/athletes/"):
            return {"injuries": []}
        if "/roster" in url:
            return {
                "entries": [
                    {
                        "playerId": 90 + index,
                        "displayName": f"Benched {index}",
                        "active": True,
                        "didNotPlay": True,
                        "athlete": {"$ref": f"http://example.test/athletes/{index}"},
                    }
                    for index in range(30)
                ]
            }
        assert url == f"{SITE_BASE}/summary"
        summaries += 1
        return {
            "injuries": [
                {
                    "team": {"displayName": "Baltimore Ravens"},
                    "injuries": [
                        {
                            "athlete": {"id": "77", "displayName": "Fallback Raven"},
                            "details": {"fantasyStatus": {"description": "INACTIVE"}},
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(client, "fetch_schedule", schedule)
    monkeypatch.setattr(client, "_json", json)

    reports = asyncio.run(client.fetch_inactives(date(2025, 11, 23)))

    assert summaries == 1
    assert [player.name for player in reports[0].players] == ["Fallback Raven"]


def test_fetch_inactives_ignores_an_implausibly_long_roster_list(monkeypatch) -> None:
    ravens = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
    browns = TeamRef("Cleveland Browns", "5", "CLE", "cle")
    game = Game(
        "401",
        "Baltimore Ravens at Cleveland Browns",
        "BAL @ CLE",
        None,
        "In Progress",
        home=GameTeam(browns, is_home=True),
        away=GameTeam(ravens),
        state="in",
    )
    client = EspnClient(None)  # type: ignore[arg-type]

    async def schedule(window):
        return [game]

    async def json(url, params=None):
        if "/roster" in url:
            return {
                "entries": [
                    {
                        "playerId": index,
                        "displayName": f"Player {index}",
                        "active": False,
                        "didNotPlay": True,
                        "status": {"displayName": "Inactive"},
                    }
                    for index in range(MAX_INACTIVES_PER_TEAM + 1)
                ]
            }
        assert url == f"{SITE_BASE}/summary"
        return {}

    monkeypatch.setattr(client, "fetch_schedule", schedule)
    monkeypatch.setattr(client, "_json", json)

    reports = asyncio.run(client.fetch_inactives(date(2025, 11, 23)))

    assert reports[0].players == ()


def test_fetch_inactives_reports_the_game_when_no_source_answers(monkeypatch) -> None:
    """A failed read is a game without a published list, not a failed command."""
    ravens = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
    browns = TeamRef("Cleveland Browns", "5", "CLE", "cle")
    game = Game(
        "401",
        "Baltimore Ravens at Cleveland Browns",
        "BAL @ CLE",
        None,
        "In Progress",
        home=GameTeam(browns, is_home=True),
        away=GameTeam(ravens),
        state="in",
    )
    client = EspnClient(None)  # type: ignore[arg-type]

    async def schedule(window):
        return [game]

    async def json(url, params=None):
        raise EspnApiError("ESPN API returned HTTP 400")

    monkeypatch.setattr(client, "fetch_schedule", schedule)
    monkeypatch.setattr(client, "_json", json)

    reports = asyncio.run(client.fetch_inactives(date(2025, 11, 23)))

    assert [report.game.event_id for report in reports] == ["401"]
    assert reports[0].players == ()


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


def _live_event(
    situation: dict[str, object] | None,
    *,
    event_id: str = "401671800",
    home_id: str = "4",
    home_abbr: str = "CIN",
    away_id: str = "33",
    away_abbr: str = "BAL",
    state: str = "in",
    home_score: str = "17",
    away_score: str = "21",
    start: str = "2025-11-23T18:00Z",
) -> dict[str, object]:
    """A scoreboard event shaped the way ESPN sends a game in progress."""
    competition: dict[str, object] = {
        "date": start,
        "status": {
            "period": 3,
            "displayClock": "5:21",
            "type": {"state": state, "completed": state == "post", "description": "In Progress"},
        },
        "competitors": [
            {
                "homeAway": "home",
                "score": home_score,
                "team": {"id": home_id, "displayName": f"{home_abbr} team", "abbreviation": home_abbr},
            },
            {
                "homeAway": "away",
                "score": away_score,
                "team": {"id": away_id, "displayName": f"{away_abbr} team", "abbreviation": away_abbr},
            },
        ],
    }
    if situation is not None:
        competition["situation"] = situation
    return {
        "id": event_id,
        "name": f"{away_abbr} at {home_abbr}",
        "shortName": f"{away_abbr} @ {home_abbr}",
        "date": start,
        "competitions": [competition],
    }


def test_parse_scoreboard_keeps_every_game_not_only_the_ravens() -> None:
    payload = {
        "events": [
            _live_event(None, event_id="1"),
            _live_event(None, event_id="2", home_id="1", home_abbr="ATL", away_id="2", away_abbr="BUF"),
        ]
    }

    games = parse_scoreboard(payload)

    assert [game.event_id for game in games] == ["1", "2"]
    assert parse_schedule(payload)[0].event_id == "1"


def test_parse_situation_reads_a_spot_on_the_defence_side_of_the_field() -> None:
    payload = {
        "events": [
            _live_event(
                {
                    "down": 4,
                    "distance": 3,
                    "yardLine": 90,
                    "possessionText": "CIN 10",
                    "downDistanceText": "4th & 3",
                    "isRedZone": True,
                    "possession": "33",
                }
            )
        ]
    }

    situation = parse_scoreboard(payload)[0].situation

    assert situation is not None
    assert situation.possession.abbreviation == "BAL"
    assert situation.defense is not None and situation.defense.abbreviation == "CIN"
    assert situation.yards_to_goal == 10
    assert situation.is_fourth_down
    assert situation.is_red_zone
    assert situation.score_differential == 4
    assert situation.period == 3
    assert situation.clock == "5:21"
    assert situation.clock_seconds == 321
    assert situation.seconds_remaining == 900 + 321


def test_parse_situation_reads_a_spot_on_the_offence_own_side() -> None:
    payload = {
        "events": [
            _live_event(
                {
                    "down": 4,
                    "distance": 12,
                    "yardLine": 22,
                    "possessionText": "BAL 22",
                    "possession": "33",
                }
            )
        ]
    }

    situation = parse_scoreboard(payload)[0].situation

    assert situation is not None
    assert situation.yards_to_goal == 78


def test_parse_situation_reads_midfield_the_same_from_either_side() -> None:
    for text in ("BAL 50", "CIN 50"):
        payload = {"events": [_live_event({"down": 4, "distance": 1, "possessionText": text, "possession": "33"})]}

        situation = parse_scoreboard(payload)[0].situation

        assert situation is not None and situation.yards_to_goal == 50


def test_parse_situation_prefers_the_named_spot_over_a_contradicting_yard_line() -> None:
    payload = {
        "events": [
            _live_event(
                {
                    "down": 4,
                    "distance": 2,
                    # A yard line that would read as the offence's own 30.
                    "yardLine": 30,
                    "possessionText": "CIN 30",
                    "possession": "33",
                }
            )
        ]
    }

    situation = parse_scoreboard(payload)[0].situation

    assert situation is not None and situation.yards_to_goal == 30


def test_parse_situation_falls_back_to_the_yard_line_without_a_spot() -> None:
    payload = {"events": [_live_event({"down": 4, "distance": 2, "yardLine": 35, "possession": "33"})]}

    situation = parse_scoreboard(payload)[0].situation

    assert situation is not None and situation.yards_to_goal == 65


def test_parse_situation_trusts_yards_to_endzone_when_espn_sends_it() -> None:
    payload = {
        "events": [
            _live_event(
                {
                    "down": 4,
                    "distance": 2,
                    "yardsToEndzone": 41,
                    "yardLine": 12,
                    "possessionText": "CIN 41",
                    "possession": "33",
                }
            )
        ]
    }

    situation = parse_scoreboard(payload)[0].situation

    assert situation is not None and situation.yards_to_goal == 41


def test_parse_situation_is_absent_without_one_in_the_payload() -> None:
    payload = {"events": [_live_event(None)]}

    assert parse_scoreboard(payload)[0].situation is None


def test_parse_situation_is_absent_before_kickoff() -> None:
    payload = {
        "events": [
            _live_event(
                {"down": 4, "distance": 2, "possessionText": "CIN 30", "possession": "33"},
                state="pre",
            )
        ]
    }

    game = parse_scoreboard(payload)[0]

    assert game.situation is None
    assert not game.in_progress


def test_situation_summary_reads_as_a_sentence() -> None:
    payload = {
        "events": [
            _live_event(
                {
                    "down": 4,
                    "distance": 3,
                    "possessionText": "CIN 10",
                    "downDistanceText": "4th & 3",
                    "possession": "33",
                }
            )
        ]
    }

    situation = parse_scoreboard(payload)[0].situation

    assert situation is not None
    assert situation.summary == "BAL 4th & 3 at the CIN 10 • Q3 5:21 • leading by 4"


def test_select_insight_game_prefers_the_ravens() -> None:
    payload = {
        "events": [
            _live_event(None, event_id="1", home_id="1", home_abbr="ATL", away_id="2", away_abbr="BUF"),
            _live_event(None, event_id="2"),
        ]
    }
    games = parse_scoreboard(payload)

    chosen = select_insight_game(games, "Buffalo Bills")

    assert chosen is not None and chosen.event_id == "2"


def test_select_insight_game_falls_back_to_the_configured_team() -> None:
    payload = {
        "events": [
            _live_event(None, event_id="1", home_id="1", home_abbr="ATL", away_id="5", away_abbr="DAL"),
            _live_event(None, event_id="2", home_id="3", home_abbr="BUF", away_id="6", away_abbr="MIA"),
        ]
    }
    games = parse_scoreboard(payload)

    chosen = select_insight_game(games, "BUF")

    assert chosen is not None and chosen.event_id == "2"


def test_select_insight_game_falls_back_to_the_nearest_kickoff() -> None:
    payload = {
        "events": [
            _live_event(
                None, event_id="1", home_id="1", home_abbr="ATL", away_id="5", away_abbr="DAL",
                start="2025-11-23T18:00Z",
            ),
            _live_event(
                None, event_id="2", home_id="3", home_abbr="BUF", away_id="6", away_abbr="MIA",
                start="2025-11-23T21:25Z",
            ),
        ]
    }
    games = parse_scoreboard(payload)

    chosen = select_insight_game(games, None, datetime(2025, 11, 23, 21, 40, tzinfo=timezone.utc))

    assert chosen is not None and chosen.event_id == "2"


def test_select_insight_game_ignores_games_that_are_not_being_played() -> None:
    payload = {"events": [_live_event(None, event_id="1", state="post")]}

    assert select_insight_game(parse_scoreboard(payload)) is None


def test_match_team_games_accepts_an_abbreviation_a_city_or_a_nickname() -> None:
    payload = {"events": [_live_event(None, event_id="1", home_id="1", home_abbr="ATL", away_id="2", away_abbr="BUF")]}
    games = parse_scoreboard(payload)
    assert [game.event_id for game in match_team_games(games, "BUF")] == ["1"]
    assert [game.event_id for game in match_team_games(games, "atl")] == ["1"]
    assert match_team_games(games, "Seattle") == []


def test_team_names_lists_who_is_playing() -> None:
    payload = {"events": [_live_event(None, event_id="1")]}

    assert team_names(parse_scoreboard(payload)) == ["BAL team", "CIN team"]
