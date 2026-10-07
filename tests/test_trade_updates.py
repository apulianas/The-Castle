from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from ravens_bot.bluesky import BlueskyPost, merge_roster_moves, parse_roster_move
from ravens_bot.bot import RavensBot, _AnnouncementTarget, transaction_announcement_key
from ravens_bot.config import BotConfig
from ravens_bot.espn import parse_transactions
from ravens_bot.models import InjuryReport, InjuryUpdate, PlayerRef, RosterNews
from ravens_bot.state import channel_key
from ravens_bot.trade_news import (
    announcement_trade, decode_news, encode_news, same_trade, trade_version,
)


DAY = date(2026, 10, 7)
ZONE = ZoneInfo("America/New_York")
EARLY = "We have acquired WR Diontae Johnson from the Carolina Panthers."
COMPLETE = (
    "Acquired WR Diontae Johnson and a 2027 sixth-round pick from Carolina "
    "in exchange for WR Chris Moore and a 2027 fifth-round pick."
)


def club(text=EARLY, key="first"):
    return parse_roster_move(
        BlueskyPost(
            uri=f"at://did:plc:x/app.bsky.feed.post/{key}",
            text=text, created_at=datetime(2026, 10, 7, 18, tzinfo=timezone.utc),
            image_urls=(),
        ),
        ZONE,
    )


def espn(text=COMPLETE):
    return parse_transactions({"items": [{"description": text}]}, DAY)[0]


def setup_bot(tmp_path, webhook=False):
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


def poll(bot, targets, moves, updates=()):
    asyncio.run(bot._post_new_roster_news(targets, moves, InjuryReport(updates), DAY))


@pytest.mark.parametrize("webhook", [False, True])
def test_richer_trade_edits_original_after_restart_and_does_not_regress(tmp_path, webhook):
    bot, target, edit = setup_bot(tmp_path, webhook)
    first, richer = club(), espn()
    poll(bot, [target], [first])
    assert target.destination.send.call_args.kwargs.get("wait") is (True if webhook else None)
    bot.announcement_state.load()
    poll(bot, [target], [richer])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 1
    fields = {field.name: field.value for field in edit.call_args.kwargs["embeds"][0].fields}
    assert "Diontae Johnson" in fields["Ravens receive"]
    assert "sixth-round" in fields["Ravens receive"]
    assert "Chris Moore" in fields["Ravens send"]
    assert edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    if webhook:
        assert edit.call_args.args == (987,)
    else:
        bot.get_partial_messageable.return_value.get_partial_message.assert_called_with(987)
    poll(bot, [target], [first, richer, club(key="repeated")])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 1


def test_a_richer_club_report_can_upgrade_espn(tmp_path):
    bot, target, edit = setup_bot(tmp_path)
    poll(bot, [target], [espn("Acquired WR Diontae Johnson from Carolina.")])
    poll(bot, [target], [club("We have " + COMPLETE[0].lower() + COMPLETE[1:])])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 1
    assert "Bluesky" in edit.call_args.kwargs["embeds"][0].footer.text


def test_same_poll_and_same_source_trade_duplicates_are_one_message(tmp_path):
    bot, target, edit = setup_bot(tmp_path)
    first, richer = club(), espn()
    merged = merge_roster_moves([richer], [first, club(key="repeat")])
    assert merged == [richer]
    poll(bot, [target], merged)
    poll(bot, [target], [replace(richer, transaction_id="different-feed-id")])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 0


@pytest.mark.parametrize("webhook", [False, True])
def test_failed_edits_retry_without_sending_another_message(tmp_path, webhook, caplog):
    bot, target, edit = setup_bot(tmp_path, webhook)
    poll(bot, [target], [club()])
    edit.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": "no access"}
    )
    richer = espn()
    poll(bot, [target], [richer])
    assert bot._unseen(target, trade_version(richer))
    assert "Could not post or edit trade" in caplog.text
    edit.side_effect = None
    poll(bot, [target], [richer])
    assert edit.await_count == 2
    assert target.destination.send.await_count == 1
    assert not bot._unseen(target, trade_version(richer))


def test_a_deleted_trade_post_is_not_replaced_with_a_notification(tmp_path, caplog):
    bot, target, edit = setup_bot(tmp_path)
    poll(bot, [target], [club()])
    edit.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), {"code": 10008, "message": "deleted"}
    )
    poll(bot, [target], [espn()])
    assert target.destination.send.await_count == 1
    assert "Could not post or edit trade" in caplog.text


def test_failed_first_send_can_retry_and_targets_have_independent_posts(tmp_path):
    bot, first, edit = setup_bot(tmp_path)
    _, second, _ = setup_bot(tmp_path, webhook=True)
    first.destination.send.side_effect = discord.DiscordException("unavailable")
    poll(bot, [first, second], [club()])
    assert bot._unseen(first, trade_version(club()))
    first.destination.send.side_effect = None
    poll(bot, [first, second], [club()])
    poll(bot, [first, second], [espn()])
    assert first.destination.send.await_count == 2
    assert second.destination.send.await_count == 1
    assert edit.await_count == 1
    assert second.destination.edit_message.await_count == 1


def test_same_id_corrections_are_edited(tmp_path):
    bot, target, edit = setup_bot(tmp_path)
    original = espn()
    correction = replace(original, description=COMPLETE.replace("fifth-round", "fourth-round"))
    poll(bot, [target], [original])
    poll(bot, [target], [correction])
    assert edit.await_count == 1
    assert "fourth-round" in edit.call_args.kwargs["embeds"][0].fields[1].value


@pytest.mark.parametrize("text", [
    "We have agreed in principle to a trade with the Carolina Panthers for Diontae Johnson, pending a physical.",
    "We have agreed to acquire WR Diontae Johnson from Carolina.",
    "We have received WR Diontae Johnson in a trade with Carolina.",
])
def test_agreements_and_positionless_players_match_completed_deals(tmp_path, text):
    first, completed = club(text), espn()
    assert announcement_trade(first) is not None
    assert same_trade(first, completed)
    bot, target, edit = setup_bot(tmp_path)
    poll(bot, [target], [first])
    poll(bot, [target], [completed])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 1


def test_pick_only_deals_match_without_collapsing_other_deals(tmp_path):
    first = club("We have received a 2027 sixth-round draft pick in a trade with Carolina.")
    completed = espn("Acquired a 2027 6th-round pick from Carolina for a 2027 seventh-round pick.")
    assert same_trade(first, completed)
    assert merge_roster_moves([completed], [first]) == [completed]
    assert not same_trade(first, espn("Acquired a 2028 sixth-round pick from Carolina."))
    bot, target, edit = setup_bot(tmp_path)
    poll(bot, [target], [first])
    poll(bot, [target], [completed])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 1


@pytest.mark.parametrize("text", [
    "Acquired WR Diontae Johnson from Chicago.",
    "Traded WR Diontae Johnson to Carolina.",
    "Signed WR Diontae Johnson.",
    "Acquired WR Adam Thielen from Carolina.",
])
def test_other_moves_are_not_conflated_with_the_trade(tmp_path, text):
    other = espn(text)
    assert not same_trade(club(), other)
    assert len(merge_roster_moves([other], [club()])) == 2
    bot, target, _ = setup_bot(tmp_path)
    poll(bot, [target], [club()])
    poll(bot, [target], [other])
    assert target.destination.send.await_count == 2


def test_day_boundary_matches_but_unrelated_later_trade_does_not():
    assert same_trade(club(), replace(espn(), date=date(2026, 10, 8)))
    assert not same_trade(club(), replace(espn(), date=date(2026, 10, 10)))


def test_legacy_posts_without_ids_are_not_reposted(tmp_path):
    bot, target, edit = setup_bot(tmp_path)
    first = club()
    bot.announcement_state.mark(channel_key(transaction_announcement_key(first), target.key_id))
    poll(bot, [target], [first])
    assert target.destination.send.await_count == 0
    assert edit.await_count == 0


def test_trade_edits_preserve_carried_injury_news(tmp_path):
    bot, target, edit = setup_bot(tmp_path)
    injury = InjuryUpdate(PlayerRef("Diontae Johnson"), "Questionable", "Knee")
    news = RosterNews(club(), (injury,))
    assert decode_news(encode_news(news)) == news
    poll(bot, [target], [club()], [injury])
    poll(bot, [target], [espn()], [injury])
    assert any(field.name == "Injury report \u2014 Questionable"
               for field in edit.call_args.kwargs["embeds"][0].fields)


def test_richer_trade_preserves_moves_filed_alongside_original(tmp_path):
    first = club(EARLY + " We have released RB Chris Collier.")
    richer = espn()
    assert "Released RB Chris Collier." in merge_roster_moves([richer], [first])[0].description
    bot, target, edit = setup_bot(tmp_path)
    poll(bot, [target], [first])
    poll(bot, [target], [richer])
    fields = {field.name: field.value for field in edit.call_args.kwargs["embeds"][0].fields}
    assert "Chris Collier" in fields["Also"]
    assert target.destination.send.await_count == 1


def test_a_trade_in_the_log_does_not_hide_a_separate_club_signing():
    signed = club("We have signed WR Diontae Johnson.")
    assert merge_roster_moves([espn()], [signed]) == [espn(), signed]


def test_incomparable_partial_reports_do_not_erase_known_terms(tmp_path):
    bot, target, edit = setup_bot(tmp_path)
    first = club(
        "We have acquired WR Diontae Johnson from Carolina in exchange for WR Chris Moore."
    )
    incomplete = espn(
        "Acquired WR Diontae Johnson and a 2027 sixth-round pick from Carolina."
    )
    poll(bot, [target], [first])
    poll(bot, [target], [incomplete])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 0


def test_finalized_trade_replaces_pending_agreement_without_extra_assets(tmp_path):
    first = club(
        "We have agreed in principle to a trade with Carolina for Diontae Johnson, "
        "pending a physical."
    )
    completed = espn(
        "Acquired WR Diontae Johnson from Carolina for a 2027 fifth-round pick."
    )
    bot, target, edit = setup_bot(tmp_path)
    poll(bot, [target], [first])
    poll(bot, [target], [completed])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 1
    assert "pending" not in str(edit.call_args.kwargs["embeds"][0].to_dict())


def test_extra_player_does_not_allow_known_pick_details_to_disappear(tmp_path):
    first = club(
        "We have acquired WR Diontae Johnson from Carolina for a 2027 fifth-round pick."
    )
    less_specific = espn(
        "Acquired WR Diontae Johnson and WR Adam Thielen from Carolina for a draft pick."
    )
    bot, target, edit = setup_bot(tmp_path)
    poll(bot, [target], [first])
    poll(bot, [target], [less_specific])
    assert target.destination.send.await_count == 1
    assert edit.await_count == 0


def test_thinner_report_can_add_a_separate_move_without_replacing_trade_terms(tmp_path):
    bot, target, edit = setup_bot(tmp_path)
    poll(bot, [target], [espn()])
    poll(bot, [target], [club(EARLY + " We have released RB Chris Collier.")])
    fields = {field.name: field.value for field in edit.call_args.kwargs["embeds"][0].fields}
    assert "Chris Collier" in fields["Also"]
    assert "fifth-round" in fields["Ravens send"]
    assert target.destination.send.await_count == 1
