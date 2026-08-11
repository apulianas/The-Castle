from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ravens_bot.formatting import (
    MAX_LINKED_PLAYERS,
    NO_STAT,
    format_differential,
    format_full_date,
    format_game_time,
    format_games_back,
    format_inactive_player,
    format_long_date,
    format_matchup,
    format_ravens_standing,
    format_records,
    format_score,
    format_standings_detail,
    format_standings_row,
    format_transaction,
    format_win_percent,
    ordinal,
)
from ravens_bot.models import (
    Game,
    GameTeam,
    InactivePlayer,
    PlayerRef,
    Standing,
    TeamRef,
    Transaction,
)


EASTERN = ZoneInfo("America/New_York")

RAVENS_TEAM = TeamRef(
    team_id="33",
    name="Baltimore Ravens",
    abbreviation="BAL",
    logo="https://example.test/bal.png",
    link="https://www.espn.com/nfl/team/_/name/bal",
)
JETS_TEAM = TeamRef(
    team_id="20",
    name="New York Jets",
    abbreviation="NYJ",
    link="https://www.espn.com/nfl/team/_/name/nyj",
)


def build_game(
    ravens_score: int | None = None,
    jets_score: int | None = None,
    ravens_won: bool = False,
    jets_won: bool = False,
    ravens_home: bool = True,
    start_time: datetime | None = None,
) -> Game:
    ravens = GameTeam(
        team=RAVENS_TEAM,
        score=ravens_score,
        is_home=ravens_home,
        is_winner=ravens_won,
        record="6-5",
    )
    jets = GameTeam(
        team=JETS_TEAM,
        score=jets_score,
        is_home=not ravens_home,
        is_winner=jets_won,
        record="2-9",
    )
    home, away = (ravens, jets) if ravens_home else (jets, ravens)
    return Game(
        event_id="9",
        name="New York Jets at Baltimore Ravens",
        short_name="NYJ @ BAL",
        start_time=start_time,
        status="Final",
        home=home,
        away=away,
    )


def build_standing(
    name: str = "Baltimore Ravens",
    rank: int | None = 1,
    is_ravens: bool = True,
    **kwargs: object,
) -> Standing:
    defaults: dict[str, object] = {
        "team": RAVENS_TEAM if is_ravens else TeamRef(team_id="4", name=name),
        "record": "12-5",
        "rank": rank,
    }
    defaults.update(kwargs)
    return Standing(**defaults)  # type: ignore[arg-type]


def test_date_helpers_avoid_platform_specific_padding() -> None:
    """%-d is glibc only, so the helpers have to strip padding themselves."""
    assert format_long_date(date(2025, 11, 4)) == "November 4, 2025"
    assert format_full_date(date(2025, 11, 4)) == "Tuesday, November 4, 2025"


def test_format_game_time_converts_to_the_requested_zone() -> None:
    game = build_game(start_time=datetime(2025, 11, 23, 18, 0, tzinfo=timezone.utc))

    assert format_game_time(game, EASTERN) == "Sun, Nov 23 at 1:00 PM EST"


def test_format_game_time_reports_missing_kickoff() -> None:
    assert format_game_time(build_game(), EASTERN) == "Time TBA"


def test_format_matchup_states_home_and_away_from_the_ravens_side() -> None:
    assert format_matchup(build_game(ravens_home=True)) == (
        "Baltimore Ravens vs New York Jets"
    )
    assert format_matchup(build_game(ravens_home=False)) == (
        "Baltimore Ravens at New York Jets"
    )


def test_format_score_reads_from_the_ravens_side() -> None:
    assert format_score(build_game(23, 10, ravens_won=True)) == "W 23-10"
    assert format_score(build_game(10, 23, jets_won=True)) == "L 10-23"
    assert format_score(build_game(20, 20)) == "T 20-20"


def test_format_score_is_empty_before_kickoff() -> None:
    assert format_score(build_game()) is None


def test_format_records_lists_both_sides() -> None:
    assert format_records(build_game()) == "NYJ 2-9 • BAL 6-5"


def test_format_transaction_links_named_players() -> None:
    transaction = Transaction(
        transaction_id="tx1",
        date=date(2025, 11, 4),
        description="Signed S Keondre Jackson to the active roster.",
        players=(PlayerRef(name="Keondre Jackson", athlete_id="4878287"),),
    )

    assert format_transaction(transaction) == (
        "Signed S [Keondre Jackson]"
        "(https://www.espn.com/nfl/player/_/id/4878287) to the active roster."
    )


def test_format_transaction_leaves_unresolved_players_alone() -> None:
    transaction = Transaction(
        transaction_id="tx1",
        date=date(2025, 11, 4),
        description="Signed S Keondre Jackson to the active roster.",
        players=(PlayerRef(name="Keondre Jackson"),),
    )

    assert format_transaction(transaction) == transaction.description


def test_format_transaction_skips_links_on_a_mass_roster_cut() -> None:
    """Link markup on twenty names would crowd out the wording itself."""
    players = tuple(
        PlayerRef(name=f"Player {index}", athlete_id=str(index))
        for index in range(MAX_LINKED_PLAYERS + 1)
    )
    description = "Waived " + ", ".join(player.name for player in players)
    transaction = Transaction(
        transaction_id="tx2",
        date=date(2025, 8, 26),
        description=description,
        players=players,
    )

    assert format_transaction(transaction) == description


def test_transaction_headline_names_a_solo_move() -> None:
    transaction = Transaction(
        transaction_id="tx1",
        date=date(2025, 11, 4),
        description="Signed S Keondre Jackson to the active roster.",
        players=(PlayerRef(name="Keondre Jackson", position="S"),),
    )

    assert "Keondre Jackson" in transaction.headline


def test_format_inactive_player_prefixes_the_position() -> None:
    player = InactivePlayer(
        name="Raven One",
        team="Baltimore Ravens",
        reason="Healthy scratch",
        athlete_id="77",
        position="WR",
    )

    assert format_inactive_player(player) == (
        "WR [Raven One](https://www.espn.com/nfl/player/_/id/77) — Healthy scratch"
    )


def test_format_games_back_treats_a_leader_as_even() -> None:
    """ESPN sends "-" for the division leader, which is a zero, not a gap."""
    assert format_games_back("-") == NO_STAT
    assert format_games_back("0.0") == NO_STAT
    assert format_games_back(None) == NO_STAT
    assert format_games_back("2.5") == "2.5"


def test_format_win_percent_drops_the_leading_zero() -> None:
    assert format_win_percent("0.706") == ".706"
    assert format_win_percent(None) == NO_STAT


def test_format_differential_signs_a_positive_margin() -> None:
    assert format_differential(42) == "+42"
    assert format_differential(-13) == "-13"
    assert format_differential(None) == NO_STAT


def test_format_standings_row_bolds_the_ravens() -> None:
    row = format_standings_row(
        build_standing(win_percent="0.706", games_back="-", streak="W2")
    )

    assert row == (
        "1. **[Baltimore Ravens](https://www.espn.com/nfl/team/_/name/bal)** — "
        "12-5 (.706), GB —, W2"
    )


def test_format_standings_row_leaves_other_teams_unbolded() -> None:
    row = format_standings_row(
        build_standing(name="Pittsburgh Steelers", rank=2, is_ravens=False)
    )

    assert "**" not in row
    assert row.startswith("2. Pittsburgh Steelers")


def test_format_standings_row_notes_a_clinch() -> None:
    row = format_standings_row(build_standing(clinch="z"))

    assert "(z)" in row


def test_format_standings_detail_lists_available_splits_only() -> None:
    detail = format_standings_detail(
        build_standing(division_record="4-2", home_record="7-1")
    )

    assert detail == "Div 4-2 • Home 7-1"


def test_format_ravens_standing_summarises_the_division_position() -> None:
    standings = [
        build_standing(name="Pittsburgh Steelers", rank=1, is_ravens=False),
        build_standing(rank=2, playoff_seed=5, differential=42),
    ]

    assert format_ravens_standing(standings) == (
        "Baltimore Ravens: 12-5 • 2nd in the AFC North • seed 5 • point diff +42"
    )


def test_format_ravens_standing_is_empty_when_the_team_is_missing() -> None:
    steelers = build_standing(name="Pittsburgh Steelers", is_ravens=False)

    assert format_ravens_standing([steelers]) is None


def test_ordinal_handles_the_teens() -> None:
    assert [ordinal(number) for number in (1, 2, 3, 4, 11, 12, 13, 21)] == [
        "1st",
        "2nd",
        "3rd",
        "4th",
        "11th",
        "12th",
        "13th",
        "21st",
    ]
