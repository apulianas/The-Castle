from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from ravens_bot.bluesky import BlueskyPost, merge_roster_moves, parse_game_injury, parse_roster_move
from ravens_bot.bot import RavensBot, _AnnouncementTarget, game_injury_key, transaction_announcement_key
from ravens_bot.config import BotConfig
from ravens_bot.espn import EspnApiError, parse_transactions
from ravens_bot.models import Game, InjuryReport, InjuryUpdate, PlayerRef
from ravens_bot.state import channel_key


DAY = date(2026, 10, 7)
ZONE = ZoneInfo("America/New_York")


def move(text="Signed WR Chris Moore.", *, source="espn", key=None):
    if source == "bluesky":
        return parse_roster_move(BlueskyPost(
            uri=f"at://did:plc:x/app.bsky.feed.post/{key or 'first'}",
            text="We have " + text[0].lower() + text[1:],
            created_at=datetime(2026, 10, 7, 18, tzinfo=timezone.utc),
            image_urls=(),
        ), ZONE)
    transaction = parse_transactions({"items": [{"description": text}]}, DAY)[0]
    return replace(transaction, transaction_id=key) if key else transaction


def setup_bot(tmp_path, webhook):
    bot = RavensBot(BotConfig(
        discord_token="token", discord_channel_ids=(123,), discord_webhook_urls=(),
        poll_interval_seconds=300, time_zone=ZONE, state_file=str(tmp_path / "state.json"),
    ))
    destination = MagicMock(spec=discord.Webhook if webhook else discord.abc.Messageable)
    destination.send = AsyncMock(return_value=SimpleNamespace(id=987))
    edit = AsyncMock()
    destination.edit_message = edit
    bot.get_partial_messageable = MagicMock(return_value=SimpleNamespace(
        get_partial_message=MagicMock(return_value=SimpleNamespace(edit=edit)),
    ))
    target = _AnnouncementTarget("webhook:123" if webhook else "123", "test target", destination)
    return bot, target, edit


def poll(bot, target, transactions, updates=()):
    asyncio.run(bot._post_new_roster_news([target], transactions, InjuryReport(updates), DAY))


@pytest.mark.parametrize("webhook", [False, True])
def test_richer_ordinary_move_edits_after_restart(tmp_path, webhook):
    bot, target, edit = setup_bot(tmp_path, webhook)
    first = move(source="bluesky")
    richer = move("Signed WR Chris Moore to the practice squad.")
    poll(bot, target, [first])
    assert target.destination.send.call_args.kwargs.get("wait") == (True if webhook else None)
    bot.announcement_state.load()
    poll(bot, target, [richer])
    poll(bot, target, [first, richer])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 1
    assert "practice squad" in str(edit.call_args.kwargs["embeds"][0].to_dict()).lower()
    assert edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    if webhook:
        assert edit.call_args.args == (987,)
    else:
        bot.get_partial_messageable.return_value.get_partial_message.assert_called_with(987)


@pytest.mark.parametrize("webhook", [False, True])
def test_same_id_corrections_can_return_to_an_earlier_version(tmp_path, webhook):
    bot, target, edit = setup_bot(tmp_path, webhook)
    first = move("Signed WR Chris Moore to a one-year contract.", key="stable")
    corrected = move("Signed WR Chris Moore to a two-year contract.", key="stable")
    for item in (first, corrected, first, first):
        poll(bot, target, [item])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 2
    assert "one-year" in str(edit.call_args.kwargs["embeds"][0].to_dict())


@pytest.mark.parametrize("trade", [False, True])
@pytest.mark.parametrize("webhook", [False, True])
def test_late_injury_context_edits_even_when_transaction_is_unchanged(tmp_path, webhook, trade):
    bot, target, edit = setup_bot(tmp_path, webhook)
    transaction = move("Acquired WR Chris Moore from Carolina." if trade else "Signed WR Chris Moore.")
    injury = InjuryUpdate(PlayerRef("Chris Moore"), "Questionable", "Knee")
    poll(bot, target, [transaction])
    poll(bot, target, [transaction], [injury])
    poll(bot, target, [transaction], [replace(injury, status="Out")])
    poll(bot, target, [transaction], [replace(injury, status="Out")])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 2
    assert any("Out" in field.name for field in edit.call_args.kwargs["embeds"][0].fields)


@pytest.mark.parametrize("webhook", [False, True])
@pytest.mark.parametrize("status,code", [(403, 50013), (404, 10008)])
def test_failed_roster_edits_retry_without_duplicate(tmp_path, webhook, status, code, caplog):
    bot, target, edit = setup_bot(tmp_path, webhook)
    poll(bot, target, [move()])
    error = discord.Forbidden if status == 403 else discord.NotFound
    edit.side_effect = error(
        SimpleNamespace(status=status, reason="unavailable"),
        {"code": code, "message": "unavailable"},
    )
    richer = move("Signed WR Chris Moore to the practice squad.")
    poll(bot, target, [richer])
    assert "Could not post or edit roster" in caplog.text
    edit.side_effect = None
    poll(bot, target, [richer])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 2


@pytest.mark.parametrize("first,second", [
    ("Released WR Chris Moore.", "Signed WR Chris Moore to the practice squad."),
    ("Signed WR Chris Moore to the practice squad.", "Signed WR Chris Moore to the active roster."),
    ("Placed WR Chris Moore on injured reserve.", "Designated WR Chris Moore to return."),
])
def test_cross_source_separate_actions_are_not_hidden(tmp_path, first, second):
    bot, target, edit = setup_bot(tmp_path, False)
    early, later = move(first, source="bluesky"), move(second)
    assert merge_roster_moves([later], [early]) == [later, early]
    poll(bot, target, [early])
    poll(bot, target, [later])
    assert target.destination.send.await_count == 2
    assert edit.await_count == 0


def test_same_source_rewording_edits_instead_of_reposting(tmp_path):
    bot, target, edit = setup_bot(tmp_path, False)
    poll(bot, target, [move()])
    poll(bot, target, [move("Signed WR Chris Moore to the practice squad.")])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 1


def test_compound_roster_report_is_not_replaced_by_a_fragment(tmp_path):
    bot, target, edit = setup_bot(tmp_path, False)
    compound = move("Placed WR Chris Moore and C Ethan Pocic on injured reserve.")
    fragment = move("Placed C Ethan Pocic on injured reserve.", source="bluesky")
    poll(bot, target, [compound])
    poll(bot, target, [fragment])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 0


def test_legacy_roster_post_is_not_reposted_even_with_other_saved_posts(tmp_path):
    bot, target, edit = setup_bot(tmp_path, False)
    legacy = move()
    bot.announcement_state.mark(channel_key(transaction_announcement_key(legacy), target.key_id))
    poll(bot, target, [move("Released C Ethan Pocic.")])
    poll(bot, target, [legacy])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 0


def test_legacy_cross_source_dedup_survives_new_saved_posts(tmp_path):
    bot, target, edit = setup_bot(tmp_path, False)
    bot.announcement_state.mark(channel_key(
        f"moved-player:bluesky:{DAY}:chris moore", target.key_id,
    ))
    poll(bot, target, [move("Released C Ethan Pocic.")])
    poll(bot, target, [move()])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 0


def test_targets_retry_independently(tmp_path):
    bot, channel, channel_edit = setup_bot(tmp_path, False)
    _, webhook, webhook_edit = setup_bot(tmp_path, True)
    channel.destination.send.side_effect = discord.DiscordException("offline")

    def both(transaction):
        asyncio.run(bot._post_new_roster_news(
            [channel, webhook], [transaction], InjuryReport(()), DAY,
        ))

    both(move())
    channel.destination.send.side_effect = None
    both(move())
    both(move("Signed WR Chris Moore to the practice squad."))
    assert channel.destination.send.await_count == 2
    assert webhook.destination.send.await_count == 1
    assert channel_edit.await_count == webhook_edit.await_count == 1


def game_update(text, key="first"):
    return parse_game_injury(BlueskyPost(
        uri=f"at://did:plc:x/app.bsky.feed.post/{key}",
        text=text, created_at=datetime(2026, 10, 7, 18, tzinfo=timezone.utc), image_urls=(),
    ))


def game_poll(bot, target, updates):
    bot.bluesky = SimpleNamespace(fetch_game_injuries=AsyncMock(return_value=updates))
    bot._announcement_targets = AsyncMock(return_value=[target])
    bot._graphic_name_candidates = AsyncMock(return_value=[])
    asyncio.run(bot._post_game_injuries(Game(
        event_id="game", name="Ravens game", short_name="BAL",
        start_time=datetime(2026, 10, 7, 17, tzinfo=timezone.utc), status="In progress",
    )))


@pytest.mark.parametrize("webhook", [False, True])
def test_game_status_posts_stay_separate_but_source_corrections_edit(tmp_path, webhook):
    bot, target, edit = setup_bot(tmp_path, webhook)
    initial = game_update("WR Chris Moore (knee) is questionable to return.")
    corrected = game_update("WR Chris Moore (ankle) is questionable to return.")
    later = game_update("WR Chris Moore (ankle) has been ruled out.", "second")
    game_poll(bot, target, [initial])
    bot.announcement_state.load()
    game_poll(bot, target, [corrected, later])
    game_poll(bot, target, [corrected, later])
    assert target.destination.send.await_count == 2
    assert edit.await_count == 1
    assert "Ankle" in edit.call_args.kwargs["embeds"][0].title
    assert "Out" in target.destination.send.call_args.kwargs["embeds"][0].title


@pytest.mark.parametrize("webhook", [False, True])
def test_game_source_correction_retries_failed_edits(tmp_path, webhook, caplog):
    bot, target, edit = setup_bot(tmp_path, webhook)
    initial = game_update("WR Chris Moore (knee) is questionable to return.")
    corrected = game_update("WR Chris Moore (ankle) is questionable to return.")
    game_poll(bot, target, [initial])
    edit.side_effect = discord.DiscordException("offline")
    game_poll(bot, target, [corrected])
    assert "Could not post or edit in-game injury" in caplog.text
    edit.side_effect = None
    game_poll(bot, target, [corrected])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 2


def test_legacy_in_game_post_is_not_duplicated(tmp_path):
    bot, target, edit = setup_bot(tmp_path, False)
    update = game_update("WR Chris Moore (knee) is questionable to return.")
    bot.announcement_state.mark(channel_key(game_injury_key(update), target.key_id))
    game_poll(bot, target, [update])
    assert target.destination.send.await_count == edit.await_count == 0


@pytest.mark.parametrize("webhook", [False, True])
def test_chart_render_failure_does_not_kill_polling_and_retries(tmp_path, webhook, monkeypatch, caplog):
    bot, target, _ = setup_bot(tmp_path, webhook)
    report = SimpleNamespace(announcement_key="report-v1")
    bot._render_official_injury_report = AsyncMock(side_effect=OSError("artwork unavailable"))
    asyncio.run(bot._post_official_injury_report([target], report, DAY))
    assert "Official injury chart could not be drawn; will retry" in caplog.text
    assert target.destination.send.await_count == 0
    bot._render_official_injury_report.side_effect = None
    bot._render_official_injury_report.return_value = b"image"
    monkeypatch.setattr("ravens_bot.bot.official_injury_embed", lambda _: discord.Embed(title="Report"))
    asyncio.run(bot._post_official_injury_report([target], report, DAY))
    assert target.destination.send.await_count == 1
    assert bot.announcement_state.message_id(channel_key(f"official-injury:{DAY}", target.key_id)) == 987


@pytest.mark.parametrize("failed_feed", ["transactions", "injuries"])
def test_an_espn_feed_failure_does_not_block_other_roster_sources(tmp_path, failed_feed, caplog):
    bot, target, _ = setup_bot(tmp_path, False)
    bot._announcement_targets = AsyncMock(return_value=[target])
    bot.injury_reports = SimpleNamespace(fetch=AsyncMock(return_value=None))
    bot.espn = SimpleNamespace(
        fetch_transactions=AsyncMock(return_value=[move()]),
        fetch_injuries=AsyncMock(return_value=InjuryReport(())),
    )
    getattr(bot.espn, f"fetch_{failed_feed}").side_effect = EspnApiError("unavailable")
    elevation = move("Activated G Kyle Hergel from the practice squad (standard elevation).")
    bot.official_transactions = SimpleNamespace(fetch_standard_elevations=AsyncMock(return_value=[elevation]))
    bot._post_new_roster_news = AsyncMock()
    asyncio.run(bot.poll_updates())
    delivered = bot._post_new_roster_news.call_args.args[1]
    assert elevation in delivered
    if failed_feed == "injuries":
        assert move() in delivered
    assert "skipped" in caplog.text or "context unavailable" in caplog.text
