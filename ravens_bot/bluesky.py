"""Reading the Ravens' injury and inactive graphics from their Bluesky mirror.

@ravensbot.bsky.social reposts the club's X account, where the practice report,
the Friday game statuses, and the game day inactives go up as graphics minutes
before the website or ESPN carries them. Bluesky's public API needs no key, so
the feed is read directly and each graphic is run through OCR once.

OCR on these graphics reads the words reliably but not the gaps between them
("TE-MARKANDREWS"), and picks up stray marks from the layout, so every line is
matched against the roster by its letters alone. The graphic only covers the
Ravens; the opponent still comes from the official report and ESPN.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime
from statistics import median
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

import aiohttp

from .espn_urls import HEADSHOT_FEATURE_WIDTH, headshot_url
from .models import (
    RAVENS,
    RAVENS_NAME,
    InactivePlayer,
    PlayerRef,
    Transaction,
    normalize_name,
)
from .roster_moves import extract_named_players, extract_players, transaction_action
from .trades import parse_trade


LOGGER = logging.getLogger(__name__)
BLUESKY_HANDLE = "ravensbot.bsky.social"
BLUESKY_FEED_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
BLUESKY_PROFILE_URL = "https://bsky.app/profile/{handle}/post/{rkey}"
# The API's largest page. A game day alone can run to dozens of posts.
FEED_LIMIT = 100
# Polled every half minute during a game, so only the latest posts are needed;
# the club posts a dozen or so during a quarter at most.
GAME_FEED_LIMIT = 40
# Each graphic is read once; a handful covers a week's reports and a game day.
OCR_CACHE_SIZE = 16
MAX_IMAGE_BYTES = 10 * 1024 * 1024
INJURY_POST = re.compile(
    r"injury report|game status|practice estimation", re.IGNORECASE
)
INACTIVES_POST = re.compile(r"\binactives\b", re.IGNORECASE)
BLANK_CELL = "-"
# How the website writes a player without a game designation.
BLANK_STATUS = "(-)"

POSITIONS = frozenset(
    {
        "QB", "RB", "FB", "WR", "TE", "T", "G", "C", "OT", "OG", "OL", "DL",
        "DE", "DT", "NT", "LB", "ILB", "OLB", "MLB", "CB", "S", "SAF", "FS",
        "SS", "DB", "K", "P", "LS", "LT", "RT", "LG", "RG", "OC", "EDGE",
    }
)
# Positions the club sometimes writes out instead of abbreviating.
_POSITION_WORDS = {
    "quarterback": "QB", "running back": "RB", "fullback": "FB",
    "wide receiver": "WR", "tight end": "TE", "left tackle": "LT",
    "right tackle": "RT", "tackle": "T", "left guard": "LG", "right guard": "RG",
    "guard": "G", "center": "C", "defensive tackle": "DT", "defensive end": "DE",
    "nose tackle": "NT", "outside linebacker": "OLB", "inside linebacker": "ILB",
    "linebacker": "LB", "cornerback": "CB", "safety": "S", "kicker": "K",
    "punter": "P", "long snapper": "LS",
}
_POSITION_WORD = re.compile(
    r"^(?P<word>"
    + "|".join(sorted(map(re.escape, _POSITION_WORDS), key=len, reverse=True))
    + r")\s+(?=[A-Z])",
    re.IGNORECASE,
)
_SUFFIXES = ("JR", "SR", "II", "III", "IV", "V")
_DAYS = {
    "MONDAY": "Mon",
    "TUESDAY": "Tue",
    "WEDNESDAY": "Wed",
    "THURSDAY": "Thu",
    "FRIDAY": "Fri",
    "SATURDAY": "Sat",
    "SUNDAY": "Sun",
}
_PRACTICE = {"FULL": "FP", "FP": "FP", "LIMITED": "LP", "LP": "LP", "DNP": "DNP"}
_STATUSES = ("QUESTIONABLE", "DOUBTFUL", "OUT")
_ACRONYMS = frozenset({"NIR", "ACL", "MCL", "PUP", "NFI", "IR"})
# Shorter keys match too freely inside a line of run-together capitals.
_MIN_KEY_LENGTH = 5
_FUZZY_RATIO = 0.88

OcrResult = Sequence[tuple[Any, str, float]]
Recognizer = Callable[[bytes], OcrResult]


class BlueskyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlueskyPost:
    uri: str
    text: str
    created_at: datetime
    image_urls: tuple[str, ...]
    handle: str = BLUESKY_HANDLE

    @property
    def url(self) -> str:
        return BLUESKY_PROFILE_URL.format(
            handle=self.handle, rkey=self.uri.rsplit("/", 1)[-1]
        )


@dataclass(frozen=True)
class GraphicInjuryTable:
    """The Ravens' side of an injury report, as read from a graphic."""

    week: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    headshots: tuple[str | None, ...]
    post_url: str | None = None


@dataclass(frozen=True)
class _Cell:
    text: str
    x: float
    y: float
    height: float


def compact(value: str) -> str:
    """Only the letters, upper case, which is all OCR reliably keeps."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^A-Z]", "", stripped.upper())


class NameMatcher:
    """Finds a roster player inside a run-together OCR line."""

    def __init__(self, players: Iterable[PlayerRef]) -> None:
        self._keys: list[tuple[str, PlayerRef]] = []
        seen: set[str] = set()
        for player in players:
            for key in _name_keys(player.name):
                if len(key) >= _MIN_KEY_LENGTH and key not in seen:
                    seen.add(key)
                    self._keys.append((key, player))
        # Longest first, so "Chris Moore" is not answered by a shorter name.
        self._keys.sort(key=lambda item: len(item[0]), reverse=True)

    def find(self, text: str) -> PlayerRef | None:
        line = compact(text)
        if not line:
            return None
        for key, player in self._keys:
            if key in line:
                return player
        best: tuple[float, PlayerRef | None] = (0.0, None)
        for key, player in self._keys:
            tail = line[-len(key):] if len(line) > len(key) else line
            ratio = difflib.SequenceMatcher(None, key, tail).ratio()
            if ratio > best[0]:
                best = (ratio, player)
        return best[1] if best[0] >= _FUZZY_RATIO else None


def _name_keys(name: str) -> tuple[str, ...]:
    words = [compact(word) for word in name.split()]
    words = [word for word in words if word]
    full = "".join(words)
    if len(words) > 2 and words[-1] in _SUFFIXES:
        return (full, "".join(words[:-1]))
    return (full,)


def _cells(results: OcrResult) -> list[_Cell]:
    cells: list[_Cell] = []
    for box, text, _confidence in results:
        points = [(float(x), float(y)) for x, y in box]
        if not points or not str(text).strip():
            continue
        xs = [x for x, _ in points]
        ys = [y for _, y in points]
        cells.append(
            _Cell(
                text=str(text).strip(),
                x=sum(xs) / len(xs),
                y=sum(ys) / len(ys),
                height=max(ys) - min(ys),
            )
        )
    return cells


def _rows(cells: list[_Cell]) -> list[list[_Cell]]:
    """Cells grouped into lines of the graphic, top to bottom, left to right."""
    if not cells:
        return []
    tolerance = max(4.0, median(cell.height for cell in cells) * 0.6)
    rows: list[list[_Cell]] = []
    for cell in sorted(cells, key=lambda cell: cell.y):
        if rows and cell.y - rows[-1][0].y <= tolerance:
            rows[-1].append(cell)
        else:
            rows.append([cell])
    return [sorted(row, key=lambda cell: cell.x) for row in rows]


def _week(rows: list[list[_Cell]]) -> str | None:
    for row in rows:
        match = re.search(r"WEEK\s*(\d{1,2})", " ".join(c.text for c in row).upper())
        if match:
            return f"WEEK {int(match.group(1))}"
    return None


def _split_position(text: str) -> tuple[str | None, str]:
    """A leading position code, and the rest of the text."""
    match = re.match(r"\s*([A-Za-z]{1,3})\s*[-|:]\s*(.*)$", text)
    if match and match.group(1).upper() in POSITIONS:
        return match.group(1).upper(), match.group(2)
    match = re.match(r"\s*([A-Za-z]{1,3})(?![A-Za-z])\W*(.*)$", text)
    if match and match.group(1).upper() in POSITIONS:
        return match.group(1).upper(), match.group(2)
    return None, text


def _title(text: str) -> str:
    """Graphic capitals in the website's case: "HAMSTRING/NIR-REST" reads
    "Hamstring/NIR - Rest"."""
    words = re.sub(r"\s*-\s*", " - ", text).split()
    return " ".join(
        "/".join(
            part.upper() if part.upper() in _ACRONYMS else part.capitalize()
            for part in word.split("/")
        )
        for word in words
    )


def _resolve(
    matcher: NameMatcher, text: str
) -> tuple[str, str | None, str | None]:
    """Name, position, and athlete id for a player cell."""
    position, rest = _split_position(text)
    player = matcher.find(text)
    if player is not None:
        return player.name, position or player.position, player.athlete_id
    return _title(rest), position, None


def parse_injury_graphic(
    results: OcrResult, players: Iterable[PlayerRef]
) -> GraphicInjuryTable | None:
    """The practice report table on a Ravens injury graphic, if there is one."""
    rows = _rows(_cells(results))
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if {"PLAYER", "INJURY"} <= {compact(cell.text) for cell in row}
        ),
        None,
    )
    if header_index is None:
        return None
    week = _week(rows[:header_index])
    if week is None:
        return None
    columns: list[tuple[str, float]] = []
    for cell in rows[header_index]:
        label = compact(cell.text)
        if label == "PLAYER":
            columns.append(("Player", cell.x))
        elif label == "INJURY":
            columns.append(("Injury", cell.x))
        elif label in _DAYS:
            columns.append((_DAYS[label], cell.x))
        elif label == "GAMESTATUS":
            columns.append(("Game Status", cell.x))
    names = [name for name, _ in columns]
    if "Player" not in names or not any(day in names for day in _DAYS.values()):
        return None
    days = tuple(name for name in names if name in _DAYS.values())
    headers = ("Player", "Position", "Injury", *days, "Game Status")
    matcher = NameMatcher(players)
    table_rows: list[tuple[str, ...]] = []
    headshots: list[str | None] = []
    for row in rows[header_index + 1:]:
        if any("=" in cell.text for cell in row):
            break
        values: dict[str, str] = {}
        for cell in row:
            column = min(columns, key=lambda item: abs(item[1] - cell.x))[0]
            values[column] = f"{values[column]} {cell.text}" if column in values else cell.text
        player_text = values.get("Player", "")
        if not compact(player_text):
            continue
        name, position, athlete_id = _resolve(matcher, player_text)
        status = compact(values.get("Game Status", ""))
        table_rows.append(
            (
                name,
                position or BLANK_CELL,
                _title(values["Injury"]) if values.get("Injury") else BLANK_CELL,
                *(
                    _PRACTICE.get(compact(values.get(day, "")), BLANK_CELL)
                    for day in days
                ),
                next((s for s in _STATUSES if s == status), BLANK_STATUS),
            )
        )
        headshots.append(headshot_url(athlete_id, HEADSHOT_FEATURE_WIDTH))
    if not table_rows:
        return None
    return GraphicInjuryTable(
        week=week,
        headers=headers,
        rows=tuple(table_rows),
        headshots=tuple(headshots),
    )


def parse_inactives_graphic(
    results: OcrResult, players: Iterable[PlayerRef]
) -> tuple[InactivePlayer, ...]:
    """Every Ravens player named on an inactives graphic, top to bottom."""
    rows = _rows(_cells(results))
    start = next(
        (
            index
            for index, row in enumerate(rows)
            if any("INACTIVE" in compact(cell.text) for cell in row)
        ),
        None,
    )
    if start is None:
        return ()
    matcher = NameMatcher(players)
    found: list[InactivePlayer] = []
    seen: set[str] = set()
    for row in rows[start + 1:]:
        text = " ".join(cell.text for cell in row)
        position, rest = _split_position(row[0].text)
        player = matcher.find(text)
        if player is not None:
            name, athlete_id = player.name, player.athlete_id
            position = position or player.position
        elif position is not None:
            # Someone the roster does not know yet, such as a practice squad
            # call-up; only a line that opens with a position is trusted.
            remainder = " ".join([rest, *(cell.text for cell in row[1:])]).strip()
            words = [
                w
                for w in re.split(r"[^A-Za-z.'-]+", remainder)
                if len(w) > 1 and w.upper() not in POSITIONS
            ]
            if len(words) < 2:
                continue
            name, athlete_id = _title(" ".join(words[:3])), None
        else:
            continue
        key = compact(name)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            InactivePlayer(
                name=name,
                team=RAVENS_NAME,
                reason=None,
                athlete_id=athlete_id,
                position=position,
                is_ravens=True,
            )
        )
    return tuple(found)


def parse_feed(payload: dict[str, Any], handle: str = BLUESKY_HANDLE) -> list[BlueskyPost]:
    """The account's own image posts, newest first, without reposts."""
    posts: list[BlueskyPost] = []
    for item in payload.get("feed") or []:
        if not isinstance(item, dict) or item.get("reason"):
            continue
        post = item.get("post") or {}
        author = (post.get("author") or {}).get("handle")
        if author and author != handle:
            continue
        record = post.get("record") or {}
        try:
            created = datetime.fromisoformat(
                str(record.get("createdAt", "")).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        embed = post.get("embed") or {}
        images = embed.get("images") or (embed.get("media") or {}).get("images") or []
        urls = tuple(
            image["fullsize"]
            for image in images
            if isinstance(image, dict) and isinstance(image.get("fullsize"), str)
        )
        posts.append(
            BlueskyPost(
                uri=str(post.get("uri", "")),
                text=str(record.get("text", "")),
                created_at=created,
                image_urls=urls,
                handle=handle,
            )
        )
    posts.sort(key=lambda post: post.created_at, reverse=True)
    return posts


def posts_on(
    posts: Iterable[BlueskyPost], pattern: re.Pattern[str], day: date, time_zone: ZoneInfo
) -> list[BlueskyPost]:
    return [
        post
        for post in posts
        if post.image_urls
        and pattern.search(post.text)
        and post.created_at.astimezone(time_zone).date() == day
    ]


@dataclass(frozen=True)
class GameInjuryUpdate:
    """One in-game injury line the club posted, such as "is questionable to return"."""

    name: str
    position: str | None
    injury: str | None
    status: str
    text: str
    post: BlueskyPost


# The club's in-game wording, most specific first; each maps to a short status.
_GAME_STATUSES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"\b(?:has been |is )?ruled out\b", "Out"),
        (r"\bwill not return\b|\bwon'?t return\b|\bout for the (?:rest of the )?game\b", "Out"),
        (r"\bnot expected to return\b", "Not expected to return"),
        (r"\bdoubtful to return\b", "Doubtful to return"),
        (r"\bquestionable to return\b", "Questionable to return"),
        (r"\bbeing evaluated\b", "Being evaluated"),
        (r"\bhas (?:now )?(?:returned|been cleared)(?: to (?:the )?game)?\s*[.!]*$|\bis back in the game\b|\bis cleared to return\b", "Returned"),
    )
)
_GAME_INJURY = re.compile(
    r"^\s*(?:(?P<position>[A-Z]{1,4})\s+)?"
    r"(?P<name>[A-Z][\w.'’-]+(?:\s+[A-Z][\w.'’-]+){0,3})"
    r"\s*(?:\((?P<injury>[^)]{1,40})\))?"
    r"\s+(?:is|has|will|was)\b"
)


def parse_game_injury(post: BlueskyPost) -> GameInjuryUpdate | None:
    """An in-game injury update, when that is all a post says.

    A follow-up can name the player by surname alone ("Hamilton has now
    returned to the game."); ``game_injuries`` resolves that against the
    game's earlier lines.
    """
    text = " ".join(post.text.split())
    if len(text) > 200:
        return None
    position: str | None = None
    body = text
    spelled = _POSITION_WORD.match(text)
    if spelled is not None:
        position = _POSITION_WORDS[spelled.group("word").lower()]
        body = text[spelled.end():]
    match = _GAME_INJURY.match(body)
    if match is None:
        return None
    if match.group("position") is not None:
        if position is not None or match.group("position") not in POSITIONS:
            return None
        position = match.group("position")
    status = next(
        (label for pattern, label in _GAME_STATUSES if pattern.search(body[match.end("name"):])),
        None,
    )
    if status is None:
        return None
    injury = match.group("injury")
    return GameInjuryUpdate(
        name=match.group("name"),
        position=position,
        injury=injury.strip().capitalize() if injury else None,
        status=status,
        text=text,
        post=post,
    )


def game_injuries(
    posts: Iterable[BlueskyPost], since: datetime
) -> list[GameInjuryUpdate]:
    """In-game injury updates posted since ``since``, oldest first.

    A line naming only a surname takes the player from the game's earlier
    line about them, and is dropped when there is none to say who it means.
    """
    parsed = sorted(
        (
            update
            for post in posts
            if post.created_at >= since
            for update in (parse_game_injury(post),)
            if update is not None
        ),
        key=lambda update: update.post.created_at,
    )
    updates: list[GameInjuryUpdate] = []
    for update in parsed:
        if " " not in update.name:
            earlier = next(
                (
                    previous
                    for previous in reversed(updates)
                    if previous.name.split()[-1].lower() == update.name.lower()
                ),
                None,
            )
            if earlier is None:
                continue
            update = replace(
                update,
                name=earlier.name,
                position=update.position or earlier.position,
                injury=update.injury or earlier.injury,
            )
        updates.append(update)
    return updates


ROSTER_MOVE_ID_PREFIX = "bluesky:"
_ROSTER_VERBS = (
    "signed", "re-signed", "placed", "waived", "released", "activated",
    "elevated", "claimed", "traded", "acquired", "received", "designated", "reinstated",
    "promoted", "terminated", "restored", "added",
)
_TRADE_AGREEMENT = (
    r"agreed(?:\s+in\s+principle)?(?:\s+to\s+terms)?\s+(?:on|to)\s+"
    r"(?:a\s+trade|trade|acquire)\b"
)
# "We have placed …", "We have also activated …", "The Ravens activated …":
# the club's voice, which the move log writes as a bare verb.
_CLUB_VOICE = re.compile(
    r"\b(?:We(?:\s+have|['’]ve)?|The\s+Ravens(?:\s+have)?)\s+(?:also\s+)?"
    rf"(?P<verb>{'|'.join(_ROSTER_VERBS)}|{_TRADE_AGREEMENT})\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+")
_TRADE_ASSET = re.compile(r"\b(?:acquired?|traded?|received?|for)\s+", re.IGNORECASE)


def _trade_post_players(description: str) -> tuple[PlayerRef, ...]:
    """Recover names without codes so later move-log copies still deduplicate."""
    players = list(extract_players(description))
    seen = {normalize_name(player.name) for player in players}
    for match in _TRADE_ASSET.finditer(description):
        asset = description[match.end():]
        position = _POSITION_WORD.match(asset)
        if position is not None:
            code = _POSITION_WORDS[position.group("word").lower()]
            candidates = extract_players(f"{code} {asset[position.end():]}")
        elif asset.split(" ", 1)[0].removesuffix("s") in POSITIONS:
            continue
        else:
            candidates = extract_named_players(asset)
        for player in candidates:
            key = normalize_name(player.name)
            if key not in seen:
                seen.add(key)
                players.append(player)
    return tuple(players)


def parse_roster_move(post: BlueskyPost, time_zone: ZoneInfo) -> Transaction | None:
    """A roster move the club announced, written the way its move log reads.

    "We have placed C A on Injured Reserve. We have also activated G B …"
    becomes "Placed C A on Injured Reserve. Activated G B …", so the move is
    laid out and illustrated like any other.
    """
    text = " ".join(_URL.sub("", post.text).split())
    opening = _CLUB_VOICE.match(text)
    if opening is None:
        return None
    description = _CLUB_VOICE.sub(
        lambda match: match.group("verb").capitalize(), text
    ).strip()
    players = extract_players(description)
    agreement = re.match(_TRADE_AGREEMENT, description, re.IGNORECASE) is not None
    trade = parse_trade(description)
    if agreement or trade is not None:
        players = _trade_post_players(description)
    if not players and not agreement and trade is None:
        return None
    return Transaction(
        transaction_id=f"{ROSTER_MOVE_ID_PREFIX}{post.uri.rsplit('/', 1)[-1]}",
        date=post.created_at.astimezone(time_zone).date(),
        description=description,
        type_text="Trade agreement" if agreement else transaction_action(description),
        athlete=players[0].name if players else None,
        players=players,
        team=RAVENS,
        source_url=post.url,
    )


def roster_moves_on(
    posts: Iterable[BlueskyPost], day: date, time_zone: ZoneInfo
) -> list[Transaction]:
    """The club's roster moves announced on ``day``, oldest first."""
    moves = [
        move
        for post in sorted(posts, key=lambda post: post.created_at)
        if post.created_at.astimezone(time_zone).date() == day
        for move in (parse_roster_move(post, time_zone),)
        if move is not None
    ]
    return moves


def is_roster_move_post(transaction: Transaction) -> bool:
    return transaction.transaction_id.startswith(ROSTER_MOVE_ID_PREFIX)


def player_keys(transaction: Transaction) -> frozenset[str]:
    return frozenset(
        key for key in (normalize_name(player.name) for player in transaction.players) if key
    )


def merge_roster_moves(
    transactions: list[Transaction], moves: list[Transaction]
) -> list[Transaction]:
    """Add the club's posts for moves the other feeds do not list yet.

    The club's post and ESPN's entry for the same move are worded differently,
    so they are matched by the players they name on the same day. Once ESPN
    lists every player a post names, ESPN's richer entry is the one kept; the
    bot's announcement state stops it repeating a post already made.
    """
    listed: dict[date, set[str]] = {}
    for item in transactions:
        listed.setdefault(item.date, set()).update(player_keys(item))
    return [
        *transactions,
        *(
            move
            for move in moves
            if not player_keys(move) or not player_keys(move) <= listed.get(move.date, set())
        ),
    ]


_ENGINE: Any = None
_ENGINE_LOCK = threading.Lock()


def rapidocr_recognize(image: bytes) -> OcrResult:
    """OCR with RapidOCR, loading its models on first use."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise BlueskyError("RapidOCR is not installed.") from exc
            _ENGINE = RapidOCR()
        engine = _ENGINE
        result, _elapsed = engine(image)
    return result or []


class BlueskyClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        handle: str = BLUESKY_HANDLE,
        recognize: Recognizer = rapidocr_recognize,
    ) -> None:
        self._session = session
        self._handle = handle
        self._recognize = recognize
        self._ocr_cache: OrderedDict[str, OcrResult] = OrderedDict()

    async def fetch_posts(self, limit: int = FEED_LIMIT) -> list[BlueskyPost]:
        try:
            async with self._session.get(
                BLUESKY_FEED_URL,
                params={
                    "actor": self._handle,
                    "limit": str(limit),
                    "filter": "posts_no_replies",
                },
                headers={"User-Agent": "The-Castle Ravens Discord bot"},
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as exc:
            raise BlueskyError("The Ravens Bluesky feed could not be fetched.") from exc
        if not isinstance(payload, dict):
            raise BlueskyError("The Ravens Bluesky feed was not understood.")
        return parse_feed(payload, self._handle)

    async def read_image(self, url: str) -> OcrResult:
        cached = self._ocr_cache.get(url)
        if cached is not None:
            self._ocr_cache.move_to_end(url)
            return cached
        try:
            async with self._session.get(url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise BlueskyError(
                            "A Ravens Bluesky graphic was too large to read."
                        )
        except aiohttp.ClientError as exc:
            raise BlueskyError("A Ravens Bluesky graphic could not be fetched.") from exc
        try:
            result = await asyncio.to_thread(self._recognize, bytes(data))
        except BlueskyError:
            raise
        except Exception as exc:  # OCR failures must not stop the poll
            raise BlueskyError(f"A Ravens Bluesky graphic could not be read: {exc}") from exc
        self._ocr_cache[url] = result
        while len(self._ocr_cache) > OCR_CACHE_SIZE:
            self._ocr_cache.popitem(last=False)
        return result

    async def fetch_injury_table(
        self, day: date, time_zone: ZoneInfo, players: Iterable[PlayerRef]
    ) -> GraphicInjuryTable | None:
        """The newest injury graphic posted on ``day``, read into a table."""
        players = tuple(players)
        for post in posts_on(await self.fetch_posts(), INJURY_POST, day, time_zone):
            for url in post.image_urls:
                table = parse_injury_graphic(await self.read_image(url), players)
                if table is not None:
                    return GraphicInjuryTable(
                        week=table.week,
                        headers=table.headers,
                        rows=table.rows,
                        headshots=table.headshots,
                        post_url=post.url,
                    )
        return None

    async def fetch_inactives(
        self, day: date, time_zone: ZoneInfo, players: Iterable[PlayerRef]
    ) -> tuple[InactivePlayer, ...]:
        """The Ravens inactives graphic posted on ``day``, or nobody."""
        players = tuple(players)
        for post in posts_on(await self.fetch_posts(), INACTIVES_POST, day, time_zone):
            for url in post.image_urls:
                found = parse_inactives_graphic(await self.read_image(url), players)
                if found:
                    return found
        return ()

    async def fetch_game_injuries(self, since: datetime) -> list[GameInjuryUpdate]:
        """The club's in-game injury lines posted since kickoff, oldest first."""
        return game_injuries(await self.fetch_posts(GAME_FEED_LIMIT), since)

    async def fetch_roster_moves(
        self, day: date, time_zone: ZoneInfo
    ) -> list[Transaction]:
        """The roster moves the club announced on ``day``."""
        return roster_moves_on(await self.fetch_posts(), day, time_zone)
