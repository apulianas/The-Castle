"""Drafttek inventory and chart-equivalent trades, not predictions of accepted deals."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
from itertools import combinations
from typing import Literal

import aiohttp

from .cache import AsyncTtlCache


DRAFTTEK_URL = "https://www.drafttek.com/nfl-trade-value-chart.asp"
RICH_HILL_URL = "https://www.drafttek.com/NFL-Trade-Value-Chart-Rich-Hill.asp"
CHART_NAMES = {"jj": "Jimmy Johnson", "rich_hill": "Rich Hill"}
Chart = Literal["jj", "rich_hill"]
DRAFT_TTL_SECONDS = 3600
MAX_PACKAGE_PICKS = 5
MAX_PICK_NUMBER = 300


class DraftError(RuntimeError):
    pass


@dataclass(frozen=True)
class DraftPick:
    year: int
    round: int
    number: int | None
    team: str
    value: Decimal
    quartile: int | None = None

    @property
    def future(self) -> bool:
        return self.number is None

    @property
    def label(self) -> str:
        if self.future:
            return f"{self.year} R{self.round} (hypothetical)"
        return f"{self.year} R{self.round} #{self.number}"


@dataclass(frozen=True)
class DraftSnapshot:
    year: int
    picks: tuple[DraftPick, ...]
    future_picks: tuple[DraftPick, ...]
    fetched_at: datetime
    updated: str | None
    chart: Chart = "jj"
    future_available: bool = True

    @property
    def ravens(self) -> tuple[DraftPick, ...]:
        return tuple(pick for pick in self.picks if pick.team == "BAL")

    @property
    def ravens_future(self) -> tuple[DraftPick, ...]:
        return tuple(pick for pick in self.future_picks if pick.team == "BAL")


@dataclass(frozen=True)
class TradePackage:
    picks: tuple[DraftPick, ...]

    @property
    def value(self) -> Decimal:
        return sum((pick.value for pick in self.picks), Decimal(0))


@dataclass(frozen=True)
class TradePlan:
    target: DraftPick
    current: tuple[TradePackage, ...] = ()
    future: tuple[TradePackage, ...] = ()
    strongest: TradePackage | None = None


def _array(page: str, name: str, *, optional: bool = False) -> list[dict[str, object]]:
    match = re.search(rf"window\.{name}\s*=\s*(\[.*?\])\s*;", page, re.S)
    if match is None:
        if optional and not re.search(rf"window\.{name}\s*=", page):
            return []
        raise DraftError(f"Drafttek's {name} inventory is missing or unreadable.")
    # Drafttek publishes literal JS objects with unquoted keys, not executable data.
    text = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', match[1])
    try:
        rows = json.loads(text, parse_float=Decimal)
    except (ValueError, InvalidOperation) as exc:
        raise DraftError(f"Drafttek's {name} inventory could not be decoded.") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DraftError(f"Drafttek's {name} inventory has an unexpected format.")
    return rows


def _integer(row: dict[str, object], key: str, low: int, high: int) -> int:
    value = row.get(key)
    if type(value) is not int or not low <= value <= high:
        raise DraftError(f"Drafttek supplied an invalid {key}.")
    return value


def _value(raw: object) -> Decimal:
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise DraftError("Drafttek supplied an invalid point value.") from exc
    if not value.is_finite() or not 0 < value <= 10000:
        raise DraftError("Drafttek supplied an invalid point value.")
    return value


def _team(row: dict[str, object]) -> str:
    team = row.get("team")
    if not isinstance(team, str) or not re.fullmatch(r"[A-Z]{2,3}", team):
        raise DraftError("Drafttek supplied an invalid pick owner.")
    return team


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def parse_drafttek(page: str, fetched_at: datetime) -> DraftSnapshot:
    title = re.search(r"<title\b[^>]*>(.*?)</title>", page, re.I | re.S)
    year_match = re.search(r"\b(20\d{2})\b", title[1]) if title else None
    if year_match is None:
        raise DraftError("Drafttek's draft year is missing.")
    year = int(year_match[1])
    if not fetched_at.year <= year <= fetched_at.year + 1:
        raise DraftError(f"Drafttek is showing the {year} draft, not a current draft inventory.")
    picks = tuple(sorted((
        DraftPick(
            year, _integer(row, "round", 1, 7),
            _integer(row, "pick", 1, MAX_PICK_NUMBER), _team(row),
            _value(row.get("value")),
        )
        for row in _array(page, "DT_TRADE_PICKS")
    ), key=lambda pick: pick.number or 0))
    numbers = [pick.number for pick in picks]
    if (
        not 224 <= len(picks) <= MAX_PICK_NUMBER
        or numbers != list(range(1, len(picks) + 1))
        or {pick.round for pick in picks} != set(range(1, 8))
        or [pick.round for pick in picks] != sorted(pick.round for pick in picks)
        or len([pick for pick in picks if pick.team == "BAL"]) > 32
    ):
        raise DraftError("Drafttek's draft inventory is incomplete or inconsistent.")
    future_rows = _array(page, "DT_FUTURE_PICKS", optional=True)
    future = tuple(
        DraftPick(
            _integer(row, "year", year + 1, year + 1),
            _integer(row, "round", 1, 2), None, _team(row),
            _value(row.get("value")), _integer(row, "quartile", 1, 4),
        )
        for row in future_rows
    )
    identities = {(pick.year, pick.round, pick.team) for pick in future}
    if len(identities) != len(future):
        raise DraftError("Drafttek's future estimates contain duplicate picks.")
    updated = re.search(r'Most Recent Update:\s*(?:<strong>)?(.*?)(?:</strong>|</div>)', page, re.I | re.S)
    return DraftSnapshot(
        year, picks, future, fetched_at, _text(updated[1]) if updated else None,
        future_available=len([pick for pick in future if pick.team == "BAL"]) == 2,
    )


class _RichHillParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[int, Decimal] = {}
        self._cell: list[str] | None = None
        self._is_pick = False
        self._is_value = False
        self._number: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "td":
            self._cell = []
            self._is_pick = bool(
                {"TradeValueDataA", "TradeValueDataB"} & set((attributes.get("class") or "").split())
            )
            self._is_value = False
        if self._cell is not None and attributes.get("id") == "ConsolidatedTradeValue":
            self._is_value = True

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "td" or self._cell is None:
            return
        text = "".join(self._cell).strip()
        if self._is_pick:
            match = re.match(r"(\d+)\b", text)
            self._number = int(match[1]) if match else None
        elif self._is_value and (text or self._number is not None):
            if self._number is None or self._number in self.values:
                raise DraftError("Drafttek's Rich Hill chart contains an unmatched or duplicate value.")
            self.values[self._number] = _value(text)
            self._number = None
        else:
            self._number = None
        self._cell = None


def with_rich_hill(snapshot: DraftSnapshot, page: str) -> DraftSnapshot:
    parser = _RichHillParser()
    parser.feed(page)
    picks: list[DraftPick] = []
    for pick in snapshot.picks:
        if pick.number is None or pick.number not in parser.values:
            raise DraftError("Drafttek's Rich Hill chart does not cover the current pick inventory.")
        picks.append(replace(pick, value=parser.values[pick.number]))
    future: list[DraftPick] = []
    for pick in snapshot.future_picks:
        slots = [item for item in picks if item.round == pick.round + 1][:32]
        if len(slots) != 32 or pick.quartile is None:
            raise DraftError("Drafttek's Rich Hill future estimates could not be calculated.")
        midpoint = (pick.quartile - 1) * 8 + 3
        value = (slots[midpoint].value + slots[midpoint + 1].value) / 2
        future.append(replace(pick, value=value))
    return replace(snapshot, picks=tuple(picks), future_picks=tuple(future), chart="rich_hill")


def plan_trade(snapshot: DraftSnapshot, number: int) -> TradePlan:
    target = next((pick for pick in snapshot.picks if pick.number == number), None)
    if target is None:
        raise ValueError(f"Pick #{number} is not listed in Drafttek's {snapshot.year} inventory (1-{len(snapshot.picks)}).")
    if target.team == "BAL":
        return TradePlan(target)
    inventory = snapshot.ravens + snapshot.ravens_future
    current: list[TradePackage] = []
    future: list[TradePackage] = []

    def rank(package: TradePackage) -> tuple:
        return (
            package.value - target.value, len(package.picks),
            sum(pick.future for pick in package.picks),
            tuple((pick.year, pick.round, pick.number or 0) for pick in package.picks),
        )

    for count in range(1, min(MAX_PACKAGE_PICKS, len(inventory)) + 1):
        for picks in combinations(inventory, count):
            package = TradePackage(picks)
            if package.value < target.value:
                continue
            candidates = future if any(pick.future for pick in picks) else current
            candidates.append(package)
            candidates.sort(key=rank)
            del candidates[2:]
    strongest = TradePackage(tuple(sorted(
        inventory, key=lambda pick: pick.value, reverse=True,
    )[:MAX_PACKAGE_PICKS]))
    return TradePlan(target, tuple(current), tuple(future[:1]), strongest)


class DraftClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._pages: AsyncTtlCache[str, tuple[str, datetime]] = AsyncTtlCache(DRAFT_TTL_SECONDS)

    async def _page(self, url: str) -> tuple[str, datetime]:
        async def fetch() -> tuple[str, datetime]:
            try:
                async with self._session.get(
                    url, headers={"User-Agent": "The-Castle Ravens Discord bot"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    response.raise_for_status()
                    return await response.text(), datetime.now(timezone.utc)
            except (aiohttp.ClientError, TimeoutError, UnicodeError) as exc:
                raise DraftError("Drafttek could not be fetched. Please try again later.") from exc
        return await self._pages.get_or_fetch(url, fetch)

    async def fetch(self, chart: Chart = "jj") -> DraftSnapshot:
        if chart not in CHART_NAMES:
            raise ValueError("Choose the Jimmy Johnson or Rich Hill chart.")

        page, fetched_at = await self._page(DRAFTTEK_URL)
        try:
            snapshot = parse_drafttek(page, fetched_at)
            if chart == "rich_hill":
                rich_page, _ = await self._page(RICH_HILL_URL)
                snapshot = with_rich_hill(snapshot, rich_page)
            return snapshot
        except DraftError:
            self._pages.invalidate(DRAFTTEK_URL)
            self._pages.invalidate(RICH_HILL_URL)
            raise

    async def trade(self, snapshot: DraftSnapshot, number: int) -> TradePlan:
        return await asyncio.to_thread(plan_trade, snapshot, number)
