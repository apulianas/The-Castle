from __future__ import annotations

from datetime import date

from ravens_bot.espn import parse_inactive_report, parse_schedule, parse_standings, parse_transactions
from ravens_bot.models import Game


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


def test_transaction_date_prefers_original_date_over_last_modified() -> None:
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


def test_parse_inactive_report_finds_nested_inactive_players() -> None:
    game = Game("401", "Baltimore Ravens at Cleveland Browns", "BAL @ CLE", None, "Pre-Game")
    summary = {
        "boxscore": {
            "teams": [
                {
                    "team": {"displayName": "Baltimore Ravens"},
                    "inactives": [
                        {"athlete": {"displayName": "Raven One"}, "reason": {"displayName": "Healthy scratch"}}
                    ],
                },
                {
                    "team": {"displayName": "Cleveland Browns"},
                    "players": [{"displayName": "Brown One", "status": {"displayName": "Inactive"}}],
                },
            ]
        }
    }

    report = parse_inactive_report(summary, game)

    assert [(player.name, player.team) for player in report.players] == [
        ("Raven One", "Baltimore Ravens"),
        ("Brown One", "Cleveland Browns"),
    ]


def test_parse_standings_reads_records() -> None:
    payload = {
        "standings": [
            {
                "entries": [
                    {
                        "team": {"displayName": "Baltimore Ravens"},
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

    assert standings[0].team == "Baltimore Ravens"
    assert standings[0].record == "12-5"
    assert standings[0].rank == 1
