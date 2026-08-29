"""Trades, which are the one move whose verb does not give away its direction.

Every description quoted here is real wording from ESPN's NFL transaction feed.
"""

from __future__ import annotations

from datetime import date

from ravens_bot.embeds import roster_news_post, transaction_embeds
from ravens_bot.espn import apply_roster, parse_transactions
from ravens_bot.models import PlayerRef, RosterNews, normalize_name
from ravens_bot.trades import find_team, split_sentences


TARGET_DATE = date(2025, 11, 4)

ROSTER = {
    normalize_name(name): PlayerRef(
        name=name, athlete_id=athlete_id, position=position
    )
    for name, athlete_id, position in (
        ("Roquan Smith", "1", "LB"),
        ("A.J. Klein", "2", "LB"),
        ("Rashad Fenton", "3", "CB"),
        ("Kenny Pickett", "4", "QB"),
        ("William Jackson III", "5", "CB"),
        ("Chase Claypool", "6", "WR"),
        ("Jacob Martin", "7", "DE"),
    )
}


def build(description: str, resolve: bool = False):
    transaction = parse_transactions(
        {"items": [{"description": description}]}, TARGET_DATE
    )[0]
    return apply_roster(transaction, ROSTER) if resolve else transaction


def names(players) -> list[str]:
    return [player.name for player in players]


def test_a_player_traded_away_is_not_read_as_an_arrival() -> None:
    trade = build("Traded CB William Jackson III to Pittsburgh.").trade

    assert trade is not None
    assert names(trade.outgoing.players) == ["William Jackson III"]
    assert trade.incoming.is_empty
    assert trade.partner is not None and trade.partner.abbreviation == "PIT"


def test_a_pick_traded_away_for_a_player_is_an_arrival() -> None:
    """"Traded" opens the sentence, but the Ravens are the ones receiving."""
    transaction = build(
        "Traded a 2023 seventh-round draft pick to Kansas City Chiefs "
        "for CB Rashad Fenton."
    )
    trade = transaction.trade

    assert trade is not None
    assert names(trade.incoming.players) == ["Rashad Fenton"]
    assert trade.outgoing.assets == "a 2023 seventh-round draft pick"
    assert transaction.adds_to_roster
    assert transaction.joining_player is not None
    assert transaction.joining_player.name == "Rashad Fenton"


def test_a_pick_acquired_for_a_player_is_a_departure() -> None:
    """The bug this guards: "Acquired" opens a sentence about a player leaving."""
    transaction = build(
        "Acquired a 2026 fifth-round draft pick from Las Vegas Raiders "
        "for QB Kenny Pickett."
    )
    trade = transaction.trade

    assert trade is not None
    assert names(trade.outgoing.players) == ["Kenny Pickett"]
    assert trade.incoming.assets == "a 2026 fifth-round draft pick"
    assert not transaction.adds_to_roster
    assert transaction.joining_player is None


def test_a_player_acquired_for_a_pick_is_an_arrival() -> None:
    transaction = build(
        "Acquired QB Kenny Pickett from Cleveland in exchange for "
        "a 2026 fifth-round draft pick."
    )
    trade = transaction.trade

    assert trade is not None
    assert names(trade.incoming.players) == ["Kenny Pickett"]
    assert trade.outgoing.assets == "a 2026 fifth-round draft pick"
    # "Cleveland" ends in a connector word, which a careless trim eats.
    assert trade.partner is not None and trade.partner.name == "Cleveland Browns"


def test_both_sides_of_a_package_deal_are_kept() -> None:
    trade = build(
        "Traded DE Jacob Martin and a 2024 fifth-round draft pick to Denver "
        "in exchange for a 2024 fourth-round draft pick."
    ).trade

    assert trade is not None
    assert names(trade.outgoing.players) == ["Jacob Martin"]
    assert trade.outgoing.assets == "a 2024 fifth-round draft pick"
    assert trade.incoming.players == ()
    assert trade.incoming.assets == "a 2024 fourth-round draft pick"


def test_moves_filed_alongside_a_trade_are_kept_out_of_it() -> None:
    """The reinstatements are their own news and belong to neither side."""
    trade = build(
        "Traded LB Roquan Smith to Chicago in exchange for LB A.J. Klein and "
        "a 2023 second-round and fifth-round draft pick. Reinstated OLBs Tyus "
        "Bowser from the physically unable to perform list (PUP) and David "
        "Ojabo from the non-football injury list (NFI) to the active roster."
    ).trade

    assert trade is not None
    assert names(trade.incoming.players) == ["A.J. Klein"]
    assert trade.incoming.assets == "a 2023 second-round and fifth-round draft pick"
    assert names(trade.outgoing.players) == ["Roquan Smith"]
    assert "Tyus Bowser" in trade.other_moves
    assert "Roquan" not in trade.other_moves


def test_a_trade_is_found_after_the_moves_filed_before_it() -> None:
    trade = build(
        "Released OL Kenyon Green. Released QB Kyle McCord. Acquired OL Fred "
        "Johnson from Jacksonville in exchange for a 2026 seventh-round pick."
    ).trade

    assert trade is not None
    assert names(trade.incoming.players) == ["Fred Johnson"]
    assert trade.partner is not None and trade.partner.abbreviation == "JAX"
    assert trade.other_moves.startswith("Released OL Kenyon Green.")


def test_a_deal_with_no_club_named_is_still_a_trade() -> None:
    trade = build(
        "Traded a 2023 second-round and a 2024 third-round draft pick in "
        "exchange for TE T.J. Hockenson, a 2023 fourth-round pick and a 2024 "
        "conditional fourth round pick."
    ).trade

    assert trade is not None
    assert trade.partner is None
    assert names(trade.incoming.players) == ["T.J. Hockenson"]
    assert (
        trade.incoming.assets
        == "a 2023 fourth-round pick and a 2024 conditional fourth round pick"
    )


def test_a_condition_attached_to_a_pick_survives() -> None:
    trade = build(
        "Traded a 2023 fifth-round draft pick, pending a physical to "
        "San Francisco 49ers for RB Jeff Wilson."
    ).trade

    assert trade is not None
    assert trade.outgoing.assets == "a 2023 fifth-round draft pick, pending a physical"
    assert trade.partner is not None and trade.partner.abbreviation == "SF"


def test_ordinary_moves_are_not_read_as_trades() -> None:
    assert build("Waived CB Daryl Worley.").trade is None
    assert build("Signed WR Tylan Wallace to the practice squad.").trade is None
    assert build("Activated TE Isaiah Likely from injured reserve.").trade is None
    # An adding verb plus a "from" is not a deal with anybody.
    assert build("Claimed CB Trent McDuffie off waivers from Miami.").trade is None
    assert (
        build("Acquired the contract of RB Owen Wright from the practice squad.").trade
        is None
    )


def test_a_signing_still_reads_as_an_arrival() -> None:
    """The trade path must not disturb how every other move is read."""
    transaction = build("Signed WR Devontez Walker to the active roster.")

    assert transaction.trade is None
    assert transaction.adds_to_roster
    assert transaction.joining_player is not None


def test_a_headline_leads_with_who_the_ravens_got() -> None:
    assert build(
        "Traded a 2023 seventh-round draft pick to Kansas City Chiefs "
        "for CB Rashad Fenton."
    ).headline == "Trade — Ravens acquire CB Rashad Fenton from the Kansas City Chiefs"
    assert build("Traded CB William Jackson III to Pittsburgh.").headline == (
        "Trade — Ravens send CB William Jackson III to the Pittsburgh Steelers"
    )
    assert build(
        "Traded a 2026 fourth-round draft pick to Miami for a 2026 fifth-round "
        "draft pick."
    ).headline == "Trade with the Miami Dolphins"


def test_a_trade_post_lists_each_side_and_links_the_players() -> None:
    transaction = build(
        "Traded LB Roquan Smith to Chicago in exchange for LB A.J. Klein and "
        "a 2023 second-round and fifth-round draft pick.",
        resolve=True,
    )

    embed = transaction_embeds([transaction], TARGET_DATE)[0]
    fields = {field.name: field.value for field in embed.fields}

    assert embed.title == (
        "Trade — Ravens acquire LB A.J. Klein from the Chicago Bears"
    )
    assert fields["Ravens receive"] == (
        "[LB A.J. Klein](https://www.espn.com/nfl/player/_/id/2)\n"
        "A 2023 second-round and fifth-round draft pick"
    )
    assert fields["Ravens send"] == (
        "[LB Roquan Smith](https://www.espn.com/nfl/player/_/id/1)"
    )


def test_a_one_player_trade_gets_a_full_size_headshot() -> None:
    transaction = build("Traded CB William Jackson III to Pittsburgh.", resolve=True)

    embed = transaction_embeds([transaction], TARGET_DATE)[0]

    assert embed.image.url is not None and "w=520" in embed.image.url
    assert embed.thumbnail.url is None


def test_a_trade_of_picks_alone_shows_the_other_club() -> None:
    """With nobody to picture, the opponent's logo says more than the Ravens'."""
    transaction = build(
        "Traded a 2026 fourth-round draft pick to Miami for a 2026 "
        "fifth-round draft pick."
    )

    embed = transaction_embeds([transaction], TARGET_DATE)[0]

    assert embed.thumbnail.url is not None and "/mia.png" in embed.thumbnail.url


def test_a_trade_carrying_injury_news_keeps_both() -> None:
    from tests.test_roster_news import build_update

    transaction = build(
        "Traded a 2023 seventh-round draft pick to Kansas City Chiefs "
        "for CB Rashad Fenton.",
        resolve=True,
    )
    fenton = transaction.players[0]
    news = RosterNews(transaction=transaction, injuries=(build_update(fenton),))

    embeds, carried = roster_news_post(news, TARGET_DATE)
    fields = {field.name: field.value for field in embeds[0].fields}

    assert embeds[0].title.startswith("Trade — Ravens acquire CB Rashad Fenton")
    assert "Ravens receive" in fields
    assert any(name.startswith("Injury report") for name in fields)
    assert len(carried) == 1


def test_a_trade_in_a_digest_keeps_its_full_wording() -> None:
    trade = build("Traded CB William Jackson III to Pittsburgh.")
    other = parse_transactions(
        {"items": [{"description": "Waived TE Jordan Murray."}]}, TARGET_DATE
    )[0]

    embed = transaction_embeds([trade, other], TARGET_DATE)[0]
    fields = {field.name: field.value for field in embed.fields}

    assert fields["Trade — Ravens send CB William Jackson III to the Pittsburgh Steelers"] == (
        "Traded CB William Jackson III to Pittsburgh."
    )


def test_a_club_is_found_by_city_nickname_or_abbreviation() -> None:
    assert find_team("Chicago").abbreviation == "CHI"
    assert find_team("the Chicago Bears").abbreviation == "CHI"
    assert find_team("Bears").abbreviation == "CHI"
    assert find_team("Chicago Bears pending a physical").abbreviation == "CHI"
    # Two clubs share each of these cities, so a bare city decides nothing.
    assert find_team("New York") is None
    assert find_team("Los Angeles") is None
    assert find_team("New York Jets").abbreviation == "NYJ"
    # A club that has moved still appears in its old transactions.
    assert find_team("Oakland").abbreviation == "LV"
    assert find_team("Sheffield") is None


def test_sentences_split_without_cutting_names() -> None:
    assert split_sentences(
        "Traded LB A.J. Klein to Chicago. Placed TE Irv Smith Jr. on injured "
        "reserve. Signed WR B C."
    ) == [
        "Traded LB A.J. Klein to Chicago.",
        "Placed TE Irv Smith Jr. on injured reserve.",
        "Signed WR B C.",
    ]


def test_a_trade_is_announced_to_the_channel_once(tmp_path) -> None:
    """The whole path: ESPN's payload in, one post out, no repeat next poll."""
    from ravens_bot.models import InjuryReport
    from tests.test_roster_news import build_bot, build_target, poll

    # A real Ravens entry, as ESPN filed it: no id, no type, a team reference by
    # URL, and the prose alone. The stamp is midnight Pacific, which is 08:00
    # UTC once the clocks go back.
    payload = {
        "items": [
            {
                "date": "2025-11-04T08:00Z",
                "description": (
                    "Acquired CB Tre'Davious White from the Los Angeles Rams "
                    "in exchange for a 2026 seventh-round draft pick. "
                    "Released RB Chris Collier."
                ),
                "team": {
                    "$ref": "http://sports.core.api.espn.com/v2/sports/football"
                    "/leagues/nfl/seasons/2025/teams/33?lang=en&region=us"
                },
            }
        ]
    }
    transactions = parse_transactions(payload, TARGET_DATE)
    target = build_target()
    bot = build_bot(tmp_path, target, seen_injuries=True)

    posts = poll(bot, target, transactions, InjuryReport(()))

    assert len(posts) == 1
    assert posts[0][0].title == (
        "Trade — Ravens acquire CB Tre'Davious White from the Los Angeles Rams"
    )

    assert poll(bot, target, transactions, InjuryReport(())) == posts


def test_a_received_player_is_an_arrival() -> None:
    """ESPN also writes an acquisition as "Received", which reads the same way."""
    trade = build(
        "Received WR Diontae Johnson and a sixth-round draft pick from "
        "Carolina in exchange for a fifth-round pick."
    ).trade

    assert trade is not None
    assert names(trade.incoming.players) == ["Diontae Johnson"]
    assert trade.incoming.assets == "a sixth-round draft pick"
    assert trade.outgoing.assets == "a fifth-round pick"
    assert trade.outgoing.is_empty is False
    assert trade.partner is not None and trade.partner.abbreviation == "CAR"


def test_a_received_trade_can_open_a_later_sentence() -> None:
    transaction = build(
        "Signed RB Myles Gaskin to a contract. Received T Cam Robinson from "
        "Jacksonville in exchange for a conditional 2026 fifth and "
        "seventh-round draft pick."
    )

    trade = transaction.trade
    assert trade is not None
    assert names(trade.incoming.players) == ["Cam Robinson"]
    assert trade.outgoing.assets == (
        "a conditional 2026 fifth and seventh-round draft pick"
    )
    assert trade.partner is not None and trade.partner.abbreviation == "JAX"
    assert trade.other_moves == "Signed RB Myles Gaskin to a contract."


def test_a_trade_named_without_a_return_side_still_reads_as_one() -> None:
    """The Ravens' own entry for the Jaire Alexander deal names only the club."""
    trade = build("Received a 2026 sixth-round pick in a trade with Philadelphia.").trade

    assert trade is not None
    assert trade.incoming.assets == "a 2026 sixth-round pick"
    assert trade.outgoing.is_empty
    assert trade.partner is not None and trade.partner.abbreviation == "PHI"


def test_a_received_arrival_is_not_read_as_a_departure() -> None:
    """The direction regression that "Received" would otherwise reintroduce."""
    transaction = build(
        "Received RB Brian Robinson Jr. from Washington in exchange for a "
        "2026 sixth-round draft pick."
    )

    assert transaction.adds_to_roster is True
    trade = transaction.trade
    assert trade is not None
    assert trade.outgoing.assets == "a 2026 sixth-round draft pick"
    assert trade.outgoing.players == ()
    assert names(trade.incoming.players) == ["Brian Robinson Jr."]


def test_espns_misspelt_trade_verb_still_reads_as_a_departure() -> None:
    """ESPN publishes "Trade" without the d, and the direction must survive it."""
    transaction = build(
        "Released LB Chris Board and WR DeVante Paker. Trade QB Mac Jones to "
        "the Jacksonville Jaguars in exchange for a 2024 draft choice."
    )

    trade = transaction.trade
    assert trade is not None
    assert transaction.adds_to_roster is False
    assert names(trade.outgoing.players) == ["Mac Jones"]
    assert trade.incoming.assets == "a 2024 draft choice"
    assert trade.partner is not None and trade.partner.abbreviation == "JAX"
