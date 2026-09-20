from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from ravens_bot.espn import (
    INACTIVE_ATHLETE_TTL_SECONDS,
    SITE_BASE,
    EspnApiError,
    EspnClient,
    parse_event_inactive_roster,
)
from ravens_bot.inactives_report import chart_sections
from ravens_bot.models import Game, GameTeam, TeamRef


RAVENS = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
SAINTS = TeamRef("New Orleans Saints", "18", "NO", "no")
GAME = Game(
    "401872938",
    "New Orleans Saints at Baltimore Ravens",
    "NO @ BAL",
    datetime(2026, 9, 20, 17, tzinfo=timezone.utc),
    "In Progress",
    home=GameTeam(RAVENS, is_home=True),
    away=GameTeam(SAINTS),
)

# Relevant fields from ESPN's September 20, 2026 roster and athlete responses.
PLAYERS = (
    ("4035245", "Nnamdi Madubuike", "DT", "Neck"),
    ("4035671", "Tyler Huntley", "QB", None),
    ("4243220", "Andrew Vorhees", "G", "Coach's Decision"),
    ("4429582", "Joe Fagnano", "QB", "Coach's Decision"),
    ("4429615", "Zay Flowers", "WR", "Hamstring"),
    ("4686768", "Gerad Lichtenhan", "OT", "Coach's Decision"),
    ("4698244", "Teddye Buchanan", "LB", "Knee - ACL"),
    ("5088338", "Elijah Sarratt", "WR", None),
)
EXPECTED = {name for _, name, _, reason in PLAYERS if reason is not None}


def roster_and_athletes():
    entries = []
    athletes = {}
    for athlete_id, name, position, reason in PLAYERS:
        entries.append(
            {
                "playerId": int(athlete_id),
                "displayName": name.split()[-1],
                "active": False,
                "valid": False,
                "didNotPlay": True,
                "athlete": {"$ref": f"http://example.test/athletes/{athlete_id}"},
            }
        )
        athletes[athlete_id] = {
            "id": athlete_id,
            "fullName": name,
            "position": {"abbreviation": position},
            "status": {"name": "Active"},
            "injuries": [
                {
                    "date": "2026-09-20T15:35Z",
                    "status": "Out",
                    "details": {
                        "fantasyStatus": {"description": "INACTIVE"},
                        "type": reason,
                    },
                }
            ] if reason else [],
        }
    return {"entries": entries}, athletes


@pytest.mark.parametrize("pregame", [False, True])
def test_fetch_returns_all_six_without_dressed_reserves(monkeypatch, pregame):
    roster, athletes = roster_and_athletes()
    for index in range(46):
        athlete_id = str(index)
        roster["entries"].append(
            {
                "playerId": index,
                "displayName": f"Participant {index}",
                "active": False,
                "didNotPlay": pregame,
                "athlete": {"$ref": f"http://example.test/athletes/{athlete_id}"},
            }
        )
        athletes[athlete_id] = {"id": athlete_id, "injuries": []}
    client = EspnClient(None)  # type: ignore[arg-type]

    async def schedule(window):
        return [GAME]

    async def json(url, params=None):
        if "/competitors/33/roster" in url:
            return roster
        if "/competitors/18/roster" in url:
            return {"entries": []}
        assert url.startswith("https://example.test/athletes/")
        return athletes[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(client, "fetch_schedule", schedule)
    monkeypatch.setattr(client, "_json", json)
    reports = asyncio.run(client.fetch_inactives(date(2026, 9, 20)))

    assert len(reports) == 1
    assert len(reports[0].players) == 6
    assert {player.name for player in reports[0].players} == EXPECTED
    assert all(player.is_ravens for player in reports[0].players)
    rows = chart_sections(reports[0])[1].rows
    assert len(rows) == 6
    assert ("OT Gerad Lichtenhan", "Coach's Decision") in rows
    assert ("LB Teddye Buchanan", "Knee - ACL") in rows


def test_pregame_statuses_are_refetched_after_cache_expires(monkeypatch):
    roster, athletes = roster_and_athletes()
    client = EspnClient(None)  # type: ignore[arg-type]
    clock = [0.0]
    calls = []
    monkeypatch.setattr(client._inactive_athletes, "_clock", lambda: clock[0])

    async def schedule(window):
        return [GAME]

    async def json(url, params=None):
        if "/competitors/33/roster" in url:
            return roster
        if "/competitors/18/roster" in url:
            return {"entries": []}
        if url == f"{SITE_BASE}/summary":
            return {}
        athlete = athletes[url.rsplit("/", 1)[1]]
        calls.append(url)
        if clock[0] == 0:
            return {**athlete, "injuries": []}
        return athlete

    monkeypatch.setattr(client, "fetch_schedule", schedule)
    monkeypatch.setattr(client, "_json", json)

    async def run():
        assert not (await client.fetch_inactives(date(2026, 9, 20)))[0].players
        assert not (await client.fetch_inactives(date(2026, 9, 20)))[0].players
        assert len(calls) == 8
        clock[0] = INACTIVE_ATHLETE_TTL_SECONDS
        report = (await client.fetch_inactives(date(2026, 9, 20)))[0]
        assert {player.name for player in report.players} == EXPECTED
        assert len(calls) == 16

    asyncio.run(run())


@pytest.mark.parametrize("stamp", [None, "invalid", "2026-09-13T15:35Z", "2026-09-27T15:35Z"])
def test_current_athlete_status_cannot_leak_into_another_game(stamp):
    roster, athletes = roster_and_athletes()
    for athlete in athletes.values():
        for injury in athlete["injuries"]:
            injury["date"] = stamp
    assert parse_event_inactive_roster(roster, RAVENS, athletes, GAME) == ()


def test_primetime_game_uses_local_date_for_declarations():
    roster, athletes = roster_and_athletes()
    game = Game(
        "night", "Saints at Ravens", "NO @ BAL",
        datetime(2026, 9, 21, 0, 20, tzinfo=timezone.utc), "Scheduled",
    )
    assert {
        player.name
        for player in parse_event_inactive_roster(roster, RAVENS, athletes, game)
    } == EXPECTED


def test_out_and_nonparticipation_are_not_inactive_declarations():
    roster, athletes = roster_and_athletes()
    for athlete in athletes.values():
        for injury in athlete["injuries"]:
            injury["details"]["fantasyStatus"] = {"description": "OUT"}
    assert parse_event_inactive_roster(roster, RAVENS, athletes, GAME) == ()


def test_failed_athlete_lookup_does_not_turn_a_reserve_into_an_inactive(
    monkeypatch, caplog
):
    roster, athletes = roster_and_athletes()
    client = EspnClient(None)  # type: ignore[arg-type]

    async def json(url, params=None):
        if "/competitors/33/roster" in url:
            return roster
        if "/competitors/18/roster" in url:
            return {"entries": []}
        athlete_id = url.rsplit("/", 1)[1]
        if athlete_id == "4035671":
            raise EspnApiError("Unavailable")
        return athletes[athlete_id]

    monkeypatch.setattr(client, "_json", json)
    report = asyncio.run(client._fetch_event_inactives(GAME))
    assert {player.name for player in report.players} == EXPECTED
    assert "Inactive status unavailable for athlete 4035671" in caplog.text
