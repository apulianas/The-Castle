from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import discord

from ravens_bot.bot import (
    RavensBot,
    _AnnouncementTarget,
    injury_announcement_key,
    transaction_announcement_key,
)
from ravens_bot.config import BotConfig
from ravens_bot.embeds import (
    MAX_EMBED_CHARS,
    MAX_EMBED_FIELDS,
    MAX_FIELD_CHARS,
    roster_news_post,
    transaction_embeds,
)
from ravens_bot.espn import combine_roster_news, parse_transactions, transaction_action
from ravens_bot.espn_urls import transactions_url
from ravens_bot.models import (
    RAVENS_SLUG,
    InjuryReport,
    InjuryUpdate,
    PlayerRef,
    RosterNews,
    Transaction,
    same_player,
)
from ravens_bot.state import channel_key


EASTERN = ZoneInfo("America/New_York")
TARGET_DATE = date(2025, 11, 4)
LIKELY = PlayerRef(name="Isaiah Likely", athlete_id="4430025", position="TE")


def build_transaction(
    description: str,
    players: tuple[PlayerRef, ...] = (),
    transaction_id: str = "tx",
) -> Transaction:
    return Transaction(
        transaction_id=transaction_id,
        date=TARGET_DATE,
        description=description,
        type_text=transaction_action(description),
        players=players,
    )


def build_update(player: PlayerRef, status: str = "Active") -> InjuryUpdate:
    return InjuryUpdate(
        player=player,
        status=status,
        detail="Knee",
        updated=datetime(2025, 11, 4, 17, 30, tzinfo=timezone.utc),
    )


class FakeDestination:
    """A channel that records what it was sent, or refuses to send at all."""

    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.posts: list[tuple[str, list[discord.Embed]]] = []

    async def send(self, content: str, embeds: list[discord.Embed]) -> None:
        if self.fails:
            raise discord.DiscordException("channel is unavailable")
        self.posts.append((content, embeds))


def build_target(fails: bool = False) -> _AnnouncementTarget:
    return _AnnouncementTarget("123", "channel 123", FakeDestination(fails))


def build_bot(tmp_path, target: _AnnouncementTarget, seen_injuries: bool) -> RavensBot:
    bot = RavensBot(
        BotConfig(
            discord_token="token",
            discord_channel_ids=(123,),
            discord_webhook_urls=(),
            poll_interval_seconds=300,
            time_zone=EASTERN,
            state_file=str(tmp_path / "state.json"),
        )
    )
    if seen_injuries:
        # Injury history is what tells the bot this is not a first run.
        bot.announcement_state.mark(channel_key("injury:4:Out:", target.key_id))
    return bot


def poll(
    bot: RavensBot,
    target: _AnnouncementTarget,
    transactions: list[Transaction],
    report: InjuryReport,
) -> list[tuple[str, list[discord.Embed]]]:
    asyncio.run(bot._post_new_roster_news([target], transactions, report, TARGET_DATE))
    return target.destination.posts


def test_same_player_matches_on_id_across_different_spellings() -> None:
    """A description writes "C.J. Okoye" where the roster writes "CJ Okoye"."""
    assert same_player(
        PlayerRef(name="C.J. Okoye", athlete_id="9"),
        PlayerRef(name="CJ Okoye", athlete_id="9"),
    )
    assert same_player(PlayerRef(name="C.J. Okoye"), PlayerRef(name="CJ Okoye"))


def test_same_player_treats_two_ids_that_disagree_as_two_people() -> None:
    assert not same_player(
        PlayerRef(name="Mike Green", athlete_id="1"),
        PlayerRef(name="Mike Green", athlete_id="2"),
    )


def test_an_activation_and_its_injury_update_become_one_item() -> None:
    player = PlayerRef(name="Isaiah Likely", athlete_id="4430025", position="TE")
    transaction = build_transaction(
        "Activated TE Isaiah Likely from injured reserve.", (player,)
    )

    moves, standalone = combine_roster_news([transaction], [build_update(player)])

    assert standalone == ()
    assert len(moves) == 1
    assert moves[0].transaction is transaction
    assert [update.player.name for update in moves[0].injuries] == ["Isaiah Likely"]


def test_an_update_about_nobody_in_the_move_stays_on_its_own() -> None:
    transaction = build_transaction(
        "Signed WR Keondre Jackson to the active roster.",
        (PlayerRef(name="Keondre Jackson", athlete_id="1"),),
    )
    update = build_update(PlayerRef(name="Zay Flowers", athlete_id="2"), "Questionable")

    moves, standalone = combine_roster_news([transaction], [update])

    assert moves[0].injuries == ()
    assert [item.player.name for item in standalone] == ["Zay Flowers"]


def test_an_update_is_carried_by_only_one_move() -> None:
    """Two posts of the same status change would be the duplicate this avoids."""
    player = PlayerRef(name="Ronnie Stanley", athlete_id="3916387")
    first = build_transaction(
        "Placed OT Ronnie Stanley on injured reserve.", (player,), "tx1"
    )
    second = build_transaction(
        "Designated OT Ronnie Stanley to return.", (player,), "tx2"
    )

    moves, standalone = combine_roster_news([first, second], [build_update(player)])

    assert len(moves[0].injuries) == 1
    assert moves[1].injuries == ()
    assert standalone == ()


def test_a_move_pairs_with_an_update_espn_left_without_an_id() -> None:
    """The transaction feed resolves ids from the roster; the injury feed may not."""
    transaction = build_transaction(
        "Activated CB Jalyn Armour-Davis from injured reserve.",
        (PlayerRef(name="Jalyn Armour-Davis", athlete_id="4361050"),),
    )
    update = build_update(PlayerRef(name="Jalyn Armour-Davis"))

    moves, standalone = combine_roster_news([transaction], [update])

    assert len(moves[0].injuries) == 1
    assert standalone == ()


def test_combined_post_shows_the_move_and_the_injury_status() -> None:
    player = PlayerRef(name="Isaiah Likely", athlete_id="4430025", position="TE")
    news = RosterNews(
        transaction=build_transaction(
            "Activated TE Isaiah Likely from injured reserve.", (player,)
        ),
        injuries=(build_update(player),),
    )

    embed = roster_news_post(news, TARGET_DATE, EASTERN)[0][0]

    assert embed.title == "Ravens roster move — November 4, 2025"
    assert embed.url == transactions_url(RAVENS_SLUG)
    assert [field.name for field in embed.fields] == [
        "Activated — TE Isaiah Likely",
        "Injury report — Active",
    ]
    assert "Knee" in embed.fields[1].value
    assert "Updated" in (embed.description or "")


def test_a_move_with_no_injury_news_posts_exactly_as_before() -> None:
    transaction = build_transaction(
        "Signed WR Keondre Jackson to the active roster.",
        (PlayerRef(name="Keondre Jackson", athlete_id="4878287"),),
    )

    combined = roster_news_post(RosterNews(transaction), TARGET_DATE, EASTERN)[0][0]
    plain = transaction_embeds([transaction], TARGET_DATE)[0]

    assert combined.to_dict() == plain.to_dict()


def test_a_combined_post_about_one_player_keeps_the_full_size_headshot() -> None:
    player = PlayerRef(name="Isaiah Likely", athlete_id="4430025", position="TE")
    news = RosterNews(
        transaction=build_transaction(
            "Activated TE Isaiah Likely from injured reserve.", (player,)
        ),
        injuries=(build_update(player),),
    )

    embed = roster_news_post(news, TARGET_DATE, EASTERN)[0][0]

    assert embed.image.url is not None and "w=520" in embed.image.url
    assert embed.thumbnail.url is None


def test_a_combined_post_pictures_the_player_joining_the_roster() -> None:
    """The arrival is the news; the player heading to injured reserve is not."""
    arriving = PlayerRef(name="Keondre Jackson", athlete_id="1", position="WR")
    leaving = PlayerRef(name="Ronnie Stanley", athlete_id="2", position="OT")
    news = RosterNews(
        transaction=build_transaction(
            "Signed WR Keondre Jackson to the active roster. "
            "Placed OT Ronnie Stanley on injured reserve.",
            (arriving, leaving),
        ),
        injuries=(build_update(leaving, "Injured Reserve"),),
    )

    embed = roster_news_post(news, TARGET_DATE, EASTERN)[0][0]

    assert embed.image.url is None
    assert embed.thumbnail.url is not None and "full%2F1.png" in embed.thumbnail.url


def test_a_combined_post_without_an_arrival_still_pictures_the_player() -> None:
    player = PlayerRef(name="Ronnie Stanley", athlete_id="2", position="OT")
    news = RosterNews(
        transaction=build_transaction(
            "Placed OT Ronnie Stanley on injured reserve.", (player,)
        ),
        injuries=(build_update(player, "Injured Reserve"),),
    )

    embed = roster_news_post(news, TARGET_DATE, EASTERN)[0][0]

    assert embed.image.url is not None and "full%2F2.png" in embed.image.url


def test_a_digest_of_moves_pictures_the_arrival_not_the_departure() -> None:
    departure = build_transaction(
        "Placed OT Ronnie Stanley on injured reserve.",
        (PlayerRef(name="Ronnie Stanley", athlete_id="2"),),
        "tx1",
    )
    arrival = build_transaction(
        "Activated CB Jalyn Armour-Davis from injured reserve.",
        (PlayerRef(name="Jalyn Armour-Davis", athlete_id="1"),),
        "tx2",
    )

    embed = transaction_embeds([departure, arrival], TARGET_DATE)

    assert "full%2F1.png" in embed[0].thumbnail.url


def test_transactions_are_read_as_adding_or_removing_a_player() -> None:
    assert build_transaction("Activated TE A B from injured reserve.").adds_to_roster
    assert build_transaction("Re-signed WR A B to the practice squad.").adds_to_roster
    assert not build_transaction("Placed OT A B on injured reserve.").adds_to_roster
    assert not build_transaction("Waived CB A B.").adds_to_roster


def test_a_hyphenated_action_is_read_whole() -> None:
    """"Re-signed" read as "Re" left the headline saying "Re — WR ..."."""
    assert transaction_action("Re-signed WR Tylan Wallace.") == "Re-signed"

    transaction = parse_transactions(
        {"items": [{"description": "Re-signed WR Tylan Wallace."}]}, TARGET_DATE
    )[0]

    assert transaction.headline == "Re-signed — WR Tylan Wallace"


def test_a_combined_post_stays_inside_discords_limits() -> None:
    """A cutdown-day move can name twenty players who are all on the report."""
    players = tuple(
        PlayerRef(name=f"Player Number{index}", athlete_id=str(index), position="WR")
        for index in range(30)
    )
    news = RosterNews(
        transaction=build_transaction("Waived thirty players." * 40, players),
        injuries=tuple(build_update(player, "Out") for player in players),
    )

    embed = roster_news_post(news, TARGET_DATE, EASTERN)[0][0]

    assert len(embed) <= MAX_EMBED_CHARS
    assert len(embed.fields) <= MAX_EMBED_FIELDS
    assert all(len(field.value) <= MAX_FIELD_CHARS for field in embed.fields)


def test_players_sharing_a_status_share_a_field() -> None:
    first = PlayerRef(name="Zay Flowers", athlete_id="1", position="WR")
    second = PlayerRef(name="Nate Wiggins", athlete_id="2", position="CB")
    news = RosterNews(
        transaction=build_transaction(
            "Placed WR Zay Flowers and CB Nate Wiggins on injured reserve.",
            (first, second),
        ),
        injuries=(build_update(first, "Out"), build_update(second, "Out")),
    )

    embed = roster_news_post(news, TARGET_DATE, EASTERN)[0][0]

    assert [field.name for field in embed.fields][1:] == ["Injury report — Out"]
    assert "Zay Flowers" in embed.fields[1].value
    assert "Nate Wiggins" in embed.fields[1].value


def test_a_move_too_big_for_one_post_leaves_the_rest_to_be_announced(tmp_path) -> None:
    """Marking an update the embed had to drop would lose that news for good."""
    players = tuple(
        PlayerRef(name=f"Player Number{index}", athlete_id=str(index), position="WR")
        for index in range(24)
    )
    updates = tuple(
        InjuryUpdate(
            player=player,
            status="Out",
            comment="Did not practice on Wednesday. " * 8,
            updated=datetime(2025, 11, 4, 17, 30, tzinfo=timezone.utc),
        )
        for player in players
    )
    transaction = build_transaction("Waived twenty-four players.", players)
    target = build_target()
    bot = build_bot(tmp_path, target, seen_injuries=True)

    posts = poll(bot, target, [transaction], InjuryReport(updates))

    assert len(posts) == 1
    assert posts[0][1][0].footer.text.startswith("Showing ")
    dropped = [
        update
        for update in updates
        if bot._unseen(target, injury_announcement_key(update))
    ]
    assert dropped

    later = poll(bot, target, [transaction], InjuryReport(updates))

    assert [content for content, _ in later[1:]] == [
        f"Ravens injury update: {update.player.display_name}" for update in dropped
    ]


def test_an_unbounded_espn_comment_still_fits_a_field() -> None:
    """A field Discord rejects would leave the whole post unannounced."""
    player = PlayerRef(name="Isaiah Likely", athlete_id="4430025", position="TE")
    news = RosterNews(
        transaction=build_transaction(
            "Activated TE Isaiah Likely from injured reserve.", (player,)
        ),
        injuries=(
            InjuryUpdate(
                player=player,
                status="Active",
                comment="Practised in full on Wednesday. " * 60,
            ),
        ),
    )

    embeds, carried = roster_news_post(news, TARGET_DATE, EASTERN)

    assert len(carried) == 1
    assert all(len(field.value) <= MAX_FIELD_CHARS for field in embeds[0].fields)


def test_polling_announces_an_activation_as_one_post(tmp_path) -> None:
    target = build_target()
    bot = build_bot(tmp_path, target, seen_injuries=True)
    transaction = build_transaction(
        "Activated TE Isaiah Likely from injured reserve.", (LIKELY,)
    )
    report = InjuryReport((build_update(LIKELY),))

    posts = poll(bot, target, [transaction], report)

    assert len(posts) == 1
    content, embeds = posts[0]
    assert content == "Ravens roster move: Activated — TE Isaiah Likely"
    assert [field.name for field in embeds[0].fields] == [
        "Activated — TE Isaiah Likely",
        "Injury report — Active",
    ]


def test_polling_again_repeats_neither_half_of_a_combined_post(tmp_path) -> None:
    target = build_target()
    bot = build_bot(tmp_path, target, seen_injuries=True)
    transaction = build_transaction(
        "Activated TE Isaiah Likely from injured reserve.", (LIKELY,)
    )
    report = InjuryReport((build_update(LIKELY),))

    poll(bot, target, [transaction], report)
    posts = poll(bot, target, [transaction], report)

    assert len(posts) == 1


def test_an_update_after_the_move_was_posted_is_still_announced(tmp_path) -> None:
    """A move already posted must not swallow injury news that arrives later."""
    target = build_target()
    bot = build_bot(tmp_path, target, seen_injuries=True)
    transaction = build_transaction(
        "Activated TE Isaiah Likely from injured reserve.", (LIKELY,)
    )
    bot.announcement_state.mark(
        channel_key(transaction_announcement_key(transaction), target.key_id)
    )

    posts = poll(bot, target, [transaction], InjuryReport((build_update(LIKELY),)))

    assert [content for content, _ in posts] == [
        "Ravens injury update: TE Isaiah Likely"
    ]


def test_a_first_run_posts_the_standing_report_and_the_move_separately(
    tmp_path,
) -> None:
    """The consolidated report covers the whole list, so nothing merges into it."""
    target = build_target()
    bot = build_bot(tmp_path, target, seen_injuries=False)
    transaction = build_transaction(
        "Activated TE Isaiah Likely from injured reserve.", (LIKELY,)
    )

    posts = poll(bot, target, [transaction], InjuryReport((build_update(LIKELY),)))

    assert [content for content, _ in posts] == [
        "Ravens injury report",
        "Ravens roster move: Activated — TE Isaiah Likely",
    ]


def test_a_combined_post_that_fails_to_send_records_nothing(tmp_path) -> None:
    target = build_target(fails=True)
    bot = build_bot(tmp_path, target, seen_injuries=True)
    transaction = build_transaction(
        "Activated TE Isaiah Likely from injured reserve.", (LIKELY,)
    )
    update = build_update(LIKELY)

    assert poll(bot, target, [transaction], InjuryReport((update,))) == []
    assert bot._unseen(target, transaction_announcement_key(transaction))
    assert bot._unseen(target, injury_announcement_key(update))
