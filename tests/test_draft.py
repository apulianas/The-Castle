from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from ravens_bot import draft as module
from ravens_bot.bot import _draftpicks_command
from ravens_bot.cache import AsyncTtlCache
from ravens_bot.draft import (
    DRAFTTEK_URL, DRAFT_TTL_SECONDS, MAX_PACKAGE_PICKS, RICH_HILL_URL, DraftClient, DraftError,
    DraftPick, DraftSnapshot, parse_drafttek, plan_trade, with_rich_hill,
)
from ravens_bot.embeds import draft_picks_embed, help_embed


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(module, "datetime", Clock)


def source_rows():
    return [
        dict(round=(number - 1) // 32 + 1, pick=number,
             team="BAL" if number in (29, 61, 93, 130, 176, 217, 223, 224) else "NYJ",
             value=225 - number)
        for number in range(1, 225)
    ]


def page(rows=None, *, future=True, year=2027):
    rows = source_rows() if rows is None else rows
    literal = re.sub(r'"(\w+)":', r"\1:", json.dumps(rows))
    text = (
        f"<title>{year} NFL Draft Trade Value Chart</title>"
        '<div class="dt-updated">Most Recent Update: <strong>October 8, 2026 &middot; 5 PM</strong></div>'
        f"<script>window.DT_TRADE_PICKS = {literal};"
    )
    if future:
        text += (
            f'window.DT_FUTURE_PICKS = [{{year:{year+1},round:1,pick:-1,team:"BAL",'
            'value:100.5,future:true,quartile:4,projectedSlot:29},'
            f'{{year:{year+1},round:2,pick:-2,team:"BAL",'
            'value:50,future:true,quartile:4,projectedSlot:29}];'
        )
    return text + "</script>"


def rich_page(count=224):
    return "<table>" + "".join(
        f'<tr><td class="TradeValueData{"B" if number % 2 else "A"}">'
        f'&nbsp;{number}</font>&nbsp;<FONT id="ConsolidatedTradeColor">BUF</font></td>'
        f'<td><FONT id="ConsolidatedTradeValue">{(225-number)/2}</font></td></tr>'
        for number in range(1, count + 1)
    ) + '<tr><td class="TradeValueDataA">&nbsp;</td><td><font id="ConsolidatedTradeValue"></font></td></tr></table>'


def pick(number, value, *, team="BAL", year=2027, round=1):
    return DraftPick(year, round, number, team, Decimal(str(value)), 4 if number is None else None)


def trade_snapshot():
    return DraftSnapshot(
        2027, (pick(1, 3000, team="NYJ"), pick(10, 100, team="NYJ"),
               pick(29, 80), pick(61, 25, round=2), pick(93, 20, round=3),
               pick(130, 5, round=4)),
        (pick(None, 100, year=2028), pick(None, 40, year=2028, round=2)),
        NOW, "October 8, 2026",
    )


def test_parse_inventory_uses_owners_not_original_slot_and_preserves_decimals():
    rows = source_rows()
    rows[28]["team"] = "PHI"
    rows[9]["team"] = "BAL"
    rows[9]["value"] = 12.4
    snapshot = parse_drafttek(page(rows), NOW)
    assert snapshot.year == 2027
    assert 29 not in [p.number for p in snapshot.ravens]
    assert snapshot.ravens[0].number == 10
    assert snapshot.ravens[0].value == Decimal("12.4")
    assert snapshot.updated == "October 8, 2026 \u00b7 5 PM"
    assert snapshot.fetched_at == NOW
    assert snapshot.future_available
    assert snapshot.ravens_future[0].value == Decimal("100.5")
    assert all(p.year == 2028 and p.future for p in snapshot.ravens_future)


@pytest.mark.parametrize("change", [
    lambda rows: rows.pop(),
    lambda rows: rows.append(rows[0]),
    lambda rows: rows[0].update(pick=True),
    lambda rows: rows[0].update(round=8),
    lambda rows: rows[0].update(value=-1),
    lambda rows: rows[0].update(value="NaN"),
    lambda rows: rows[0].update(value="Infinity"),
    lambda rows: rows[0].update(value="wrong"),
    lambda rows: rows[0].update(team=""),
    lambda rows: rows[0].pop("team"),
])
def test_bad_inventory_is_an_error(change):
    rows = source_rows()
    change(rows)
    with pytest.raises(DraftError):
        parse_drafttek(page(rows), NOW)


@pytest.mark.parametrize("text", [
    "<html>Maintenance</html>",
    page().replace("window.DT_TRADE_PICKS", "window.OTHER"),
    page().replace("round: 1", "round: bad", 1),
    page(year=2025),
    page(year=2028),
    page().replace("year:2028", "year:2029"),
    page().replace("quartile:4", "quartile:5"),
    page().replace("round:2,pick:-2", "round:1,pick:-2"),
    page().replace("window.DT_FUTURE_PICKS = [", "window.DT_FUTURE_PICKS = broken["),
], ids=["maintenance", "missing", "malformed", "old", "too-far", "future-year", "quartile", "duplicate", "broken-future"])
def test_bad_schema_year_or_future_is_an_error(text):
    with pytest.raises(DraftError):
        parse_drafttek(text, NOW)


def test_missing_future_is_disclosed_without_inventing_assets():
    snapshot = parse_drafttek(page(future=False), NOW)
    assert not snapshot.future_available
    assert not snapshot.future_picks
    assert "missing or incomplete" in str(draft_picks_embed(snapshot).to_dict())


def test_rich_hill_uses_only_values_and_same_discount_method():
    jj = parse_drafttek(page(), NOW)
    rich = with_rich_hill(jj, rich_page())
    assert rich.chart == "rich_hill"
    assert [(p.number, p.team) for p in rich.picks] == [(p.number, p.team) for p in jj.picks]
    assert rich.ravens[0].value == Decimal(98)
    assert rich.ravens_future[0].value == (rich.picks[59].value + rich.picks[60].value) / 2
    assert rich.ravens_future[1].value == (rich.picks[91].value + rich.picks[92].value) / 2
    assert jj.ravens[0].value == Decimal(196)


@pytest.mark.parametrize("html", [
    rich_page(223),
    rich_page() + rich_page(1),
    rich_page().replace("112.0", "NaN", 1),
    "<html>No values available</html>",
], ids=["missing-pick", "duplicate-pick", "invalid-value", "missing-chart"])
def test_rich_hill_never_silently_falls_back_to_jj(html):
    with pytest.raises(DraftError):
        with_rich_hill(parse_drafttek(page(), NOW), html)


def test_packages_cover_target_with_unique_owned_assets_and_minimum_surplus():
    snapshot = trade_snapshot()
    plan = plan_trade(snapshot, 10)
    assert plan.current[0].value == 100
    assert [p.number for p in plan.current[0].picks] == [29, 93]
    assert plan.current[1].value == 105
    assert len(plan.current[1].picks) == 2
    assert plan.future[0].value == 100
    assert len(plan.future[0].picks) == 1
    for package in plan.current + plan.future:
        assert package.value >= plan.target.value
        assert len(package.picks) <= MAX_PACKAGE_PICKS
        assert len(set(package.picks)) == len(package.picks)
        assert all(p in snapshot.ravens + snapshot.ravens_future for p in package.picks)


def test_decimal_package_matches_exactly():
    snapshot = replace(trade_snapshot(), picks=(
        pick(10, "0.3", team="NYJ"), pick(29, "0.1"), pick(61, "0.2"),
    ), future_picks=())
    assert plan_trade(snapshot, 10).current[0].value == Decimal("0.3")


def test_search_cap_does_not_claim_six_pick_deal_is_available():
    snapshot = replace(
        trade_snapshot(),
        picks=(pick(1, 60, team="NYJ"),) + tuple(pick(number, 10) for number in range(2, 8)),
        future_picks=(),
    )
    plan = plan_trade(snapshot, 1)
    assert not plan.current and not plan.future
    assert plan.strongest.value == 50
    assert len(plan.strongest.picks) == MAX_PACKAGE_PICKS


def test_unreachable_owned_and_unlisted_targets():
    snapshot = trade_snapshot()
    plan = plan_trade(snapshot, 1)
    assert not plan.current and not plan.future
    assert plan.strongest.value == 265
    assert not plan_trade(snapshot, 29).current
    with pytest.raises(ValueError, match="not listed"):
        plan_trade(snapshot, 300)
    assert "Already a Ravens pick" in str(draft_picks_embed(snapshot, plan_trade(snapshot, 29)).to_dict())
    assert "short by 2,735 pts" in str(draft_picks_embed(snapshot, plan).to_dict())


def test_reports_totals_assumptions_sources_and_discord_limits():
    snapshot = trade_snapshot()
    inventory = draft_picks_embed(snapshot)
    total = next(f.value for f in inventory.fields if f.name == "Current-draft total")
    assert total == "**4 picks | 130 pts**"
    assert "unverified" in str(inventory.to_dict())
    assert "Source update: October 8, 2026" in inventory.footer.text
    assert "2026-10-09 00:00 UTC" in inventory.footer.text
    for data in (snapshot, replace(snapshot, chart="rich_hill")):
        for number in (None, 1, 10, 29):
            embed = draft_picks_embed(data, plan_trade(data, number) if number else None)
            assert len(embed) < 6000
            assert len(embed.fields) <= 25
            assert all(len(field.value) <= 1024 for field in embed.fields)
            assert DRAFTTEK_URL in str(embed.to_dict())
            if data.chart == "rich_hill":
                assert RICH_HILL_URL in str(embed.to_dict())
    assert "/draftpicks [pick] [chart]" in [field.name for field in help_embed().fields]


def session_for(*pages):
    session = MagicMock()
    contexts = []
    for text in pages:
        response = MagicMock()
        response.text = AsyncMock(return_value=text)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        contexts.append(context)
    session.get.side_effect = contexts
    return session


def test_client_caches_concurrent_reads_and_reuses_ownership_for_rich_hill():
    async def run():
        session = session_for(page(), rich_page())
        client = DraftClient(session)
        first, second = await asyncio.gather(client.fetch(), client.fetch())
        assert first == second
        rich = await client.fetch("rich_hill")
        assert rich.fetched_at == first.fetched_at
        assert session.get.call_count == 2
        assert session.get.call_args.kwargs["timeout"].total == 30
        with pytest.raises(ValueError):
            await client.fetch("unknown")
    asyncio.run(run())


def test_expiry_refreshes_ownership_across_charts_without_extending_stale_cache():
    async def run():
        updated = source_rows()
        updated[28]["team"] = "PHI"
        session = session_for(page(), rich_page(), page(updated))
        client = DraftClient(session)
        now = [0.0]
        client._pages = AsyncTtlCache(DRAFT_TTL_SECONDS, clock=lambda: now[0])
        await client.fetch()
        now[0] = DRAFT_TTL_SECONDS - 1
        assert 29 in [p.number for p in (await client.fetch("rich_hill")).ravens]
        now[0] = DRAFT_TTL_SECONDS
        assert 29 not in [p.number for p in (await client.fetch("rich_hill")).ravens]
        assert session.get.call_count == 3
    asyncio.run(run())


def test_expired_data_is_not_returned_after_http_failure():
    async def run():
        session = session_for(page())
        session.get.side_effect = [next(session.get.side_effect), aiohttp.ClientError("offline")]
        client = DraftClient(session)
        now = [0.0]
        client._pages = AsyncTtlCache(DRAFT_TTL_SECONDS, clock=lambda: now[0])
        await client.fetch()
        now[0] = DRAFT_TTL_SECONDS
        with pytest.raises(DraftError):
            await client.fetch()
    asyncio.run(run())


@pytest.mark.parametrize("error", [aiohttp.ClientError("offline"), TimeoutError(), UnicodeError()])
def test_client_fetch_failures_are_explicit_and_retried(error):
    async def run():
        session = session_for(page())
        good = session.get.side_effect
        context = next(good)
        session.get.side_effect = [error, context]
        client = DraftClient(session)
        with pytest.raises(DraftError, match="could not be fetched"):
            await client.fetch()
        assert (await client.fetch()).ravens
        assert session.get.call_count == 2
    asyncio.run(run())


def test_invalid_page_does_not_poison_cache():
    async def run():
        session = session_for("broken", page())
        client = DraftClient(session)
        with pytest.raises(DraftError):
            await client.fetch()
        assert (await client.fetch()).ravens
    asyncio.run(run())


def interaction():
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.parametrize("number,chart", [(None, "jj"), (10, "jj"), (10, "rich_hill")])
def test_command_routes_chart_and_optional_pick(number, chart):
    snapshot = replace(trade_snapshot(), chart=chart)
    client = SimpleNamespace(
        fetch=AsyncMock(return_value=snapshot),
        trade=AsyncMock(return_value=plan_trade(snapshot, number) if number else None),
    )
    command = _draftpicks_command(SimpleNamespace(draft_picks=client))
    user = interaction()
    asyncio.run(command.callback(user, number, chart))
    client.fetch.assert_awaited_once_with(chart)
    user.response.defer.assert_awaited_once_with(ephemeral=True)
    assert user.followup.send.call_args.kwargs["embed"].title
    if number:
        client.trade.assert_awaited_once_with(snapshot, number)
    else:
        client.trade.assert_not_awaited()
    tree = discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    options = command.to_dict(tree)["options"]
    assert options[0]["min_value"] == 1 and options[0]["max_value"] == 300
    assert {choice["value"] for choice in options[1]["choices"]} == {"jj", "rich_hill"}


@pytest.mark.parametrize("client", [
    None,
    SimpleNamespace(fetch=AsyncMock(side_effect=DraftError("Source unavailable"))),
])
def test_command_discloses_unavailable_source(client):
    user = interaction()
    asyncio.run(_draftpicks_command(SimpleNamespace(draft_picks=client)).callback(user))
    assert user.followup.send.call_args.kwargs["embed"].title == "Ravens data unavailable"


def test_command_reports_absent_target():
    async def run():
        client = DraftClient(MagicMock())
        client.fetch = AsyncMock(return_value=trade_snapshot())
        user = interaction()
        await _draftpicks_command(SimpleNamespace(draft_picks=client)).callback(user, 300)
        assert "not listed" in user.followup.send.call_args.args[0]
        assert user.followup.send.call_args.kwargs["ephemeral"]
    asyncio.run(run())
