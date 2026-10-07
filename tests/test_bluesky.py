from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ravens_bot.bluesky import (
    BlueskyClient,
    BlueskyError,
    BlueskyPost,
    GraphicInjuryTable,
    INJURY_POST,
    NameMatcher,
    game_injuries,
    merge_roster_moves,
    parse_feed,
    parse_game_injury,
    parse_roster_move,
    roster_moves_on,
    parse_inactives_graphic,
    parse_injury_graphic,
    posts_on,
)
from ravens_bot.bot import RavensBot, _AnnouncementTarget
from ravens_bot.embeds import game_injury_embed, official_injury_embed, transaction_embeds
from ravens_bot.injury_report import (
    InjuryTable,
    OfficialInjuryReport,
    merge_graphic_table,
)
from ravens_bot.models import (
    Game,
    GameTeam,
    InactivePlayer,
    InactiveReport,
    InjuryReport,
    PlayerRef,
    TeamRef,
    Transaction,
)
from ravens_bot.state import AnnouncementState


DATA = Path(__file__).parent / "data"
EASTERN = ZoneInfo("America/New_York")
ROSTER = (
    PlayerRef("Mark Andrews", "3116365", "TE"),
    PlayerRef("Calais Campbell", "11258", "DT"),
    PlayerRef("Zay Flowers", "4429205", "WR"),
    PlayerRef("Jovaughn Gwyn", "4362470", "G"),
    PlayerRef("Trey Hendrickson", "3052743", "LB"),
    PlayerRef("Marlon Humphrey", "3040506", "CB"),
    PlayerRef("Lamar Jackson", "3916387", "QB"),
    PlayerRef("Chris Moore", "2576581", "WR"),
    PlayerRef("Nick Moore", "3138707", "LS"),
    PlayerRef("Ethan Pocic", "3051927", "C"),
    PlayerRef("John Simpson", "3915437", "G"),
    PlayerRef("Durham Smythe", "3052897", "TE"),
    PlayerRef("Ronnie Stanley", "2978336", "T"),
    PlayerRef("Aeneas Peebles", "4565311", "DT"),
    PlayerRef("Joe Fagnano", "4429582", "QB"),
)


def _ocr(name: str):
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


def test_friday_graphic_reads_into_the_website_s_columns() -> None:
    table = parse_injury_graphic(_ocr("bluesky_game_status_ocr"), ROSTER)

    assert table is not None
    assert table.week == "WEEK 4"
    assert table.headers == (
        "Player", "Position", "Injury", "Wed", "Thu", "Fri", "Game Status",
    )
    assert len(table.rows) == 12
    assert table.rows[0] == ("Mark Andrews", "TE", "Hand", "FP", "FP", "FP", "(-)")
    assert table.rows[2] == (
        "Zay Flowers", "WR", "Hamstring/NIR - Rest", "LP", "FP", "LP", "QUESTIONABLE",
    )
    assert table.rows[3][:2] == ("Jovaughn Gwyn", "C")
    assert table.rows[3][-1] == "OUT"
    assert table.rows[-1] == (
        "Ronnie Stanley", "T", "Toe/NIR - Rest", "LP", "LP", "DNP", "QUESTIONABLE",
    )
    assert all(table.headshots)
    assert "3916387" in (table.headshots[6] or "")


def test_thursday_graphic_has_no_friday_and_no_status_yet() -> None:
    table = parse_injury_graphic(_ocr("bluesky_thursday_report_ocr"), ROSTER)

    assert table is not None
    assert table.headers == ("Player", "Position", "Injury", "Wed", "Thu", "Game Status")
    assert table.rows[7] == ("Chris Moore", "WR", "Ankle", "LP", "LP", "(-)")


def test_inactives_graphic_resolves_names_through_layout_noise() -> None:
    players = parse_inactives_graphic(_ocr("bluesky_inactives_ocr"), ROSTER)

    assert [(p.position, p.name) for p in players] == [
        ("WR", "Chris Moore"),
        ("DT", "Aeneas Peebles"),
        ("OLB", "Trey Hendrickson"),
        ("QB", "Joe Fagnano"),
    ]
    assert all(p.is_ravens and p.athlete_id for p in players)


def test_inactives_without_a_roster_keep_lines_that_open_with_a_position() -> None:
    players = parse_inactives_graphic(_ocr("bluesky_inactives_ocr"), ())

    names = [p.name for p in players]
    assert "Chris Moore" in names
    assert "Joe Fagnano" in names
    assert all(p.athlete_id is None for p in players)


def test_a_graphic_without_a_table_is_not_a_report() -> None:
    assert parse_injury_graphic(_ocr("bluesky_inactives_ocr"), ROSTER) is None
    assert parse_inactives_graphic(_ocr("bluesky_game_status_ocr"), ROSTER) == ()


def test_matcher_prefers_the_longer_name_and_forgives_a_misread() -> None:
    matcher = NameMatcher(ROSTER)

    assert matcher.find("WR-CHRISMOORE").name == "Chris Moore"
    assert matcher.find("OLB-TREYHENDRICKS0N").name == "Trey Hendrickson"
    assert matcher.find("3RD QB") is None


def test_matcher_ignores_a_name_suffix_the_graphic_leaves_off() -> None:
    matcher = NameMatcher((PlayerRef("Kevin Winston Jr.", "1"),))

    assert matcher.find("S-KEVINWINSTON").name == "Kevin Winston Jr."


FEED = {
    "feed": [
        {
            "post": {
                "uri": "at://did:plc:x/app.bsky.feed.post/abc",
                "author": {"handle": "ravensbot.bsky.social"},
                "record": {"text": "Game status vs. Titans", "createdAt": "2026-10-02T20:05:38.000Z"},
                "embed": {"images": [{"fullsize": "https://cdn/img1"}]},
            }
        },
        {
            "post": {
                "uri": "at://did:plc:y/app.bsky.feed.post/def",
                "author": {"handle": "someone.else"},
                "record": {"text": "Injury Report", "createdAt": "2026-10-02T21:00:00.000Z"},
                "embed": {"images": [{"fullsize": "https://cdn/img2"}]},
            },
            "reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
        },
        {
            "post": {
                "uri": "at://did:plc:x/app.bsky.feed.post/ghi",
                "author": {"handle": "ravensbot.bsky.social"},
                "record": {"text": "Thursday's Injury Report", "createdAt": "2026-10-01T20:21:29.000Z"},
                "embed": {"images": [{"fullsize": "https://cdn/img3"}]},
            }
        },
    ]
}


def test_feed_keeps_the_account_s_own_image_posts() -> None:
    posts = parse_feed(FEED)

    assert [post.image_urls for post in posts] == [("https://cdn/img1",), ("https://cdn/img3",)]
    assert posts[0].url == "https://bsky.app/profile/ravensbot.bsky.social/post/abc"


def test_only_the_day_s_report_posts_are_considered() -> None:
    posts = parse_feed(FEED)

    friday = posts_on(posts, INJURY_POST, date(2026, 10, 2), EASTERN)
    assert [post.uri.rsplit("/", 1)[-1] for post in friday] == ["abc"]
    assert posts_on(posts, INJURY_POST, date(2026, 10, 3), EASTERN) == []


class _Client(BlueskyClient):
    def __init__(self, posts, results) -> None:
        super().__init__(session=None, recognize=lambda data: [])  # type: ignore[arg-type]
        self.posts = posts
        self.results = results
        self.reads: list[str] = []

    async def fetch_posts(self, limit=100):
        return self.posts

    async def read_image(self, url):
        self.reads.append(url)
        return self.results[url]


def test_client_reads_the_day_s_graphic_and_links_the_post() -> None:
    client = _Client(
        parse_feed(FEED),
        {"https://cdn/img1": _ocr("bluesky_game_status_ocr")},
    )

    table = asyncio.run(client.fetch_injury_table(date(2026, 10, 2), EASTERN, ROSTER))

    assert table is not None
    assert table.post_url.endswith("/post/abc")
    assert client.reads == ["https://cdn/img1"]


class _Content:
    def __init__(self, chunks) -> None:
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(self, chunks) -> None:
        self.content = _Content(chunks)

    def raise_for_status(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, chunks) -> None:
        self.chunks = chunks
        self.gets = 0

    def get(self, url, **kwargs):
        self.gets += 1
        return _Response(self.chunks)


def test_a_graphic_is_read_whole_and_only_once() -> None:
    seen: list[bytes] = []

    def recognize(data: bytes):
        seen.append(data)
        return [([[0, 0], [1, 0], [1, 1], [0, 1]], "TEXT", 1.0)]

    session = _Session([b"abc", b"def", b"ghi"])
    client = BlueskyClient(session, recognize=recognize)  # type: ignore[arg-type]

    first = asyncio.run(client.read_image("https://cdn/img"))
    second = asyncio.run(client.read_image("https://cdn/img"))

    assert seen == [b"abcdefghi"]
    assert first is second
    assert session.gets == 1


def test_an_unreadable_graphic_is_a_bluesky_error() -> None:
    def recognize(data: bytes):
        raise OSError("could not create decoder object")

    client = BlueskyClient(_Session([b"x"]), recognize=recognize)  # type: ignore[arg-type]

    try:
        asyncio.run(client.read_image("https://cdn/img"))
    except BlueskyError as exc:
        assert "could not be read" in str(exc)
    else:
        raise AssertionError("expected BlueskyError")


def _graphic(days: int = 3) -> GraphicInjuryTable:
    table = parse_injury_graphic(
        _ocr("bluesky_game_status_ocr" if days == 3 else "bluesky_thursday_report_ocr"),
        ROSTER,
    )
    assert table is not None
    return GraphicInjuryTable(
        week=table.week,
        headers=table.headers,
        rows=table.rows,
        headshots=table.headshots,
        post_url="https://bsky.app/profile/ravensbot.bsky.social/post/abc",
    )


HEADERS = ("Player", "Position", "Injury", "Wed", "Thu", "Fri", "Game Status")
TITANS = InjuryTable(
    team="Tennessee Titans",
    headers=HEADERS,
    rows=(("Tony Pollard", "RB", "Foot", "DNP", "FP", "FP", "(-)"),),
)


def _official(fri: str = "") -> OfficialInjuryReport:
    return OfficialInjuryReport(
        week="WEEK 4",
        tables=(
            InjuryTable(
                team="Baltimore Ravens",
                headers=HEADERS,
                rows=(("Mark Andrews", "TE", "Hand", "FP", "FP", fri, "(-)"),),
                headshots=("https://nfl/andrews.png",),
            ),
            TITANS,
        ),
    )


def test_a_newer_graphic_replaces_only_the_ravens_table() -> None:
    merged = merge_graphic_table(_official(), _graphic())

    assert merged is not None
    assert merged.tables[1] is TITANS
    assert merged.tables[0].headers == HEADERS
    assert len(merged.tables[0].rows) == 12
    assert merged.graphic_url is not None
    assert "Bluesky" in official_injury_embed(merged).footer.text


def test_the_website_wins_once_it_has_caught_up() -> None:
    official = _official(fri="FP")

    assert merge_graphic_table(official, _graphic()) is official


def test_a_thursday_graphic_is_laid_out_under_friday_s_columns() -> None:
    official = OfficialInjuryReport(
        week="WEEK 4",
        tables=(
            InjuryTable(
                team="Baltimore Ravens",
                headers=HEADERS,
                rows=(("Mark Andrews", "TE", "Hand", "FP", "", "", "(-)"),),
            ),
            TITANS,
        ),
    )

    merged = merge_graphic_table(official, _graphic(days=2))

    assert merged is not None
    assert merged.tables[0].rows[0] == (
        "Mark Andrews", "TE", "Hand", "FP", "FP", "-", "(-)",
    )


def test_website_still_on_last_week_gives_a_ravens_only_report() -> None:
    old = OfficialInjuryReport(week="WEEK 3", tables=(TITANS,))

    merged = merge_graphic_table(old, _graphic())

    assert merged is not None
    assert merged.week == "WEEK 4"
    assert [table.team for table in merged.tables] == ["Baltimore Ravens"]
    assert merge_graphic_table(None, _graphic()).week == "WEEK 4"


def test_a_stale_graphic_never_overrides_a_newer_week() -> None:
    newer = OfficialInjuryReport(week="WEEK 5", tables=(TITANS,))

    assert merge_graphic_table(newer, _graphic()) is newer
    assert merge_graphic_table(newer, None) is newer


RAVENS = TeamRef("Baltimore Ravens", "33", "BAL", "bal")
TITANS_TEAM = TeamRef("Tennessee Titans", "10", "TEN", "ten")
GAME = Game(
    "401",
    "Tennessee Titans at Baltimore Ravens",
    "TEN @ BAL",
    datetime(2026, 10, 4, 17, tzinfo=timezone.utc),
    "Pre-Game",
    home=GameTeam(RAVENS, is_home=True),
    away=GameTeam(TITANS_TEAM),
)


class _Inactives:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.calls = 0
        self.result = result
        self.error = error

    async def fetch_inactives(self, day, time_zone, players):
        self.calls += 1
        if self.error:
            raise self.error
        return parse_inactives_graphic(_ocr("bluesky_inactives_ocr"), players)


class _Espn:
    async def fetch_roster(self):
        return {player.name: player for player in ROSTER}


def _bot(bluesky) -> RavensBot:
    bot = RavensBot.__new__(RavensBot)
    bot.bluesky = bluesky
    bot.espn = _Espn()  # type: ignore[assignment]
    bot.config = type("C", (), {"time_zone": EASTERN})()
    return bot


def test_graphic_fills_the_ravens_while_espn_has_only_the_opponent() -> None:
    bluesky = _Inactives()
    titan = InactivePlayer("Tony Pollard", "Tennessee Titans", "Foot")
    report = InactiveReport(game=GAME, players=(titan,))

    filled = asyncio.run(_bot(bluesky)._with_graphic_inactives(report, date(2026, 10, 4)))

    assert filled.players[0] is titan
    assert [p.name for p in filled.players[1:]] == [
        "Chris Moore", "Aeneas Peebles", "Trey Hendrickson", "Joe Fagnano",
    ]


def test_espn_s_ravens_list_is_left_alone() -> None:
    bluesky = _Inactives()
    report = InactiveReport(
        game=GAME, players=(InactivePlayer("Chris Moore", is_ravens=True, reason="Ankle"),)
    )

    assert asyncio.run(_bot(bluesky)._with_graphic_inactives(report, date(2026, 10, 4))) is report
    assert bluesky.calls == 0


def test_a_bluesky_outage_leaves_espn_s_answer_alone(caplog) -> None:
    report = InactiveReport(game=GAME, players=())
    bot = _bot(_Inactives(error=BlueskyError("down")))

    assert asyncio.run(bot._with_graphic_inactives(report, date(2026, 10, 4))) is report
    assert "Bluesky inactives graphic unavailable" in caplog.text

KICKOFF = datetime(2026, 10, 4, 17, tzinfo=timezone.utc)


def _post(text: str, minutes: int = 30, rkey: str | None = None) -> BlueskyPost:
    return BlueskyPost(
        uri=f"at://did:plc:x/app.bsky.feed.post/{rkey or abs(hash(text))}",
        text=text,
        created_at=KICKOFF + timedelta(minutes=minutes),
        image_urls=(),
    )


def test_in_game_lines_read_player_injury_and_status() -> None:
    cases = {
        "TE Durham Smythe (Achilles) has been ruled out.": ("TE", "Durham Smythe", "Achilles", "Out"),
        "QB Lamar Jackson (ankle) is questionable to return.": ("QB", "Lamar Jackson", "Ankle", "Questionable to return"),
        "Marlon Humphrey (calf) is questionable to return.": (None, "Marlon Humphrey", "Calf", "Questionable to return"),
        "WR Zay Flowers (knee) is doubtful to return.": ("WR", "Zay Flowers", "Knee", "Doubtful to return"),
        "T Ronnie Stanley is being evaluated for a concussion.": ("T", "Ronnie Stanley", None, "Being evaluated"),
        "CB Marlon Humphrey has returned to the game.": ("CB", "Marlon Humphrey", None, "Returned"),
        "DT Calais Campbell (shoulder) will not return.": ("DT", "Calais Campbell", "Shoulder", "Out"),
    }
    for text, expected in cases.items():
        update = parse_game_injury(_post(text))
        assert update is not None, text
        assert (update.position, update.name, update.injury, update.status) == expected


def test_other_posts_are_not_injury_lines() -> None:
    for text in (
        "Game status vs. Titans",
        "We have placed C Jovaughn Gwyn and C Ethan Pocic on Injured Reserve.",
        "Derrick Henry has returned a kick 98 yards for a touchdown!",
        "TYLER LOOP 64-YARD FIELD GOAL",
        "Inactives vs. Titans",
        "Lamar Jackson is ready to roll.",
    ):
        assert parse_game_injury(_post(text)) is None, text


def test_only_lines_since_kickoff_count_oldest_first() -> None:
    posts = [
        _post("CB Marlon Humphrey (calf) has been ruled out.", 90),
        _post("CB Marlon Humphrey (calf) is questionable to return.", 60),
        _post("TE Durham Smythe (Achilles) has been ruled out.", -60 * 24 * 7),
    ]

    updates = game_injuries(posts, KICKOFF)

    assert [update.status for update in updates] == ["Questionable to return", "Out"]


def test_in_game_embed_has_headshot_status_and_link() -> None:
    update = parse_game_injury(_post("Marlon Humphrey (calf) is questionable to return.", rkey="abc"))
    player = NameMatcher(ROSTER).find(update.name)

    embed = game_injury_embed(update, player, GAME)

    assert embed.title == "CB Marlon Humphrey (Calf): Questionable to return"
    assert embed.description == "Marlon Humphrey (calf) is questionable to return."
    assert embed.url == "https://bsky.app/profile/ravensbot.bsky.social/post/abc"
    assert "3040506" in embed.thumbnail.url
    assert embed.footer.text.startswith("In-game update vs. Tennessee Titans")


class _Destination:
    def __init__(self) -> None:
        self.sent: list[list] = []

    async def send(self, embeds):
        self.sent.append(embeds)


class _GameFeed:
    def __init__(self, posts=(), error: Exception | None = None) -> None:
        self.posts = list(posts)
        self.error = error

    async def fetch_game_injuries(self, since):
        if self.error:
            raise self.error
        return game_injuries(self.posts, since)


def _game_bot(feed, tmp_path):
    bot = _bot(feed)
    bot.config = type("C", (), {"time_zone": EASTERN, "has_announcement_targets": True})()
    bot.announcement_state = AnnouncementState(str(tmp_path / "state.json"))
    destination = _Destination()

    async def targets():
        return [_AnnouncementTarget("1", "channel 1", destination)]

    bot._announcement_targets = targets  # type: ignore[method-assign]
    return bot, destination


def test_each_in_game_line_is_posted_once(tmp_path) -> None:
    feed = _GameFeed([_post("CB Marlon Humphrey (calf) is questionable to return.", 60, "a")])
    bot, destination = _game_bot(feed, tmp_path)

    asyncio.run(bot._post_game_injuries(GAME))
    asyncio.run(bot._post_game_injuries(GAME))
    feed.posts.append(_post("CB Marlon Humphrey (calf) has been ruled out.", 90, "b"))
    asyncio.run(bot._post_game_injuries(GAME))

    assert [embeds[0].title for embeds in destination.sent] == [
        "CB Marlon Humphrey (Calf): Questionable to return",
        "CB Marlon Humphrey (Calf): Out",
    ]


def test_a_bluesky_outage_during_a_game_posts_nothing(tmp_path) -> None:
    bot, destination = _game_bot(_GameFeed(error=BlueskyError("down")), tmp_path)

    asyncio.run(bot._post_game_injuries(GAME))

    assert destination.sent == []


def test_spelled_out_and_line_positions_are_read() -> None:
    center = parse_game_injury(_post("Center Jovaughn Gwynn (ankle) is questionable to return."))
    tackle = parse_game_injury(_post("LT Ronnie Stanley (toe) is questionable to return."))
    receiver = parse_game_injury(_post("Wide receiver Zay Flowers (hamstring) has been ruled out."))

    assert (center.position, center.name) == ("C", "Jovaughn Gwynn")
    assert (tackle.position, tackle.name) == ("LT", "Ronnie Stanley")
    assert (receiver.position, receiver.name, receiver.status) == ("WR", "Zay Flowers", "Out")


def test_a_surname_follow_up_takes_the_player_from_the_earlier_line() -> None:
    posts = [
        _post("S Kyle Hamilton is being evaluated for a concussion.", 30),
        _post("Hamilton has now returned to the game.", 32),
        _post("Andrews has now returned to the game.", 40),
    ]

    updates = game_injuries(posts, KICKOFF)

    assert [(u.position, u.name, u.status) for u in updates] == [
        ("S", "Kyle Hamilton", "Being evaluated"),
        ("S", "Kyle Hamilton", "Returned"),
    ]


def test_walkthrough_day_graphics_count_as_injury_reports() -> None:
    assert INJURY_POST.search("We held a walkthrough on Friday, the report is a practice estimation.")


def _move_post(text: str, rkey: str = "m1", when: datetime | None = None) -> BlueskyPost:
    return BlueskyPost(
        uri=f"at://did:plc:x/app.bsky.feed.post/{rkey}",
        text=text,
        created_at=when or datetime(2026, 10, 3, 20, 1, tzinfo=timezone.utc),
        image_urls=(),
    )


def test_club_roster_moves_read_like_the_move_log() -> None:
    move = parse_roster_move(
        _move_post(
            "We have placed C Jovaughn Gwyn and C Ethan Pocic on Injured Reserve.\n\n"
            "We have also activated G Kyle Hergel (standard elevation) from the practice squad."
        ),
        EASTERN,
    )

    assert move.transaction_id == "bluesky:m1"
    assert move.date == date(2026, 10, 3)
    assert move.description == (
        "Placed C Jovaughn Gwyn and C Ethan Pocic on Injured Reserve. "
        "Activated G Kyle Hergel (standard elevation) from the practice squad."
    )
    assert [p.display_name for p in move.players] == [
        "C Jovaughn Gwyn", "C Ethan Pocic", "G Kyle Hergel",
    ]


def test_other_wording_and_links_are_handled() -> None:
    elevation = parse_roster_move(
        _move_post("The Ravens activated (standard practice elevation) S K'Von Wallace for tomorrow's game."),
        EASTERN,
    )
    signing = parse_roster_move(
        _move_post("We have signed S K'Von Wallace to the 53-man roster.\n\nhttps://www.baltimoreravens.com/news/x"),
        EASTERN,
    )

    assert elevation.description.startswith("Activated (standard practice elevation) S K'Von Wallace")
    assert signing.description == "Signed S K'Von Wallace to the 53-man roster."
    for text in ("We're up 24-10 heading to the 4th.", "We have a new episode of Wired tonight!"):
        assert parse_roster_move(_move_post(text), EASTERN) is None


@pytest.mark.parametrize("text", [
    "We have traded LB Roquan Smith to the Chicago Bears for a 2027 second-round pick.",
    "We have acquired WR Diontae Johnson from the Carolina Panthers in exchange for a 2027 fifth-round pick.",
    "We have received WR Diontae Johnson in a trade with the Carolina Panthers.",
    "We have acquired Diontae Johnson from the Carolina Panthers in exchange for a 2027 fifth-round pick.",
    "We have acquired wide receiver Diontae Johnson from the Carolina Panthers in exchange for a 2027 fifth-round pick.",
    "We have traded a 2027 fifth-round pick to the Carolina Panthers for a 2027 sixth-round pick.",
])
def test_bluesky_trades_link_to_the_post_and_credit_the_source(text: str) -> None:
    post = replace(_move_post(text), handle="club.example")
    move = parse_roster_move(post, EASTERN)

    assert move is not None
    assert move.trade is not None
    assert move.source_url == post.url
    assert merge_roster_moves([], [move]) == [move]
    embed = transaction_embeds([move], move.date)[0]
    assert embed.url == "https://bsky.app/profile/club.example/post/m1"
    assert embed.footer.text.endswith("Baltimore Ravens via Bluesky")
    assert "Ravens receive" in [field.name for field in embed.fields]


@pytest.mark.parametrize("announcement", [
    "We have agreed to terms on a trade with the Carolina Panthers for WR Diontae Johnson",
    "We've agreed in principle to a trade with the Carolina Panthers for Diontae Johnson",
    "The Ravens have agreed to a trade with the Carolina Panthers for a 2027 fifth-round pick",
    "We have agreed to trade WR Chris Moore to the Carolina Panthers for a 2027 fifth-round pick",
    "We have agreed to acquire WR Diontae Johnson from the Carolina Panthers",
])
def test_trade_agreements_preserve_the_announced_terms(announcement: str) -> None:
    terms = ", pending a physical."
    move = parse_roster_move(_move_post(f"{announcement}{terms}"), EASTERN)

    assert move is not None
    assert move.type_text == "Trade agreement"
    assert move.description.endswith(terms)
    assert merge_roster_moves([], [move]) == [move]
    unrelated = _espn("Signed WR Chris Moore.", PlayerRef("Chris Moore", position="WR"))
    if not move.players:
        assert merge_roster_moves([unrelated], [move]) == [unrelated, move]
    embed = transaction_embeds([move], move.date)[0]
    assert terms in embed.description
    assert embed.url == _move_post("").url


@pytest.mark.parametrize("text", [
    "We could trade WR Chris Moore to the Chicago Bears.",
    "We have not traded WR Chris Moore to the Chicago Bears.",
    "We have agreed to host a trade discussion on tonight's show.",
    "We have received your trade suggestions.",
    "Trade rumors: The Ravens have acquired WR Chris Moore.",
])
def test_trade_discussion_is_not_an_announcement(text: str) -> None:
    assert parse_roster_move(_move_post(text), EASTERN) is None


@pytest.mark.parametrize("text", [
    "We have acquired Diontae Johnson from the Carolina Panthers in exchange for a 2027 fifth-round pick.",
    "We have agreed in principle to a trade with the Carolina Panthers for Diontae Johnson, pending a physical.",
])
def test_a_trade_without_player_codes_is_announced_only_once(tmp_path, text: str) -> None:
    bot, destination = _game_bot(_GameFeed(), tmp_path)
    target = _AnnouncementTarget("1", "channel 1", destination)
    post = _move_post(text)
    moves = merge_roster_moves([], roster_moves_on([post], date(2026, 10, 3), EASTERN))

    for _ in range(2):
        asyncio.run(bot._post_new_roster_news(
            [target], moves, InjuryReport(()), date(2026, 10, 3)
        ))

    assert len(destination.sent) == 1
    assert destination.sent[0][0].url == post.url


def test_only_the_day_s_moves_are_returned() -> None:
    posts = [
        _move_post("We have signed WR Chris Moore to the Practice Squad.", "a"),
        _move_post(
            "We have waived LB Carl Jones Jr.", "b",
            datetime(2026, 10, 2, 20, tzinfo=timezone.utc),
        ),
    ]

    assert [m.transaction_id for m in roster_moves_on(posts, date(2026, 10, 3), EASTERN)] == ["bluesky:a"]


def _espn(description: str, *players: PlayerRef, day: date = date(2026, 10, 3)) -> Transaction:
    return Transaction("espn-1", day, description, players=players)


def test_a_club_post_espn_already_lists_is_left_out() -> None:
    move = parse_roster_move(_move_post("We have placed C Ethan Pocic on Injured Reserve."), EASTERN)
    espn = _espn("Placed C Ethan Pocic on injured reserve.", PlayerRef("Ethan Pocic", position="C"))

    assert merge_roster_moves([espn], [move]) == [espn]
    assert merge_roster_moves([], [move]) == [move]


def test_a_partly_listed_club_post_is_still_added() -> None:
    move = parse_roster_move(
        _move_post("We have placed C Jovaughn Gwyn and C Ethan Pocic on Injured Reserve."), EASTERN
    )
    espn = _espn("Placed C Ethan Pocic on injured reserve.", PlayerRef("Ethan Pocic", position="C"))

    assert merge_roster_moves([espn], [move]) == [espn, move]


def test_espn_s_copy_of_a_posted_club_move_is_not_posted_again(tmp_path) -> None:
    bot, destination = _game_bot(_GameFeed(), tmp_path)
    target = _AnnouncementTarget("1", "channel 1", destination)
    move = parse_roster_move(_move_post("We have placed C Ethan Pocic on Injured Reserve."), EASTERN)
    espn = _espn("Placed C Ethan Pocic on injured reserve.", PlayerRef("Ethan Pocic", position="C"))
    nothing = InjuryReport(())

    asyncio.run(bot._post_new_roster_news([target], [move], nothing, date(2026, 10, 3)))
    asyncio.run(bot._post_new_roster_news([target], [espn], nothing, date(2026, 10, 3)))
    asyncio.run(bot._post_new_roster_news([target], [espn], nothing, date(2026, 10, 3)))

    assert len(destination.sent) == 1
    assert destination.sent[0][0].footer.text.endswith("Baltimore Ravens via Bluesky")
    assert destination.sent[0][0].url == _move_post("").url


def test_two_espn_moves_for_one_player_are_both_posted(tmp_path) -> None:
    bot, destination = _game_bot(_GameFeed(), tmp_path)
    target = _AnnouncementTarget("1", "channel 1", destination)
    moore = PlayerRef("Chris Moore", position="WR")
    released = _espn("Released WR Chris Moore.", moore)
    signed = Transaction("espn-2", date(2026, 10, 3), "Signed WR Chris Moore to the practice squad.", players=(moore,))

    asyncio.run(bot._post_new_roster_news([target], [released], InjuryReport(()), date(2026, 10, 3)))
    asyncio.run(bot._post_new_roster_news([target], [released, signed], InjuryReport(()), date(2026, 10, 3)))

    assert len(destination.sent) == 2
    assert destination.sent[0][0].footer.text.endswith("Data: ESPN")


def test_practice_squad_elevations_are_roster_moves() -> None:
    single = parse_roster_move(
        _move_post("The Ravens activated (standard practice elevation) S K\u2019Von Wallace for tomorrow\u2019s game against Dallas."),
        EASTERN,
    )
    pair = parse_roster_move(
        _move_post("We have activated (standard practice squad elevations) LB Carl Jones and WR Chris Moore for tomorrow\u2019s game."),
        EASTERN,
    )

    assert single.type_text == "Activated"
    assert [p.display_name for p in single.players] == ["S K\u2019Von Wallace"]
    assert [p.display_name for p in pair.players] == ["LB Carl Jones", "WR Chris Moore"]
    assert "standard practice squad elevations" in pair.description


def test_the_club_s_log_copy_of_a_posted_elevation_is_not_posted_again(tmp_path) -> None:
    bot, destination = _game_bot(_GameFeed(), tmp_path)
    target = _AnnouncementTarget("1", "channel 1", destination)
    move = parse_roster_move(
        _move_post("We have activated G Kyle Hergel (standard elevation) from the practice squad."), EASTERN
    )
    logged = Transaction(
        "ravens-official:2026-10-03:abc", date(2026, 10, 3),
        "Activated G Kyle Hergel from the practice squad (standard elevation).",
        players=(PlayerRef("Kyle Hergel", position="G"),),
    )

    asyncio.run(bot._post_new_roster_news([target], [move], InjuryReport(()), date(2026, 10, 3)))
    asyncio.run(bot._post_new_roster_news([target], [logged], InjuryReport(()), date(2026, 10, 3)))

    assert len(destination.sent) == 1
