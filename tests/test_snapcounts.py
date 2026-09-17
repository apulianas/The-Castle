from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from ravens_bot.bot import _recent_snap_reports, _snapcounts_command
from ravens_bot.embeds import (
    MAX_EMBED_CHARS,
    MAX_EMBED_FIELDS,
    MAX_FIELD_CHARS,
    player_snap_embed,
    player_snap_totals_embed,
    snap_count_embed,
    snap_count_embeds,
    snap_totals_embed,
    snap_totals_embeds,
)
from ravens_bot.espn import parse_roster
from ravens_bot.formatting import format_snap_changes, format_snap_row
from ravens_bot.models import (
    DEFENSE,
    OFFENSE,
    SPECIAL_TEAMS,
    Game,
    GameTeam,
    PlayerRef,
    SnapCountReport,
    TeamRef,
)
from ravens_bot.snapcounts import (
    PLAYERS_URL,
    SNAP_COUNTS_URL,
    SnapCountClient,
    SnapCountError,
    aggregate,
    build_report,
    match_game,
    match_players,
    parse_snap_counts,
    parse_players,
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


@pytest.mark.parametrize("row", [
    ",x,2025,REG,2,No Game Id,,QB,BAL,CLE,10,1,0,0,0,0",
    "2025_02_BAL_CLE,x,2025,REG,2,,,QB,BAL,CLE,10,1,0,0,0,0",
    "2025_02_BAL_CLE,x,2025,REG,2,Lamar Jackson,JackLa00,QB,BAL,CLE,ten,,0,0,0,0",
])
def test_parse_snap_counts_rejects_unreadable_rows(row: str) -> None:
    with pytest.raises(SnapCountError):
        parse_snap_counts(HEADER + row)


def test_parse_snap_counts_returns_nothing_for_a_missing_section() -> None:
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


def test_report_units_include_every_participant_sorted_by_unit_snaps() -> None:
    report = build_report(_ravens_game(), parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"])

    assert [entry.name for entry in report.unit(OFFENSE)] == [
        "Lamar Jackson",
        "Zay Flowers",
    ]
    assert [entry.name for entry in report.unit(DEFENSE)] == ["Roquan Smith"]
    assert [entry.name for entry in report.unit(SPECIAL_TEAMS)] == [
        "Nick Moore", "Zay Flowers", "Roquan Smith",
    ]


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


PLAYERS = (
    "pfr_id,espn_id,display_name,position\n"
    "JackLa00,3916387,Lamar Jackson,QB\n"
    "FlowZa00,4429615,Zay Flowers,WR\n"
    "MoorNi00,4569987,Nick Moore,LS\n"
)


def _report() -> SnapCountReport:
    return build_report(_ravens_game(), parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"])


@pytest.mark.parametrize("text", ["", "<html>Bad Gateway</html>", "team,player\nBAL,Lamar"])
def test_snap_schema_errors_are_not_unpublished_data(text: str) -> None:
    with pytest.raises(SnapCountError, match="required columns"):
        parse_snap_counts(text)


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "1.2", "unknown"])
def test_invalid_counts_are_source_errors(value: str) -> None:
    with pytest.raises(SnapCountError, match="invalid count"):
        parse_snap_counts(SAMPLE.replace(",68,1,", f",{value},1,"))


@pytest.mark.parametrize("value", ["nan", "inf", "-0.1", "1.2", "unknown"])
def test_invalid_shares_are_source_errors(value: str) -> None:
    with pytest.raises(SnapCountError, match="invalid share"):
        parse_snap_counts(SAMPLE.replace(",68,1,", f",68,{value},"))


def test_missing_share_is_not_zero_or_an_invented_denominator() -> None:
    snaps = next(iter(parse_snap_counts(
        HEADER + "2025_02_BAL_CLE,x,2025,REG,2,Lamar Jackson,JackLa00,QB,BAL,CLE,68,,0,0,0,0"
    ).values()))
    assert snaps.players[0].offense == 68
    assert snaps.players[0].offense_share is None
    assert snaps.totals[OFFENSE] == 0


def test_aggregate_does_not_treat_missing_denominator_as_zero() -> None:
    first = _report()
    unknown = replace(first, game=_ravens_game(event_id="2"), offense_total=0)
    lamar = next(total for total in aggregate([first, unknown])
                 if total.player.name == "Lamar Jackson")
    assert lamar.offense == 136
    assert lamar.offense_total == 0


def test_crosswalk_resolves_historical_player_by_id_despite_roster_name_collision() -> None:
    snaps = parse_snap_counts(SAMPLE)["2025_02_BAL_CLE"]
    roster = {"lamar jackson": PlayerRef("Lamar Jackson", athlete_id="999", headshot="wrong")}
    report = build_report(_ravens_game(), snaps, roster, parse_players(PLAYERS))
    player = report.players[0]
    assert player.pfr_id == "JackLa00"
    assert player.player.athlete_id == "3916387"
    assert player.player.headshot is None
    assert "3916387" in player.player.photo_url()
    assert "3916387" in player.player.page_url
    assert player.offense == 68
    assert player.offense_share == 1


def test_crosswalk_id_does_not_require_matching_name() -> None:
    snaps = parse_snap_counts(SAMPLE.replace("Lamar Jackson", "L. Jackson"))["2025_02_BAL_CLE"]
    report = build_report(_ravens_game(), snaps, crosswalk=parse_players(PLAYERS))
    assert report.players[0].name == "L. Jackson"
    assert report.players[0].player.athlete_id == "3916387"


def test_missing_pfr_id_can_use_unique_crosswalk_name() -> None:
    snaps = parse_snap_counts(SAMPLE.replace("JackLa00", ""))["2025_02_BAL_CLE"]
    report = build_report(_ravens_game(), snaps, crosswalk=parse_players(PLAYERS))
    assert report.players[0].pfr_id == "JackLa00"
    assert report.players[0].player.athlete_id == "3916387"


def test_conflicting_pfr_id_never_falls_back_to_name() -> None:
    snaps = parse_snap_counts(SAMPLE.replace("JackLa00", "Other00"))["2025_02_BAL_CLE"]
    roster = {"lamar jackson": PlayerRef("Lamar Jackson", athlete_id="3916387")}
    report = build_report(_ravens_game(), snaps, roster, parse_players(PLAYERS))
    assert report.players[0].player.athlete_id is None


def test_ambiguous_crosswalk_name_never_uses_current_roster() -> None:
    snaps = parse_snap_counts(SAMPLE.replace("JackLa00", ""))["2025_02_BAL_CLE"]
    crosswalk = parse_players(PLAYERS + "Other00,999,Lamar Jackson,CB\n")
    roster = {"lamar jackson": PlayerRef("Lamar Jackson", athlete_id="3916387")}
    report = build_report(_ravens_game(), snaps, roster, crosswalk)
    assert report.players[0].player.athlete_id is None
    assert report.players[0].pfr_id is None


def test_roster_normalized_name_collisions_are_not_first_match_wins(caplog) -> None:
    roster = parse_roster({"athletes": [{"items": [
        {"id": "1", "fullName": "C.J. Player"},
        {"id": "2", "fullName": "CJ Player"},
        {"id": "3", "fullName": "Someone Else"},
    ]}]})
    assert list(roster) == ["someone else"]
    assert "ambiguous" in caplog.text


@pytest.mark.parametrize("text", [
    "",
    "pfr_id,display_name\nJackLa00,Lamar Jackson",
    PLAYERS + "JackLa00,999,Lamar Jackson,QB\n",
    PLAYERS.replace("3916387", "javascript:bad"),
])
def test_malformed_crosswalk_raises_source_error(text: str) -> None:
    with pytest.raises(SnapCountError):
        parse_players(text)


def test_identity_stays_stable_across_names_but_separates_namesakes() -> None:
    first = _report()
    original = first.players[0]
    renamed = replace(original, player=PlayerRef("L. Jackson"))
    namesake = replace(original, pfr_id="Other00")
    second = replace(first, game=_ravens_game(event_id="2"), players=(renamed, namesake), previous=first)
    totals = aggregate([first, second])
    assert len(totals) == len(first.players) + 1
    assert second.previous_player(renamed) == original
    assert second.previous_player(namesake) is None
    assert next(total for total in totals if total.games == 2).offense == 136


def test_changes_use_published_shares_and_include_explicit_zero_declines() -> None:
    first = _report()
    player = first.players[1]
    current = replace(player, offense_share=0.6, special_teams=0, special_teams_share=0)
    report = replace(first, players=(current,), previous=first)
    assert format_snap_changes(current, report) == " | O -19.0 pp, D 0.0 pp, ST -12.0 pp"
    embed = player_snap_embed(current, report)
    assert "-19.0 pp" in embed.fields[1].value
    assert "-12.0 pp" in embed.fields[1].value
    assert "54 of 68" in embed.fields[0].value
    assert "percentage points" in embed.description
    assert "previous completed game" in embed.description
    assert "2025" in embed.description


def test_missing_share_and_missing_player_are_not_zero() -> None:
    first = _report()
    player = replace(first.players[1], offense_share=None)
    report = replace(first, players=(player,), previous=first)
    assert "O N/A" in format_snap_changes(player, report)
    newcomer = replace(player, pfr_id="New00")
    assert "not listed in prior game" in format_snap_changes(newcomer, report)
    assert "+0" not in format_snap_changes(newcomer, report)


def test_team_report_calls_out_disappearing_players_without_fake_declines() -> None:
    first = _report()
    report = replace(first, players=first.players[1:], previous=first)
    embed = snap_count_embed(report)
    absent = next(field for field in embed.fields if field.name == "Previously listed players")
    assert "Lamar Jackson" in absent.value
    assert "not assumed zero" in absent.value
    assert "-100" not in absent.value


def test_team_report_keeps_explicit_zero_snap_players_and_their_real_decline() -> None:
    first = _report()
    zero = replace(first.players[0], offense=0, offense_share=0)
    report = replace(first, players=(zero,), previous=first)
    embed = snap_count_embed(report)
    field = next(field for field in embed.fields if field.name == "No snaps")
    assert "Lamar Jackson" in field.value
    assert "0 snaps" in field.value
    assert "O -100.0 pp" in field.value


def test_multiweek_breakdown_keeps_all_units_and_unlisted_unpublished_games() -> None:
    first = _report()
    unpublished = SnapCountReport(game=_ravens_game(event_id="2", week="Week 3"), previous=first)
    missing = replace(first, game=_ravens_game(event_id="3", week="Week 4"),
                      players=first.players[:1], previous=unpublished)
    reports = [first, unpublished, missing]
    totals = next(total for total in aggregate(reports) if total.player.name == "Zay Flowers")
    embed = player_snap_totals_embed(totals, reports, 3)
    breakdown = "\n".join(field.value for field in embed.fields[1:])
    assert "special teams" in breakdown and "offense" in breakdown
    assert "Week 3" in breakdown and "not published" in breakdown
    assert "Week 4" in breakdown and "not listed" in breakdown
    assert "1 listed" in embed.description
    team = snap_totals_embed(aggregate(reports), reports, 3)
    assert "2/3 games published" in team.footer.text
    assert "not listed in latest game" in "\n".join(field.value for field in team.fields)


def test_trends_stay_within_discord_limits_for_a_large_roster_and_42_games() -> None:
    first = _report()
    player = first.players[1]
    players = tuple(replace(player, pfr_id=f"P{index}", player=PlayerRef(f"Player Number {index}"))
                    for index in range(100))
    first = replace(first, players=players)
    reports = [replace(first, game=_ravens_game(event_id=str(index)), previous=first)
               for index in range(42)]
    totals = aggregate(reports)
    for embed in (
        snap_count_embed(reports[-1]),
        snap_totals_embed(totals, reports, 42),
        player_snap_totals_embed(totals[0], reports, 42),
    ):
        assert len(embed) <= MAX_EMBED_CHARS
        assert len(embed.fields) <= MAX_EMBED_FIELDS
        assert all(len(field.value) <= MAX_FIELD_CHARS for field in embed.fields)
        assert "Showing " in embed.footer.text


class _Response:
    def __init__(self, text: str, status: int = 200):
        self.body = text
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self):
        return self.body


class _Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def test_client_caches_crosswalk_and_season_but_keeps_unpublished_game_gaps() -> None:
    session = _Session({
        PLAYERS_URL: _Response(PLAYERS),
        SNAP_COUNTS_URL.format(season=2025): _Response(SAMPLE),
    })
    client = SnapCountClient(session)
    games = [_ravens_game(), _ravens_game(event_id="2", opponent="PIT")]

    async def run():
        first = await client.fetch_reports(games)
        second = await client.fetch_reports(games)
        return first, second

    first, second = asyncio.run(run())
    assert [report.game.event_id for report in first] == ["1", "2"]
    assert not first[-1].players
    assert first[-1].previous.game.event_id == "1"
    assert second[0].players[0].player.athlete_id == "3916387"
    assert session.calls.count(PLAYERS_URL) == 1
    assert session.calls.count(SNAP_COUNTS_URL.format(season=2025)) == 1


def test_cross_season_comparison_never_skips_an_unpublished_previous_game() -> None:
    session = _Session({
        PLAYERS_URL: _Response(PLAYERS),
        SNAP_COUNTS_URL.format(season=2024): _Response(SAMPLE.replace("2025", "2024")),
        SNAP_COUNTS_URL.format(season=2025): _Response(SAMPLE),
    })
    client = SnapCountClient(session)
    previous = _ravens_game(event_id="old", season=2024)
    current = _ravens_game(event_id="current")
    direct = asyncio.run(client.fetch_reports([previous, current]))
    assert direct[-1].previous.game.season == 2024
    assert "O 0.0 pp" in format_snap_changes(direct[-1].players[0], direct[-1])
    gap = _ravens_game(event_id="gap", season=2024, opponent="PIT", season_type=3)
    reports = asyncio.run(client.fetch_reports([previous, gap, current]))
    assert reports[-1].previous.game.event_id == "gap"
    assert "prior game unpublished" in format_snap_changes(reports[-1].players[0], reports[-1])


@pytest.mark.parametrize("response", [
    _Response("", 404), _Response("", 503), aiohttp.ClientConnectionError("offline"), TimeoutError(),
])
def test_crosswalk_outage_logs_and_retains_counts_with_roster_fallback(response, caplog) -> None:
    session = _Session({
        PLAYERS_URL: response,
        SNAP_COUNTS_URL.format(season=2025): _Response(SAMPLE),
    })
    reports = asyncio.run(SnapCountClient(session).fetch_reports(
        [_ravens_game()], {"lamar jackson": PlayerRef("Lamar Jackson", athlete_id="3916387")}
    ))
    assert reports[0].players[0].offense == 68
    assert reports[0].players[0].player.athlete_id == "3916387"
    assert "without nflverse player crosswalk" in caplog.text


def test_crosswalk_schema_error_is_not_swallowed_as_optional_outage() -> None:
    session = _Session({PLAYERS_URL: _Response("bad,schema\none,two")})
    with pytest.raises(SnapCountError, match="required columns"):
        asyncio.run(SnapCountClient(session).fetch_reports([_ravens_game()]))


def test_missing_season_404_is_pending_but_empty_success_is_source_error() -> None:
    url = SNAP_COUNTS_URL.format(season=2025)
    client = SnapCountClient(_Session({url: _Response("", 404)}))
    assert asyncio.run(client.fetch_season(2025)) == {}
    client = SnapCountClient(_Session({url: _Response("")}))
    with pytest.raises(SnapCountError, match="required columns"):
        asyncio.run(client.fetch_season(2025))


def test_recent_snap_reports_fetches_one_extra_game_without_inflating_totals() -> None:
    baseline = _report()
    current = replace(baseline, game=_ravens_game(event_id="current"), previous=baseline)
    bot = SimpleNamespace(
        config=SimpleNamespace(time_zone=ZoneInfo("UTC")),
        espn=SimpleNamespace(fetch_recent_games=AsyncMock(return_value=[baseline.game, current.game]),
                             fetch_roster=AsyncMock(return_value={})),
        snap_counts=SimpleNamespace(fetch_reports=AsyncMock(return_value=[baseline, current])),
    )
    reports = asyncio.run(_recent_snap_reports(bot, 1))
    assert bot.espn.fetch_recent_games.call_args.args[0] == 2
    assert reports == [current]
    assert aggregate(reports)[0].games == 1
    assert reports[0].previous == baseline


@pytest.mark.parametrize("player", [None, "Lamar"])
def test_command_never_substitutes_previous_game_when_latest_unpublished(player) -> None:
    baseline = _report()
    pending = SnapCountReport(game=_ravens_game(event_id="latest", opponent="PIT"), previous=baseline)
    bot = SimpleNamespace(
        config=SimpleNamespace(time_zone=ZoneInfo("UTC")),
        espn=SimpleNamespace(fetch_recent_games=AsyncMock(return_value=[baseline.game, pending.game]),
                             fetch_roster=AsyncMock(return_value={})),
        snap_counts=SimpleNamespace(fetch_reports=AsyncMock(return_value=[baseline, pending])),
    )
    interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(_snapcounts_command(bot).callback(interaction, player=player, weeks=1))
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "have not been published" in embed.description
    assert not embed.fields


def test_command_explains_a_player_who_disappeared_instead_of_inventing_zero() -> None:
    baseline = _report()
    current = replace(baseline, players=baseline.players[1:], previous=baseline)
    bot = SimpleNamespace(
        config=SimpleNamespace(time_zone=ZoneInfo("UTC")),
        espn=SimpleNamespace(fetch_recent_games=AsyncMock(return_value=[baseline.game, current.game]),
                             fetch_roster=AsyncMock(return_value={})),
        snap_counts=SimpleNamespace(fetch_reports=AsyncMock(return_value=[baseline, current])),
    )
    interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(_snapcounts_command(bot).callback(interaction, player="Lamar", weeks=1))
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "Lamar Jackson is not listed" in embed.description
    assert "absence is not assumed" in embed.description


@pytest.mark.parametrize("weeks", [1, 2])
def test_team_command_sends_separate_units_including_crossover_players(weeks) -> None:
    baseline = _report()
    current = replace(baseline, game=_ravens_game(event_id="current"), previous=baseline)
    bot = SimpleNamespace(
        config=SimpleNamespace(time_zone=ZoneInfo("UTC")),
        espn=SimpleNamespace(
            fetch_recent_games=AsyncMock(return_value=[baseline.game, current.game]),
            fetch_roster=AsyncMock(return_value={}),
        ),
        snap_counts=SimpleNamespace(fetch_reports=AsyncMock(return_value=[baseline, current])),
    )
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    asyncio.run(_snapcounts_command(bot).callback(interaction, weeks=weeks))
    calls = interaction.followup.send.call_args_list
    assert len(calls) == 3
    offense, defense, special = [call.kwargs["embed"] for call in calls]
    for call, unit in zip(calls, (OFFENSE, DEFENSE, SPECIAL_TEAMS)):
        assert call.kwargs["ephemeral"] is True
        embed = call.kwargs["embed"]
        assert unit in embed.title
        assert len(embed) <= MAX_EMBED_CHARS
        assert len(embed.fields) <= MAX_EMBED_FIELDS
        assert all(len(field.value) <= MAX_FIELD_CHARS for field in embed.fields)
    assert "Zay Flowers" in offense.fields[0].value
    assert "Roquan Smith" in defense.fields[0].value
    text = "\n".join(field.value for field in special.fields)
    assert text.index("Nick Moore") < text.index("Zay Flowers") < text.index("Roquan Smith")
    assert f"{3 * weeks} of {25 * weeks} (12%)" in text
    assert f"{2 * weeks} of {25 * weeks} (8%)" in text
    assert "Lamar Jackson" not in text
    assert "ST 0.0 pp" in text
    assert all(field.name.startswith("Special teams") for field in special.fields)


@pytest.mark.parametrize("weeks", [1, 2])
def test_special_teams_report_is_not_crowded_out_by_offense(weeks) -> None:
    original = _report()
    player = original.players[1]
    players = tuple(
        replace(player, pfr_id=f"P{index}", special_teams=0,
                player=PlayerRef(f"Offensive Player Number {index}"))
        for index in range(100)
    ) + (original.players[3],)
    current = replace(original, players=players, previous=original)
    reports = [original, current]
    embed = (
        snap_count_embed(current, SPECIAL_TEAMS) if weeks == 1
        else snap_totals_embed(aggregate(reports), reports, weeks, SPECIAL_TEAMS)
    )
    assert "Nick Moore" in embed.fields[0].value
    assert "Offensive Player Number" not in "\n".join(field.value for field in embed.fields)
    assert "Showing " not in embed.footer.text


@pytest.mark.parametrize("weeks", [1, 2])
def test_empty_special_teams_unit_is_explicit(weeks) -> None:
    report = replace(_report(), players=(_report().players[0],))
    embed = (
        snap_count_embed(report, SPECIAL_TEAMS) if weeks == 1
        else snap_totals_embed(aggregate([report]), [report], weeks, SPECIAL_TEAMS)
    )
    assert "No players have recorded snaps in this unit" in embed.description
    assert not embed.fields


@pytest.mark.parametrize("weeks", [1, 2])
def test_long_unit_reports_paginate_without_dropping_or_repeating_players(weeks) -> None:
    original = _report()
    players = tuple(
        replace(original.players[1], pfr_id=f"P{index}",
                player=PlayerRef(f"Special Teams Contributor {index:03d}", athlete_id=str(index)))
        for index in range(100)
    )
    baseline = replace(original, players=players)
    current = replace(baseline, game=_ravens_game(event_id="current"), previous=baseline)
    reports = [baseline, current]
    pages = (
        snap_count_embeds(current, SPECIAL_TEAMS) if weeks == 1
        else snap_totals_embeds(aggregate(reports), reports, weeks, SPECIAL_TEAMS)
    )
    assert len(pages) > 1
    text = "\n".join(field.value for embed in pages for field in embed.fields)
    for player in players:
        assert text.count(player.name) == 1
    for index, embed in enumerate(pages, 1):
        assert len(embed) <= MAX_EMBED_CHARS
        assert len(embed.fields) <= MAX_EMBED_FIELDS
        assert all(len(field.value) <= MAX_FIELD_CHARS for field in embed.fields)
        assert embed.footer.text.startswith(f"Page {index}/{len(pages)}")

    bot = SimpleNamespace(
        config=SimpleNamespace(time_zone=ZoneInfo("UTC")),
        espn=SimpleNamespace(
            fetch_recent_games=AsyncMock(return_value=[baseline.game, current.game]),
            fetch_roster=AsyncMock(return_value={}),
        ),
        snap_counts=SimpleNamespace(fetch_reports=AsyncMock(return_value=reports)),
    )
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    asyncio.run(_snapcounts_command(bot).callback(interaction, weeks=weeks))
    sent = [
        call.kwargs["embed"] for call in interaction.followup.send.call_args_list
        if "special teams" in call.kwargs["embed"].title
    ]
    assert [embed.to_dict() for embed in sent] == [embed.to_dict() for embed in pages]
