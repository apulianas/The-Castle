from __future__ import annotations

import asyncio
import csv
import gzip
import io
import threading
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from ravens_bot import recap as module
from ravens_bot.bot import _recap_command
from ravens_bot.embeds import recap_embed
from ravens_bot.models import Game, GameTeam, RAVENS, TeamRef
from ravens_bot.recap import (
    REQUIRED_COLUMNS, RecapClient, RecapError, RecapReport, RecapSeason,
    _off_loop, _parse_compressed, find_recap_game, match_game, number,
    parse_pbp, select_completed_game,
)
from ravens_bot.recap_formatting import recap_fields


EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 16, 18, tzinfo=timezone.utc)


def game(**kwargs):
    return replace(Game(
        event_id="1", name="Cleveland Browns at Baltimore Ravens",
        short_name="CLE @ BAL", start_time=datetime(2026, 9, 13, 17, tzinfo=timezone.utc),
        status="Final", home=GameTeam(RAVENS, score=24, is_home=True),
        away=GameTeam(TeamRef("Cleveland Browns", "5", "CLE"), score=17),
        state="post", completed=True, week="Week 1",
        season=2026, season_type=2, week_number=1,
    ), **kwargs)


def row(**kwargs):
    result = dict.fromkeys(REQUIRED_COLUMNS, "0")
    result.update(
        game_id="2026_01_CLE_BAL", season="2026", season_type="REG", week="1",
        home_team="BAL", away_team="CLE", play_id="1", posteam="BAL",
        play_type="pass", play_type_nfl="PASS", desc="A pass",
        qtr="1", time="10:00", qb_dropback="1", epa="0.5", wpa="0.1",
        pass_attempt="1", complete_pass="1", passing_yards="10", yards_gained="10",
        passer_player_id="qb", passer_player_name="L.Jackson",
        rusher_player_id="rb", rusher_player_name="D.Henry",
        total_home_score="24", total_away_score="17",
    )
    result.update(kwargs)
    return result


def end(**kwargs):
    return row(play_type="", play_type_nfl="END_GAME", desc="END GAME", **kwargs)


def csv_text(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=sorted(REQUIRED_COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def parse(rows):
    return parse_pbp(io.StringIO(csv_text(rows)), 2026)


def report(rows):
    games = parse(rows)
    return RecapReport(game(), match_game(games, game()), RecapSeason(games, NOW))


def test_efficiency_filters_sacks_scrambles_runs_and_success_denominator():
    data = report([
        row(epa="1"),
        row(sack="1", complete_pass="0", passing_yards="", yards_gained="-5", epa="-1"),
        row(play_type="run", rush_attempt="1", pass_attempt="0", rushing_yards="8", epa="0"),
        row(play_type="run", qb_dropback="0", rush_attempt="1", pass_attempt="0", rushing_yards="5", epa="2"),
        row(play_type="no_play", epa="50"),
        row(play_type="qb_spike", qb_spike="1", complete_pass="0", epa="-8"),
        row(play_type="qb_kneel", qb_kneel="1", qb_dropback="0", rush_attempt="1",
            pass_attempt="0", rushing_yards="-1", epa="-8"),
        row(two_point_attempt="1", epa="50"),
        end(),
    ]).data
    assert data.offense.plays == data.offense.measured == 4
    assert data.offense.epa == 2
    assert data.offense.successes == 2
    assert data.dropbacks.plays == 3
    assert data.designed_rushes.plays == 1
    assert data.passing.attempts == 2  # completion + spike, not sack
    assert data.passing.completions == 1
    assert data.passing.yards == 10
    assert data.sack_yards == -5
    assert data.sacks == 1
    assert data.rushing.attempts == 3  # scramble + designed run + kneel
    assert data.rushing.yards == 12
    assert data.partial_reasons(game()) == []


@pytest.mark.parametrize(("possession", "wpa", "expected"), [
    ("BAL", "-0.45", -0.45), ("CLE", "-0.45", 0.45),
    ("BAL", "0.2", 0.2), ("CLE", "0.2", -0.2),
])
def test_turnover_wpa_uses_preplay_possession(possession, wpa, expected):
    data = report([row(posteam=possession, wpa=wpa, interception="1", complete_pass="0"), end()]).data
    assert data.swings[0].wpa == expected


def test_biggest_swings_include_defense_and_penalties_and_are_bounded():
    data = report([
        row(wpa="0.1"), row(wpa="-0.2"), row(posteam="CLE", wpa="-0.7"),
        row(wpa="0.4", play_type="no_play"), row(wpa="inf"), end(),
    ]).data
    assert [swing.wpa for swing in data.swings] == [0.7, 0.4, -0.2]
    assert data.wpa_missing == 1


@pytest.mark.parametrize("value", ["", "NA", "NaN", "Inf", "-inf", None, "bad"])
def test_missing_and_nonfinite_numbers(value):
    assert number(value) is None


def test_missing_measurements_are_partial_not_zero():
    result = report([row(epa="NaN", passing_yards="", wpa="Infinity"), end()])
    data = result.data
    assert data.offense.plays == 1
    assert data.offense.measured == 0
    assert not data.passing.complete
    fields = dict(recap_fields(result))
    assert "Partial recap" in fields
    assert "unavailable" in fields["Ravens offensive efficiency"]
    assert "Passing: unavailable" in fields["Ravens production"]
    assert "0 gross pass yds" not in str(fields)


def test_null_flags_and_player_names_are_partial():
    data = report([row(qb_dropback=""), row(passer_player_name="", passer_player_id=""), end()]).data
    assert data.flags_missing
    assert not data.leaders_complete
    assert not data.passing.complete


def test_null_yards_on_incomplete_pass_are_genuine_zero():
    data = report([row(complete_pass="0", passing_yards=""), end()]).data
    assert data.passing.complete
    assert data.passing.yards == 0
    assert data.passing.attempts == 1


def test_lateral_rush_credits_yards_and_td_without_extra_carry():
    data = report([
        row(play_type="run", qb_dropback="0", rush_attempt="1", pass_attempt="0",
            rushing_yards="5", lateral_rush="1", lateral_rushing_yards="10",
            lateral_rusher_player_id="wr", lateral_rusher_player_name="Z.Flowers",
            rush_touchdown="1", td_player_id="wr"),
        end(),
    ]).data
    assert data.rushing.yards == 15 and data.rushing.attempts == 1
    assert data.rushing.touchdowns == 1
    assert data.rushers["rb"][1].touchdowns == 0
    assert data.rushers["wr"][1].yards == 10
    assert data.rushers["wr"][1].attempts == 0
    assert data.rushers["wr"][1].touchdowns == 1


def test_unmodeled_kneel_wpa_does_not_mark_finished_recap_partial():
    data = report([
        row(),
        row(play_type="qb_kneel", qb_kneel="1", rush_attempt="1", qb_dropback="0",
            pass_attempt="0", rushing_yards="-1", wpa=""),
        end(),
    ]).data
    assert data.wpa_missing == 0
    assert data.partial_reasons(game()) == []


def test_end_record_and_score_agreement_required():
    assert "Partial recap" in dict(recap_fields(report([row()])))
    assert "Partial recap" in dict(recap_fields(report([row(), end(total_home_score="21")])))
    assert "Partial recap" not in dict(recap_fields(report([row(), end()])))


def test_only_ravens_regular_and_postseason_are_retained():
    games = parse([
        row(), row(home_team="IND", away_team="JAX"),
        row(season_type="PRE"), end(),
    ])
    assert list(games) == ["2026_01_CLE_BAL"]
    assert games["2026_01_CLE_BAL"].offense.plays == 1


@pytest.mark.parametrize("changed", [
    {"season": "2025"}, {"week": "nan"}, {"week": "1.5"},
    {"game_id": "2026_02_CLE_BAL"},
])
def test_invalid_metadata_is_error(changed):
    with pytest.raises(RecapError):
        parse([row(**changed)])


def test_schema_and_corrupt_csv_errors_are_not_unpublished():
    with pytest.raises(RecapError, match="schema"):
        parse_pbp(io.StringIO("team,week\nBAL,1\n"), 2026)
    with pytest.raises(RecapError, match="malformed"):
        parse_pbp(io.StringIO(csv_text([row()]) + "one,short,row\n"), 2026)
    with pytest.raises(RecapError, match="corrupt"):
        _parse_compressed(io.BytesIO(b"not gzip"), 2026)


def test_match_requires_season_week_opponent_and_host():
    games = parse([row(), end()])
    assert match_game(games, game()) is not None
    assert match_game(games, game(week_number=2)) is None
    assert match_game(games, game(season=2025)) is None
    assert match_game(games, game(home=game().away, away=game().home)) is None
    assert match_game(games, game(away=GameTeam(TeamRef("Steelers", "23", "PIT")))) is None
    with pytest.raises(RecapError, match="metadata"):
        match_game(games, game(week_number=None))


@pytest.mark.parametrize(("historical", "modern"), [("OAK", "LV"), ("SD", "LAC"), ("STL", "LA")])
def test_historical_game_ids_match_normalized_franchise_columns(historical, modern):
    games = parse([row(game_id=f"2026_01_{historical}_BAL", away_team=modern)])
    matched = match_game(games, game(away=GameTeam(TeamRef(historical, "99", historical))))
    assert matched is not None
    assert matched.game_id == f"2026_01_{historical}_BAL"


@pytest.mark.parametrize(("season", "espn_week", "pbp_week"), [
    (2026, 1, 19), (2020, 1, 18), (2012, 5, 21), (2024, 5, 22),
])
def test_postseason_week_number_translation(season, espn_week, pbp_week):
    values = [
        row(season=str(season), season_type="POST", week=str(pbp_week),
            game_id=f"{season}_{pbp_week}_CLE_BAL")
    ]
    games = parse_pbp(io.StringIO(csv_text(values)), season)
    assert match_game(games, game(season=season, season_type=3, week_number=espn_week)) is not None
    assert match_game(games, game(season=season, season_type=2, week_number=pbp_week)) is None


def test_selection_is_latest_completed_regular_or_postseason_and_local_date():
    latest = game(start_time=datetime(2026, 9, 21, 0, 20, tzinfo=timezone.utc))
    assert select_completed_game([
        game(), latest, game(completed=False, start_time=NOW),
        game(season_type=1, start_time=NOW),
    ], EASTERN) == latest
    assert select_completed_game([latest], EASTERN, date(2026, 9, 20)) == latest
    assert select_completed_game([latest], EASTERN, date(2026, 9, 21)) is None
    assert select_completed_game([], EASTERN) is None


def test_find_game_uses_prior_season_not_latest_preseason():
    espn = SimpleNamespace(fetch_season_schedule=AsyncMock(side_effect=[
        [game(season_type=1)], [], [game(season=2025)], [],
    ]))
    found = asyncio.run(find_recap_game(espn, date(2026, 8, 20), EASTERN))
    assert found.season == 2025
    assert espn.fetch_season_schedule.call_args.args == (2025,)


def test_find_dated_january_game_uses_previous_season():
    dated = game(start_time=datetime(2027, 1, 10, 18, tzinfo=timezone.utc))
    espn = SimpleNamespace(fetch_season_schedule=AsyncMock(return_value=[dated]))
    assert asyncio.run(find_recap_game(espn, date(2027, 2, 1), EASTERN, date(2027, 1, 10))) == dated
    assert espn.fetch_season_schedule.await_count == 2
    espn.fetch_season_schedule.assert_any_await(2026, season_type=2)
    espn.fetch_season_schedule.assert_any_await(2026, season_type=3)


def test_latest_game_includes_explicit_postseason_schedule():
    playoff = game(event_id="post", season_type=3, week_number=1,
                   start_time=datetime(2027, 1, 10, 18, tzinfo=timezone.utc))
    espn = SimpleNamespace(fetch_season_schedule=AsyncMock(side_effect=[[game()], [playoff]]))
    assert asyncio.run(find_recap_game(espn, date(2027, 1, 11), EASTERN)) == playoff


class Response:
    def __init__(self, payload=b"", status=200):
        self.payload = payload
        self.status = status
        self.content_length = len(payload)
        self.content = self
        self.headers = {"Last-Modified": "Wed, 16 Sep 2026 09:00:00 GMT"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for index in range(0, len(self.payload), 7):
            await asyncio.sleep(0)
            yield self.payload[index:index + 7]


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response


def test_cache_collapses_concurrent_streams_and_refreshes(monkeypatch):
    payload = gzip.compress(csv_text([row(), end()]).encode())
    session = Session(Response(payload))
    client = RecapClient(session)
    clock = [0]
    client.cache._clock = lambda: clock[0]
    original = module._parse_compressed
    threads = []

    def parse_thread(*args):
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(module, "_parse_compressed", parse_thread)

    async def run():
        reports = await asyncio.gather(*(client.fetch(game()) for _ in range(5)))
        assert all(result.data.ended for result in reports)
        assert session.calls == 1
        assert threads[0] != threading.get_ident()
        clock[0] = module.RECAP_TTL_SECONDS + 1
        await client.fetch(game())
        assert session.calls == 2

    asyncio.run(run())


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_failure_is_error_and_not_cached(status):
    session = Session(Response(status=status))
    client = RecapClient(session)
    async def run():
        for _ in range(2):
            with pytest.raises(RecapError, match=f"HTTP {status}"):
                await client.fetch(game())
        assert session.calls == 2
    asyncio.run(run())


def test_404_and_unpublished_game_are_distinct_from_errors_and_cached():
    for response in (Response(status=404), Response(gzip.compress(csv_text([]).encode()))):
        client = RecapClient(Session(response))
        async def run():
            result = await client.fetch(game())
            assert result.data is None
            assert "Not published" in dict(recap_fields(result))
            await client.fetch(game())
            assert client.session.calls == 1
        asyncio.run(run())


def test_download_and_decompression_limits(monkeypatch):
    payload = gzip.compress(csv_text([row()]).encode())
    monkeypatch.setattr(module, "MAX_COMPRESSED_BYTES", len(payload) - 1)
    with pytest.raises(RecapError, match="download size"):
        asyncio.run(RecapClient(Session(Response(payload))).fetch(game()))
    response = Response(payload)
    response.content_length = None
    with pytest.raises(RecapError, match="download size"):
        asyncio.run(RecapClient(Session(response)).fetch(game()))
    monkeypatch.setattr(module, "MAX_DECOMPRESSED_BYTES", 20)
    with pytest.raises(RecapError, match="decompressed size"):
        _parse_compressed(io.BytesIO(payload), 2026)


def test_off_loop_cancellation_waits_for_worker_before_cleanup():
    entered = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    def work():
        entered.set()
        release.wait(timeout=5)
        finished.set()

    async def run():
        task = asyncio.create_task(_off_loop(work))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(run())


def test_tempfile_closed_on_parse_error(monkeypatch):
    original = module.tempfile.TemporaryFile
    files = []
    def temp(*args, **kwargs):
        result = original(*args, **kwargs)
        files.append(result)
        return result
    monkeypatch.setattr(module.tempfile, "TemporaryFile", temp)
    with pytest.raises(RecapError):
        asyncio.run(RecapClient(Session(Response(b"corrupt"))).fetch(game()))
    assert len(files) == 1 and files[0].closed


def test_tempfile_closed_when_download_is_cancelled(monkeypatch):
    original = module.tempfile.TemporaryFile
    files = []

    def temp(*args, **kwargs):
        result = original(*args, **kwargs)
        files.append(result)
        return result

    monkeypatch.setattr(module.tempfile, "TemporaryFile", temp)

    async def run():
        started = asyncio.Event()

        class SlowResponse(Response):
            async def iter_chunked(self, size):
                yield b"partial gzip"
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(RecapClient(Session(SlowResponse())).fetch(game()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(files) == 1 and files[0].closed

    asyncio.run(run())


def test_discord_output_limits_attribution_score_and_freshness():
    result = report([
        row(desc="X" * 20000, passer_player_name="Z" * 3000),
        row(desc="Y" * 20000, wpa="-0.3"),
        row(desc="Z" * 20000, posteam="CLE", wpa="-0.8"), end(),
    ])
    embed = recap_embed(result, EASTERN)
    assert "24" in embed.title and "17" in embed.title
    assert len(embed) <= 6000
    assert len(embed.title) <= 256 and len(embed.description) <= 4096
    assert len(embed.fields) <= 25
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert "ESPN" in embed.footer.text and "NFLverse/nflfastR" in embed.footer.text
    assert "2026-09-16" in embed.footer.text
    assert "+80.0 percentage points" in str(embed.to_dict())


def interaction():
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.parametrize("mode", ["none", "unpublished", "error", "published"])
def test_command_outcomes(mode):
    selected = [] if mode == "none" else [game()]
    client = SimpleNamespace(fetch=AsyncMock())
    client.fetch.return_value = report([row(), end()]) if mode == "published" else RecapReport(game(), None, RecapSeason({}, NOW))
    if mode == "error":
        client.fetch.side_effect = RecapError("HTTP failure")
    bot = SimpleNamespace(
        config=SimpleNamespace(time_zone=EASTERN),
        espn=SimpleNamespace(fetch_season_schedule=AsyncMock(return_value=selected)),
        recaps=client,
    )
    user = interaction()
    asyncio.run(_recap_command(bot).callback(user, None))
    text = str(user.followup.send.call_args.kwargs["embed"].to_dict())
    assert {"none": "No completed", "unpublished": "Not published", "error": "HTTP failure", "published": "Ravens offensive efficiency"}[mode] in text


def test_command_invalid_date_does_not_fetch():
    bot = SimpleNamespace(config=SimpleNamespace(time_zone=EASTERN))
    user = interaction()
    asyncio.run(_recap_command(bot).callback(user, "not-a-date"))
    user.response.send_message.assert_awaited_once()
    user.response.defer.assert_not_awaited()
