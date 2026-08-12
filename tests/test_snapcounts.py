from __future__ import annotations

from ravens_bot.formatting import format_snap_row
from ravens_bot.models import (
    DEFENSE,
    OFFENSE,
    SPECIAL_TEAMS,
    Game,
    GameTeam,
    PlayerRef,
    TeamRef,
)
from ravens_bot.snapcounts import (
    aggregate,
    build_report,
    match_game,
    match_players,
    parse_snap_counts,
    team_code,
)


HEADER = (
    "game_id,pfr_game_id,season,game_type,week,player,pfr_player_id,position,team,"
    "opponent,offense_snaps,offense_pct,defense_snaps,defense_pct,st_snaps,st_pct\n"
)

SAMPLE = HEADER + "\n".join(
    [
        "2025_02_BAL_CLE,x,2025,REG,2,Lamar Jackson,JackLa00,QB,BAL,CLE,68,1,0,0,0,0",
        "2025_02_BAL_CLE,x,2025,REG,2,Zay Flowers,FlowZa00,WR,BAL,CLE,54,0.79,0,0,3,0.12",
        "2025_02_BAL_CLE,x,2025,REG,2,Roquan Smith,SmitRo00,LB,BAL,CLE,0,0,60,1,2,0.08",
        "2025_02_BAL_CLE,x,2025,REG,2,Nick Moore,MoorNi00,LS,BAL,CLE,0,0,0,0,25,1",
        "2025_02_BAL_CLE,x,2025,REG,2,Deep Reserve,ReseDe00,RB,BAL,CLE,0,0,0,0,0,0",
        "2025_02_BAL_CLE,x,2025,REG,2,Myles Garrett,GarrMy00,DE,CLE,BAL,0,0,58,1,0,0",
    ]
)


def _ravens_game(
    event_id: str = "1",
    season: int | None = 2025,
    season_type: int | None = 2,
    opponent: str = "CLE",
    ravens_home: bool = False,
    week: str | None = "Week 2",
) -> Game:
    ravens = GameTeam(
        team=TeamRef(name="Baltimore Ravens", team_id="33", abbreviation="BAL"),
        is_home=ravens_home,
    )
    other = GameTeam(
        team=TeamRef(name="Cleveland Browns", team_id="5", abbreviation=opponent),
        is_home=not ravens_home,
    )
    home, away = (ravens, other) if ravens_home else (other, ravens)
    return Game(
        event_id=event_id,
        name="Baltimore Ravens at Cleveland Browns",
        short_name="BAL @ CLE",
        start_time=None,
        status="Final",
        home=home,
        away=away,
        state="post",
        completed=True,
        week=week,
        season=season,
        season_type=season_type,
    )


def test_parse_snap_counts_keeps_only_the_ravens() -> None:
    games = parse_snap_counts(SAMPLE)

    entry = games["2025_02_BAL_CLE"]
    assert list(games) == ["2025_02_BAL_CLE"]
    assert [player.name for player in entry.players] == [
        "Lamar Jackson",
        "Zay Flowers",
        "Roquan Smith",
        "Nick Moore",
        "Deep Reserve",
    ]
    assert entry.opponent == "CLE"
    assert entry.is_home is False
    assert entry.is_regular_season is True


def test_parse_snap_counts_recovers_unit_totals_from_shares() -> None:
    entry = parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"]

    assert entry.totals[OFFENSE] == 68
    assert entry.totals[DEFENSE] == 60
    assert entry.totals[SPECIAL_TEAMS] == 25


def test_parse_snap_counts_keeps_special_teams_only_and_zero_snap_players() -> None:
    players = {player.name: player for player in parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"].players}

    assert players["Nick Moore"].primary_unit == SPECIAL_TEAMS
    assert players["Nick Moore"].special_teams == 25
    assert players["Deep Reserve"].total == 0
    assert players["Deep Reserve"].primary_unit == OFFENSE


def test_parse_snap_counts_skips_unreadable_rows_instead_of_raising() -> None:
    text = HEADER + "\n".join(
        [
            ",x,2025,REG,2,No Game Id,,QB,BAL,CLE,10,1,0,0,0,0",
            "2025_02_BAL_CLE,x,2025,REG,2,,,QB,BAL,CLE,10,1,0,0,0,0",
            "2025_02_BAL_CLE,x,2025,REG,2,Lamar Jackson,JackLa00,QB,BAL,CLE,ten,,0,0,0,0",
        ]
    )

    games = parse_snap_counts(text)

    entry = games["2025_02_BAL_CLE"]
    assert [player.name for player in entry.players] == ["Lamar Jackson"]
    assert entry.totals[OFFENSE] == 0


def test_parse_snap_counts_returns_nothing_for_a_missing_section() -> None:
    assert parse_snap_counts("") == {}
    assert parse_snap_counts(HEADER) == {}


def test_team_code_maps_espn_abbreviations() -> None:
    assert team_code("LAR") == "LA"
    assert team_code("wsh") == "WAS"
    assert team_code("KC") == "KC"
    assert team_code(None) is None


def test_match_game_uses_season_opponent_and_venue() -> None:
    games = parse_snap_counts(SAMPLE)

    assert match_game(games, _ravens_game()) is not None
    assert match_game(games, _ravens_game(ravens_home=True)) is None
    assert match_game(games, _ravens_game(season=2024)) is None
    assert match_game(games, _ravens_game(opponent="PIT")) is None


def test_match_game_separates_a_playoff_rematch() -> None:
    text = HEADER + "\n".join(
        [
            "2025_02_BAL_CLE,x,2025,REG,2,Lamar Jackson,JackLa00,QB,BAL,CLE,68,1,0,0,0,0",
            "2025_19_BAL_CLE,x,2025,WC,19,Lamar Jackson,JackLa00,QB,BAL,CLE,60,1,0,0,0,0",
        ]
    )
    games = parse_snap_counts(text)

    regular = match_game(games, _ravens_game(season_type=2))
    playoff = match_game(games, _ravens_game(season_type=3))

    assert regular is not None and regular.game_id == "2025_02_BAL_CLE"
    assert playoff is not None and playoff.game_id == "2025_19_BAL_CLE"


def test_build_report_applies_roster_art_and_links() -> None:
    games = parse_snap_counts(SAMPLE)
    game = _ravens_game()
    roster = {
        "lamar jackson": PlayerRef(
            name="Lamar Jackson",
            athlete_id="3916387",
            position="QB",
            headshot="https://example.test/lamar.png",
            link="https://example.test/lamar",
        )
    }

    report = build_report(game, games["2025_02_BAL_CLE"], roster)

    lamar = report.players[0]
    assert lamar.player.athlete_id == "3916387"
    assert lamar.player.page_url == "https://example.test/lamar"
    assert report.players[1].player.athlete_id is None
    assert report.offense_total == 68


def test_report_units_are_sorted_and_exclusive() -> None:
    report = build_report(_ravens_game(), parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"])

    assert [entry.name for entry in report.unit(OFFENSE)] == [
        "Lamar Jackson",
        "Zay Flowers",
    ]
    assert [entry.name for entry in report.unit(DEFENSE)] == ["Roquan Smith"]
    assert [entry.name for entry in report.unit(SPECIAL_TEAMS)] == ["Nick Moore"]


def test_aggregate_sums_only_the_games_a_player_appeared_in() -> None:
    first = build_report(
        _ravens_game(event_id="1"), parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"]
    )
    later_text = HEADER + (
        "2025_03_BAL_CLE,x,2025,REG,3,Zay Flowers,FlowZa00,WR,BAL,CLE,40,0.8,0,0,0,0"
    )
    second = build_report(
        _ravens_game(event_id="2"), parse_snap_counts(later_text)["2025_03_BAL_CLE"]
    )

    totals = {item.player.name: item for item in aggregate([first, second])}

    assert totals["Zay Flowers"].offense == 94
    assert totals["Zay Flowers"].offense_total == 118
    assert totals["Zay Flowers"].games == 2
    assert totals["Lamar Jackson"].offense_total == 68
    assert totals["Lamar Jackson"].games == 1


def test_match_players_prefers_an_exact_name() -> None:
    report = build_report(_ravens_game(), parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"])
    totals = aggregate([report])

    assert [item.player.name for item in match_players(totals, "lamar jackson")] == [
        "Lamar Jackson"
    ]
    assert [item.player.name for item in match_players(totals, "smith")] == [
        "Roquan Smith"
    ]
    assert match_players(totals, "nobody") == []
    assert match_players(totals, "  ") == []


def test_snap_row_links_the_player_when_a_roster_match_exists() -> None:
    roster = {
        "roquan smith": PlayerRef(
            name="Roquan Smith", athlete_id="3915511", position="ILB"
        )
    }
    report = build_report(
        _ravens_game(), parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"], roster
    )

    row = format_snap_row(report.unit(DEFENSE)[0], report, DEFENSE)

    assert row == (
        "LB [Roquan Smith](https://www.espn.com/nfl/player/_/id/3915511) — 60 of 60 (100%)"
    )
