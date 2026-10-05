from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ravens_bot.bluesky import (
    BlueskyClient,
    BlueskyError,
    BlueskyPost,
    GraphicInjuryTable,
    INJURY_POST,
    NameMatcher,
    game_injuries,
    parse_feed,
    parse_game_injury,
    parse_inactives_graphic,
    parse_injury_graphic,
    posts_on,
)
from ravens_bot.bot import RavensBot, _AnnouncementTarget
from ravens_bot.embeds import game_injury_embed, official_injury_embed
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
    PlayerRef,
    TeamRef,
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
