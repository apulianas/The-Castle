from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ravens_bot.embeds import (
    MAX_DESCRIPTION_CHARS,
    MAX_EMBED_CHARS,
    MAX_EMBED_FIELDS,
    MAX_FIELD_CHARS,
    _limit_description,
    _limit_field,
    FOURTH_DOWN_FOOTER,
    error_embed,
    fourth_down_embed,
    help_embed,
    inactive_embeds,
    next_game_embed,
    no_fourth_down_embed,
    player_snap_embed,
    player_snap_totals_embed,
    schedule_embed,
    snap_count_embed,
    snap_totals_embed,
    standings_embed,
    transaction_embeds,
)
from ravens_bot.fourthdown import advise
from ravens_bot.formatting import format_no_live_game
from ravens_bot.models import (
    Game,
    GameSituation,
    GameTeam,
    InactivePlayer,
    InactiveReport,
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
    slug="bal",
    logo="https://example.test/bal.png",
)
JETS_TEAM = TeamRef(
    team_id="20",
    name="New York Jets",
    abbreviation="NYJ",
    slug="nyj",
    logo="https://example.test/nyj.png",
)


def build_game(event_id: str = "9") -> Game:
    return Game(
        event_id=event_id,
        name="New York Jets at Baltimore Ravens",
        short_name="NYJ @ BAL",
        start_time=datetime(2025, 11, 23, 18, 0, tzinfo=timezone.utc),
        status="Scheduled",
        home=GameTeam(team=RAVENS_TEAM, is_home=True, record="6-5"),
        away=GameTeam(team=JETS_TEAM, is_home=False, record="2-9"),
        venue="M&T Bank Stadium",
        location="Baltimore, MD",
        broadcast="CBS",
        week="Week 12",
    )


def build_transaction(
    index: int = 0, players: tuple[PlayerRef, ...] = ()
) -> Transaction:
    return Transaction(
        transaction_id=f"tx{index}",
        date=date(2025, 11, 4),
        description=f"Signed player {index} to the active roster.",
        players=players,
    )


def field_names(embed) -> list[str]:
    return [field.name for field in embed.fields]


def test_limit_description_marks_where_it_cut() -> None:
    text = "a" * (MAX_DESCRIPTION_CHARS + 50)

    result = _limit_description(text)

    assert len(result) == MAX_DESCRIPTION_CHARS
    assert result.endswith("…")


def test_limit_description_leaves_short_text_untouched() -> None:
    assert _limit_description("short") == "short"


def test_limit_field_respects_the_discord_field_budget() -> None:
    result = _limit_field("b" * (MAX_FIELD_CHARS + 10))

    assert len(result) == MAX_FIELD_CHARS
    assert result.endswith("…")


def test_transaction_embed_says_so_when_there_were_no_moves() -> None:
    embed = transaction_embeds([], date(2025, 11, 4))[0]

    assert embed.title == "Ravens transactions — November 4, 2025"
    assert "No Baltimore Ravens roster transactions" in embed.description
    assert embed.fields == []


def test_transaction_embed_adds_one_field_per_move() -> None:
    embed = transaction_embeds(
        [build_transaction(0), build_transaction(1)], date(2025, 11, 4)
    )[0]

    assert len(embed.fields) == 2
    assert embed.url is not None


def test_solo_transaction_gets_a_full_size_headshot() -> None:
    """A signing is about one person, so their photo carries the post."""
    player = PlayerRef(name="Keondre Jackson", athlete_id="4878287")

    embed = transaction_embeds(
        [build_transaction(players=(player,))], date(2025, 11, 4)
    )[0]

    assert embed.image.url is not None
    assert "4878287" in embed.image.url
    assert embed.thumbnail.url is None


def test_multi_player_move_uses_a_thumbnail_instead() -> None:
    """One face would misrepresent a post covering several players."""
    players = (
        PlayerRef(name="Player One", athlete_id="1"),
        PlayerRef(name="Player Two", athlete_id="2"),
    )

    embed = transaction_embeds(
        [build_transaction(players=players)], date(2025, 11, 4)
    )[0]

    assert embed.image.url is None
    assert embed.thumbnail.url is not None and "full%2F1.png" in embed.thumbnail.url


def test_transaction_without_a_resolved_player_falls_back_to_the_logo() -> None:
    embed = transaction_embeds([build_transaction()], date(2025, 11, 4))[0]

    assert embed.image.url is None
    assert embed.thumbnail.url is not None and "bal" in embed.thumbnail.url


def test_transaction_embed_reports_how_many_moves_were_hidden() -> None:
    """Discord caps an embed at 25 fields, so the count has to be stated."""
    transactions = [build_transaction(index) for index in range(30)]

    embed = transaction_embeds(transactions, date(2025, 8, 26))[0]

    assert len(embed.fields) == MAX_EMBED_FIELDS
    assert embed.footer.text.startswith("Showing 25 of 30 moves")


def test_transaction_embed_footer_is_plain_when_nothing_is_hidden() -> None:
    embed = transaction_embeds([build_transaction()], date(2025, 11, 4))[0]

    assert embed.footer.text == "Data: ESPN"


def test_transaction_field_names_stay_within_the_title_budget() -> None:
    transaction = Transaction(
        transaction_id="tx",
        date=date(2025, 11, 4),
        description="x" * 5000,
    )

    embed = transaction_embeds([transaction], date(2025, 11, 4))[0]

    assert len(embed.fields[0].name) <= 256
    assert len(embed.fields[0].value) <= MAX_FIELD_CHARS


def test_standings_embed_lists_every_team_and_summarises_the_ravens() -> None:
    standings = [
        Standing(team=RAVENS_TEAM, record="12-5", rank=1, division_record="4-2"),
        Standing(
            team=TeamRef(team_id="23", name="Pittsburgh Steelers", abbreviation="PIT"),
            record="10-7",
            rank=2,
        ),
    ]

    embed = standings_embed(standings)

    assert "Baltimore Ravens" in embed.description
    assert "Pittsburgh Steelers" in embed.description
    assert field_names(embed) == ["BAL"]
    assert embed.footer.text.startswith("Baltimore Ravens: 12-5")


def test_standings_embed_handles_an_empty_division() -> None:
    embed = standings_embed([])

    assert "unavailable" in embed.description
    assert embed.fields == []


def test_next_game_embed_shows_kickoff_venue_and_records() -> None:
    embed = next_game_embed(build_game(), EASTERN)

    assert embed.title == "Baltimore Ravens vs New York Jets"
    assert "CBS" in embed.description
    assert "M&T Bank Stadium (Baltimore, MD)" in embed.description
    assert "NYJ 2-9 • BAL 6-5" in embed.description
    assert embed.url == "https://www.espn.com/nfl/game/_/gameId/9"


def test_next_game_embed_uses_the_opponent_logo() -> None:
    """The Ravens are in every post, so the opponent is the informative crest."""
    embed = next_game_embed(build_game(), EASTERN)

    assert embed.thumbnail.url == "https://example.test/nyj.png"


def test_next_game_embed_handles_an_empty_schedule() -> None:
    embed = next_game_embed(None, EASTERN)

    assert embed.description == "No upcoming Ravens game found."
    assert embed.thumbnail.url is not None


def test_schedule_embed_adds_one_field_per_game() -> None:
    embed = schedule_embed([build_game("1"), build_game("2")], EASTERN)

    assert len(embed.fields) == 2
    assert embed.fields[0].name == "Week 12 — Baltimore Ravens vs New York Jets"


def test_schedule_embed_reports_the_window_it_searched() -> None:
    embed = schedule_embed([], EASTERN, days=14)

    assert embed.description == "No Ravens games scheduled in the next 14 days."


def test_schedule_embed_lists_every_game_past_the_field_limit() -> None:
    games = [build_game(str(index)) for index in range(30)]

    embed = schedule_embed(games, EASTERN)

    assert len(embed.fields) == 0
    assert embed.description is not None
    assert len(embed.description.splitlines()) == 30
    assert embed.footer.text == "Data: ESPN"


def test_schedule_embed_reports_games_that_did_not_fit() -> None:
    games = [build_game(str(index)) for index in range(400)]

    embed = schedule_embed(games, EASTERN)

    shown = len(embed.description.splitlines())
    assert shown < 400
    assert embed.footer.text.startswith(f"Showing {shown} of 400 games")


def test_inactive_embed_groups_players_by_team() -> None:
    report = InactiveReport(
        game=build_game(),
        players=[
            InactivePlayer(
                name="Raven One",
                team="Baltimore Ravens",
                reason="Healthy scratch",
                position="WR",
            ),
            InactivePlayer(name="Raven Two", team="Baltimore Ravens"),
            InactivePlayer(name="Jet One", team="New York Jets"),
        ],
    )

    embed = inactive_embeds([report], date(2025, 11, 23), EASTERN)[0]

    assert field_names(embed) == ["Baltimore Ravens (2)", "New York Jets (1)"]
    assert "WR Raven One — Healthy scratch" in embed.fields[0].value


def test_inactive_embed_explains_an_unpublished_list() -> None:
    embed = inactive_embeds(
        [InactiveReport(game=build_game(), players=[])], date(2025, 11, 23), EASTERN
    )[0]

    assert "has not published" in embed.fields[0].value


def test_inactive_embeds_report_when_there_is_no_game() -> None:
    embed = inactive_embeds([], date(2025, 11, 23), EASTERN)[0]

    assert "No Baltimore Ravens game found" in embed.description


def test_error_embed_is_visually_distinct() -> None:
    embed = error_embed("ESPN did not respond.")

    assert embed.description == "ESPN did not respond."
    assert embed.color.value == 0xB00020


def test_help_embed_documents_every_command() -> None:
    names = {field.name.split()[0] for field in help_embed().fields}

    assert names == {
        "/transactions",
        "/inactives",
        "/injuries",
        "/standings",
        "/nextgame",
        "/live",
        "/schedule",
        "/snapcounts",
        "/fourthdown",
        "/help",
    }


def _snap_report(players: tuple[PlayerSnaps, ...]) -> SnapCountReport:
    game = Game(
        event_id="77",
        name="Baltimore Ravens at Cleveland Browns",
        short_name="BAL @ CLE",
        start_time=datetime(2025, 9, 14, 17, 0, tzinfo=timezone.utc),
        status="Final",
        home=GameTeam(team=JETS_TEAM, is_home=True, score=10),
        away=GameTeam(team=RAVENS_TEAM, score=27, is_winner=True),
        state="post",
        completed=True,
        week="Week 2",
        season=2025,
        season_type=2,
    )
    return SnapCountReport(
        game=game,
        players=players,
        offense_total=68,
        defense_total=60,
        special_teams_total=25,
    )


def test_snap_count_embed_groups_players_by_unit() -> None:
    report = _snap_report(
        (
            PlayerSnaps(
                player=PlayerRef(name="Lamar Jackson", position="QB", athlete_id="1"),
                offense=68,
            ),
            PlayerSnaps(
                player=PlayerRef(name="Roquan Smith", position="ILB"), defense=60
            ),
            PlayerSnaps(player=PlayerRef(name="Nick Moore", position="LS"), special_teams=25),
        )
    )

    embed = snap_count_embed(report)

    assert [field.name for field in embed.fields] == [
        "Offense (68 snaps)",
        "Defense (60 snaps)",
        "Special teams (25 snaps)",
    ]
    assert "68 of 68 (100%)" in (embed.fields[0].value or "")
    assert embed.footer.text == "Data: NFL game book via nflverse"


def test_snap_count_embed_states_a_game_with_no_published_snaps() -> None:
    embed = snap_count_embed(_snap_report(()))

    assert embed.fields == []
    assert embed.description is not None
    assert "have not been published" in embed.description


def test_snap_count_embed_keeps_a_full_roster_inside_discord_limits() -> None:
    players = tuple(
        PlayerSnaps(
            player=PlayerRef(name=f"Player Number{index:02d}", position="WR"),
            offense=60 - index,
        )
        for index in range(48)
    )

    embed = snap_count_embed(_snap_report(players))

    assert len(embed.fields) <= MAX_EMBED_FIELDS
    assert all(len(field.value or "") <= MAX_FIELD_CHARS for field in embed.fields)
    assert [field.name for field in embed.fields][:2] == [
        "Offense (68 snaps)",
        "Offense (68 snaps) (cont.)",
    ]
    assert embed.footer.text == "Data: NFL game book via nflverse"


def test_snap_count_embed_reports_players_it_had_to_hide() -> None:
    players = tuple(
        PlayerSnaps(
            player=PlayerRef(name=f"Player Number{index:03d} Of The Baltimore Ravens"),
            offense=1,
        )
        for index in range(900)
    )

    embed = snap_count_embed(_snap_report(players))

    assert len(embed) <= MAX_EMBED_CHARS
    assert len(embed.fields) <= MAX_EMBED_FIELDS
    assert embed.footer.text is not None
    assert embed.footer.text.startswith("Showing ")
    assert "of 900 players" in embed.footer.text


def test_snap_totals_embed_stays_inside_the_whole_embed_budget() -> None:
    entries = tuple(
        PlayerSnaps(
            player=PlayerRef(
                name=f"Player Number{index:03d}",
                position="WR",
                athlete_id=f"44296{index:03d}",
            ),
            offense=60 - index % 40,
        )
        for index in range(70)
    )
    report = _snap_report(entries)
    totals = [
        PlayerSnapTotals(
            player=entry.player,
            entries=tuple((report.game, entry) for _ in range(18)),
            offense=entry.offense * 18,
            offense_total=68 * 18,
        )
        for entry in entries
    ]

    embed = snap_totals_embed(totals, [report], 18)

    assert len(embed) <= MAX_EMBED_CHARS
    assert embed.footer.text is not None
    assert embed.footer.text.startswith("Showing ")


def test_player_snap_embed_uses_the_feature_headshot() -> None:
    entry = PlayerSnaps(
        player=PlayerRef(name="Zay Flowers", position="WR", athlete_id="4429615"),
        offense=54,
        special_teams=3,
    )
    report = _snap_report((entry,))

    embed = player_snap_embed(entry, report)

    assert embed.image.url is not None
    assert "w=520" in embed.image.url
    assert embed.fields[0].value == (
        "Offense: 54 of 68 (79%) snaps\nSpecial teams: 3 of 25 (12%) snaps"
    )


def test_player_snap_embed_falls_back_to_the_team_logo() -> None:
    entry = PlayerSnaps(player=PlayerRef(name="Unrostered Player"), offense=4)

    embed = player_snap_embed(entry, _snap_report((entry,)))

    assert embed.image.url is None
    assert embed.thumbnail.url is not None


def test_player_snap_totals_embed_breaks_the_period_down_by_game() -> None:
    entry = PlayerSnaps(player=PlayerRef(name="Zay Flowers", position="WR"), offense=54)
    report = _snap_report((entry,))
    totals = PlayerSnapTotals(
        player=entry.player,
        entries=((report.game, entry),),
        offense=54,
        offense_total=68,
    )

    embed = player_snap_totals_embed(totals, [report], 4)

    assert [field.name for field in embed.fields] == ["Totals", "By game"]
    assert "Week 2" in (embed.fields[1].value or "")


def test_snap_totals_embed_lists_the_games_it_covered() -> None:
    entry = PlayerSnaps(player=PlayerRef(name="Zay Flowers", position="WR"), offense=54)
    report = _snap_report((entry,))
    totals = PlayerSnapTotals(
        player=entry.player,
        entries=((report.game, entry),),
        offense=54,
        offense_total=68,
    )

    embed = snap_totals_embed([totals], [report], 3)

    assert embed.title == "Ravens snap counts — last 3 games"
    assert embed.description is not None and "Week 2" in embed.description
    assert "over 1 game" in (embed.fields[0].value or "")


def build_situation(down: int = 4, distance: int = 3, yards_to_goal: int = 10) -> GameSituation:
    return GameSituation(
        possession=RAVENS_TEAM,
        defense=JETS_TEAM,
        down=down,
        distance=distance,
        yards_to_goal=yards_to_goal,
        period=3,
        clock="5:21",
        score_differential=4,
        spot="NYJ 10",
        down_distance_text="4th & 3",
    )


def test_fourth_down_embed_states_the_call_and_prices_every_option() -> None:
    game = build_game()
    advice = advise(build_situation())

    embed = fourth_down_embed(game, advice)

    assert embed.title == "Field goal"
    assert "4th & 3" in embed.description
    assert field_names(embed) == ["Field goal", "Go for it", "Punt"]
    assert all("expected points" in field.value for field in embed.fields)
    assert embed.thumbnail.url == RAVENS_TEAM.logo
    assert embed.url == "https://www.espn.com/nfl/game/_/gameId/9"
    assert embed.footer.text == FOURTH_DOWN_FOOTER


def test_fourth_down_embed_adds_a_field_for_every_caveat() -> None:
    advice = advise(
        GameSituation(
            possession=RAVENS_TEAM,
            down=4,
            distance=2,
            yards_to_goal=40,
            period=4,
            score_differential=-9,
        )
    )

    embed = fourth_down_embed(build_game(), advice)

    assert field_names(embed).count("Worth knowing") == len(advice.caveats)
    assert advice.caveats


def test_no_fourth_down_embed_falls_back_to_a_logo_without_a_game() -> None:
    embed = no_fourth_down_embed(format_no_live_game())

    assert embed.title == "Fourth down"
    assert "No NFL game" in embed.description
    assert embed.thumbnail.url is not None
