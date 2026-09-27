"""Reading a game's inactive list from the club's own news page.

ESPN keeps no historical reason for a past game's inactive, and for an older
game it can keep no list at all, while the Ravens publish one in prose before
every kickoff and leave it up afterwards. That post is the last source tried,
behind ESPN's event roster and game summary, because it is a news article
rather than a feed: the wording is what has to be parsed, so a layout change
costs a fallback rather than the answer.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

import aiohttp

from .models import InactivePlayer, InactiveReport, RAVENS_NAME, normalize_name
from .roster_moves import extract_players


LOGGER = logging.getLogger(__name__)
INACTIVES_URL = "https://www.baltimoreravens.com/news/inactives"
# A post is a handful of names per club, so a longer read is the page's own
# navigation or a related-articles rail rather than an inactive list.
MAX_OFFICIAL_INACTIVES = 20
# Headlines date themselves as "Week 3" or by opponent; the published stamp is
# what a date is matched on, so a listing entry without one is skipped.
_DATE_ATTRIBUTES = ("datetime", "data-date", "content")


class OfficialInactivesError(RuntimeError):
    pass


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    return next((value for key, value in attrs if key == name), None)


def _parse_stamp(raw: str | None) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for pattern in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


class _ListingParser(HTMLParser):
    """Article links on the inactives news page, with whatever date they carry."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.articles: list[tuple[str, date | None, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._stamp: date | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = _attribute(attrs, "href") or ""
            if "/news/" in href:
                self._href = href
                self._text = []
                self._stamp = None
                self._depth = 1
                return
        if self._href is None:
            return
        if tag == "a":
            self._depth += 1
        if self._stamp is None:
            for name in _DATE_ATTRIBUTES:
                stamp = _parse_stamp(_attribute(attrs, name))
                if stamp is not None:
                    self._stamp = stamp
                    break

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        headline = " ".join("".join(self._text).split())
        if headline:
            self.articles.append((self._href, self._stamp, headline))
        self._href = None
        self._text = []
        self._stamp = None


class _ArticleParser(HTMLParser):
    """The prose of an article, paragraph and list item at a time."""

    _BLOCKS = frozenset({"p", "li", "h2", "h3"})
    _SKIPPED = frozenset({"script", "style", "nav", "footer"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.stamp: date | None = None
        self._text: list[str] | None = None
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED:
            self._skipping += 1
            return
        if self.stamp is None:
            for name in _DATE_ATTRIBUTES:
                stamp = _parse_stamp(_attribute(attrs, name))
                if stamp is not None:
                    self.stamp = stamp
                    break
        if tag in self._BLOCKS:
            self._flush()
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._text is not None and not self._skipping:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED:
            self._skipping = max(0, self._skipping - 1)
            return
        if tag in self._BLOCKS:
            self._flush()

    def _flush(self) -> None:
        if self._text is None:
            return
        text = " ".join("".join(self._text).split())
        if text:
            self.blocks.append(text)
        self._text = None

    def close(self) -> None:  # pragma: no cover - mirrors HTMLParser's contract
        super().close()
        self._flush()


def _week_label(week: str | None) -> str | None:
    match = re.search(r"week\s*#?\s*(\d{1,2})", (week or "").casefold())
    return f"week {int(match.group(1))}" if match else None


def article_matches(
    headline: str, stamp: date | None, target_date: date, week: str | None
) -> bool:
    """Whether a listing entry is the post for the game being asked about."""
    if stamp is not None:
        # A post goes up on game day; a day either side covers a late night
        # kickoff and the club's own time zone.
        return abs((stamp - target_date).days) <= 1
    label = _week_label(week)
    return bool(label) and label in headline.casefold()


def parse_inactive_names(page: str) -> tuple[InactivePlayer, ...]:
    """Every player named as inactive in an article, in the order written."""
    parser = _ArticleParser()
    parser.feed(page)
    parser.close()
    players: list[InactivePlayer] = []
    seen: set[str] = set()
    for block in parser.blocks:
        if "inactive" not in block.casefold() and not players:
            # The names only count once the article has said what the list is,
            # so a lead paragraph about the matchup contributes nobody.
            continue
        for player in extract_players(block):
            key = normalize_name(player.name)
            if not key or key in seen:
                continue
            seen.add(key)
            players.append(
                InactivePlayer(
                    name=player.name,
                    team=RAVENS_NAME,
                    reason=None,
                    position=player.position,
                    is_ravens=True,
                )
            )
    if len(players) > MAX_OFFICIAL_INACTIVES:
        return ()
    return tuple(players)


def merge_official_inactives(
    report: InactiveReport, players: tuple[InactivePlayer, ...]
) -> InactiveReport:
    """Add club-published names the report does not already carry."""
    if not players:
        return report
    seen = {normalize_name(player.name) for player in report.players}
    merged = list(report.players)
    for player in players:
        key = normalize_name(player.name)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(player)
    return InactiveReport(game=report.game, players=tuple(merged))


class OfficialInactivesClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _page(self, url: str) -> str:
        try:
            async with self._session.get(
                url, headers={"User-Agent": "The-Castle Ravens Discord bot"}
            ) as response:
                response.raise_for_status()
                return await response.text()
        except (aiohttp.ClientError, UnicodeError) as exc:
            raise OfficialInactivesError(
                "The official Ravens inactives page could not be fetched."
            ) from exc

    async def fetch_inactives(
        self, target_date: date, week: str | None = None
    ) -> tuple[InactivePlayer, ...]:
        """The Ravens' own inactive list for a game day, or nothing found."""
        listing = _ListingParser()
        listing.feed(await self._page(INACTIVES_URL))
        listing.close()
        for href, stamp, headline in listing.articles:
            if not article_matches(headline, stamp, target_date, week):
                continue
            page = await self._page(urljoin(INACTIVES_URL, href))
            players = parse_inactive_names(page)
            if players:
                return players
        return ()
