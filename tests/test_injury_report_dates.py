from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ravens_bot.injury_report import (
    InjuryTable,
    OfficialInjuryReport,
    add_matchup,
    practice_report_date,
)
from ravens_bot.models import Game, GameTeam, TeamRef


@pytest.mark.parametrize("blank", ["(-)", " (-) ", " ", "-"])
def test_unpublished_practice_columns_do_not_advance_the_report_date(blank: str) -> None:
    headers = ("Player", "Position", "Injury", "Wed", "Thu", "Fri", "Game Status")
    report = OfficialInjuryReport(
        "Week 2",
        tuple(
            InjuryTable(
                team,
                headers,
                ((player, "WR", "Knee", "LP", blank, blank, "-"),),
            )
            for team, player in (
                ("Baltimore Ravens", "Zay Flowers"),
                ("Cleveland Browns", "Jerry Jeudy"),
            )
        ),
    )
    game = Game(
        "1",
        "Baltimore Ravens at Cleveland Browns",
        "BAL @ CLE",
        datetime(2026, 9, 13, 17, tzinfo=timezone.utc),
        "Scheduled",
        away=GameTeam(TeamRef("Baltimore Ravens", "33", "BAL")),
        home=GameTeam(TeamRef("Cleveland Browns", "5", "CLE"), is_home=True),
        season_type=2,
        week_number=2,
    )

    assert report.latest_practice_day == "Wed"
    assert practice_report_date(
        add_matchup(report, [game]), ZoneInfo("America/New_York")
    ) == date(2026, 9, 9)
