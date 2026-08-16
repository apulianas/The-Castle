from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ravens_bot.formatting import (
    MAX_LINKED_PLAYERS,
    NO_STAT,
    format_differential,
    format_expected_points,
    format_fourth_down,
    format_fourth_down_call,
    format_fourth_down_option,
    format_fourth_down_situation,
    format_full_date,
    format_game_state,
    format_game_time,
    format_games_back,
    format_inactive_player,
    format_injury_detail,
    format_long_date,
    format_matchup,
    format_no_snap_counts,
    format_no_snap_games,
    format_player_snap_totals,
    format_player_snaps,
    format_ravens_standing,
    format_records,
    format_snap_breakdown,
    format_snap_period,
    format_snap_share,
    format_score,
    format_standings_detail,
    format_standings_row,
    format_transaction,
    format_transaction_detail,
    format_unknown_snap_player,
    format_unknown_team,
    format_win_percent,
    ordinal,
    short_team_name,
)
from ravens_bot.fourthdown import advise
from ravens_bot.models import (
    Game,
    GameSituation,
    GameTeam,
    InactivePlayer,
    InjuryUpdate,
    PlayerRef,
    PlayerSnaps,
    PlayerSnapTotals,
    SnapCountReport,
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


def test_transaction_detail_drops_the_opening_the_headline_already_states() -> None:
    transaction = Transaction(
        transaction_id="tx1",
        date=date(2025, 11, 4),
        description="Signed WR Devontez Walker to the active roster.",
        type_text="Signed",
        players=(PlayerRef(name="Devontez Walker", athlete_id="1", position="WR"),),
    )

    assert transaction.headline == "Signed — WR Devontez Walker"
    assert format_transaction_detail(transaction) == "To the active roster."


def test_transaction_detail_is_empty_when_the_headline_says_everything() -> None:
    """"Re-signed WR Tylan Wallace." leaves only the full stop behind."""
    transaction = Transaction(
        transaction_id="tx1",
        date=date(2025, 11, 4),
        description="Re-signed WR Tylan Wallace.",
        type_text="Re-signed",
        players=(PlayerRef(name="Tylan Wallace", athlete_id="1", position="WR"),),
    )

    assert format_transaction_detail(transaction) == ""


def test_transaction_detail_keeps_the_prose_of_a_compound_move() -> None:
    """Trimming a move naming two players would lose one of them."""
    players = (
        PlayerRef(name="Ronnie Stanley", athlete_id="1", position="OT"),
        PlayerRef(name="Carson Vinson", athlete_id="2", position="OT"),
    )
    transaction = Transaction(
        transaction_id="tx1",
        date=date(2025, 11, 4),
        description=(
            "Placed OT Ronnie Stanley on injured reserve and signed "
            "OT Carson Vinson to the active roster."
        ),
        type_text="Placed",
        players=players,
    )

    detail = format_transaction_detail(transaction)

    assert "Ronnie Stanley" in detail
    assert "Carson Vinson" in detail


def test_transaction_detail_keeps_prose_that_does_not_open_with_the_headline() -> None:
    transaction = Transaction(
        transaction_id="tx1",
        date=date(2025, 11, 4),
        description="The Ravens signed WR Devontez Walker.",
        type_text="Signed",
        players=(PlayerRef(name="Devontez Walker", athlete_id="1", position="WR"),),
    )

    assert format_transaction_detail(transaction).startswith("The Ravens signed")


def test_injury_detail_leaves_the_player_out() -> None:
    update = InjuryUpdate(
        player=PlayerRef(name="Zay Flowers", athlete_id="1", position="WR"),
        status="Questionable",
        detail="Knee",
        comment="Limited on Thursday.",
    )

    assert format_injury_detail(update) == "Knee · Limited on Thursday."
    assert format_injury_detail(InjuryUpdate(player=PlayerRef(name="A B"))) == ""


def test_short_team_name_keeps_only_the_nickname() -> None:
    assert short_team_name("Baltimore Ravens") == "Ravens"
    assert short_team_name("New York Jets") == "Jets"
    assert short_team_name(None) == "Team"


def test_game_state_drops_a_status_the_kickoff_line_already_implies() -> None:
    def game(status: str, state: str) -> Game:
        return Game(
            event_id="9",
            name="New York Jets at Baltimore Ravens",
            short_name="NYJ @ BAL",
            start_time=None,
            status=status,
            state=state,
            week="Week 12",
        )

    assert format_game_state(game("Scheduled", "pre")) == "Week 12"
    assert format_game_state(game("In Progress", "in")) == "In Progress • Week 12"


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


def _snap_game(week: str | None = "Week 2") -> Game:
    return Game(
        event_id="9",
        name="Baltimore Ravens at Cleveland Browns",
        short_name="BAL @ CLE",
        start_time=None,
        status="Final",
        home=GameTeam(team=TeamRef(name="Cleveland Browns", team_id="5"), is_home=True),
        away=GameTeam(
            team=TeamRef(name="Baltimore Ravens", team_id="33"), is_winner=True
        ),
        state="post",
        completed=True,
        week=week,
    )


def test_format_snap_share_states_the_share_and_the_counts() -> None:
    assert format_snap_share(42, 68) == "42 of 68 (62%)"
    assert format_snap_share(68, 68) == "68 of 68 (100%)"
    assert format_snap_share(0, 68) == "0 of 68 (0%)"


def test_format_snap_share_drops_an_unusable_denominator() -> None:
    assert format_snap_share(5, 0) == "5"
    assert format_snap_share(5, -1) == "5"
    assert format_snap_share(70, 68) == "70"


def test_format_player_snaps_lists_only_units_played() -> None:
    entry = PlayerSnaps(
        player=PlayerRef(name="Zay Flowers", position="WR"), offense=54, special_teams=3
    )
    report = SnapCountReport(
        game=_snap_game(),
        players=(entry,),
        offense_total=68,
        defense_total=60,
        special_teams_total=25,
    )

    assert format_player_snaps(entry, report) == (
        "Offense: 54 of 68 (79%) snaps\nSpecial teams: 3 of 25 (12%) snaps"
    )


def test_format_player_snaps_states_a_player_who_never_took_the_field() -> None:
    entry = PlayerSnaps(player=PlayerRef(name="Deep Reserve"))
    report = SnapCountReport(game=_snap_game(), players=(entry,), offense_total=68)

    assert format_player_snaps(entry, report) == "Did not play a snap."


def test_format_player_snap_totals_sums_across_games() -> None:
    totals = PlayerSnapTotals(
        player=PlayerRef(name="Zay Flowers", position="WR"),
        offense=94,
        offense_total=118,
    )

    assert format_player_snap_totals(totals) == "Offense: 94 of 118 (80%) snaps"


def test_format_snap_breakdown_names_the_week_and_the_matchup() -> None:
    game = _snap_game()
    entry = PlayerSnaps(player=PlayerRef(name="Zay Flowers"), offense=54)
    report = SnapCountReport(game=game, players=(entry,), offense_total=68)

    assert format_snap_breakdown(game, entry, report) == (
        "Week 2 — Baltimore Ravens at Cleveland Browns: 54 of 68 (79%) offense"
    )


def test_format_snap_period_reads_as_a_period() -> None:
    assert format_snap_period(1) == "last game"
    assert format_snap_period(4) == "last 4 games"


def test_snap_empty_states_explain_themselves() -> None:
    assert format_no_snap_games() == (
        "No completed Baltimore Ravens game found to report snap counts for."
    )
    assert format_no_snap_counts() == (
        "Snap counts have not been published for that game yet."
    )
    assert format_no_snap_counts(_snap_game()) == (
        "Snap counts have not been published for "
        "Baltimore Ravens at Cleveland Browns yet."
    )


def test_format_unknown_snap_player_offers_close_names() -> None:
    assert format_unknown_snap_player("Nobody", []) == (
        "No snap counts found for “Nobody”."
    )
    assert format_unknown_snap_player("smith", ["Roquan Smith", "Josh Smith"]) == (
        "No snap counts found for “smith”. Did you mean: Roquan Smith, Josh Smith?"
    )


def build_situation(down: int = 4, distance: int = 3, yards_to_goal: int = 10) -> GameSituation:
    return GameSituation(
        possession=RAVENS_TEAM,
        defense=JETS_TEAM,
        down=down,
        distance=distance,
        yards_to_goal=yards_to_goal,
        period=3,
        clock="5:21",
        score_differential=-4,
        spot="NYJ 10",
        down_distance_text="4th & 3",
    )


def test_format_expected_points_always_states_the_sign() -> None:
    assert format_expected_points(1.24) == "+1.2"
    assert format_expected_points(-0.61) == "-0.6"
    assert format_expected_points(0.0) == "0.0"
    assert format_expected_points(float("-inf")) == "—"


def test_format_fourth_down_option_bolds_the_recommended_one() -> None:
    advice = advise(build_situation())
    best = advice.options[0]

    assert format_fourth_down_option(best, best=True).startswith("**")
    assert not format_fourth_down_option(best).startswith("**")
    assert "win probability" in format_fourth_down_option(best)


def test_format_fourth_down_option_prices_a_clockless_down_in_points() -> None:
    advice = advise(replace(build_situation(), clock=None, clock_seconds=None))

    assert "expected points" in format_fourth_down_option(advice.options[0])


def test_format_fourth_down_situation_names_the_teams_and_the_score() -> None:
    text = format_fourth_down_situation(build_game(ravens_score=17, jets_score=21), build_situation())

    assert "4th & 3" in text
    assert "New York Jets at Baltimore Ravens" in text
    assert "NYJ 21" in text and "BAL 17" in text
    assert "trailing by 4" in text


def test_format_fourth_down_call_hedges_a_close_one() -> None:
    advice = advise(build_situation(distance=2, yards_to_goal=10))

    assert advice.is_close
    assert format_fourth_down_call(advice).startswith("Too close to call")


def test_format_fourth_down_lists_the_call_then_every_option() -> None:
    advice = advise(build_situation(distance=6, yards_to_goal=10))

    lines = format_fourth_down(build_game(), advice).splitlines()

    assert lines[0] == "Field goal"
    assert [line.split(":")[0] for line in lines[-3:]] == ["Field goal", "Go for it", "Punt"]


def test_format_unknown_team_offers_who_is_playing() -> None:
    assert "Playing right now: Baltimore Ravens" in format_unknown_team("Seahawks", ["Baltimore Ravens"])
    assert format_unknown_team("Seahawks", []).endswith("“Seahawks”.")
