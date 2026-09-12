from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from html.parser import HTMLParser

import aiohttp

from .models import RAVENS, Transaction, normalize_name
from .roster_moves import extract_players, transaction_action


TRANSACTIONS_URL = "https://www.baltimoreravens.com/team/transactions/{year}"


def transaction_log_url(year: int) -> str:
    """The club's own move log for a year, which a roster move post is read from.

    A post about a Ravens move belongs to the page the club publishes rather
    than to a third party's copy of it, so the title points back at the source.
    """
    return TRANSACTIONS_URL.format(year=year)
_STANDARD_ELEVATION = "standard elevation"
_ACTION_BOUNDARY_RE = re.compile(
    r"\.\s+(?=(?:Activated|Added|Claimed|Designated|Elevated|Placed|Promoted|"
    r"Re-signed|Released|Signed|Traded|Waived)\b)"
)


class OfficialTransactionsError(RuntimeError):
    pass


def _classes(attributes: list[tuple[str, str | None]]) -> set[str]:
    value = next((value for name, value in attributes if name == "class"), None)
    return set((value or "").split())


class _TransactionsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str]] = []
        self._date = ""
        self._cell: str | None = None
        self._text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "td":
            return
        self._cell = (
            "date" if "nfl-c-transactions-report__date" in _classes(attrs) else "move"
        )
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "td" or self._cell is None:
            return
        text = " ".join("".join(self._text).split())
        if self._cell == "date":
            self._date = text
        elif self._date and text:
            self.rows.append((self._date, text))
        self._cell = None
        self._text = []


def _elevation_descriptions(description: str) -> tuple[str, ...]:
    parts = _ACTION_BOUNDARY_RE.split(description)
    return tuple(
        f"{part.strip().rstrip('.')}."
        for part in parts
        if _STANDARD_ELEVATION in normalize_name(part)
    )


def parse_standard_elevations(page: str, target_date: date) -> list[Transaction]:
    """Read game-day practice-squad elevations from the official transaction log."""
    parser = _TransactionsParser()
    parser.feed(page)
    transactions: list[Transaction] = []
    for short_date, description in parser.rows:
        try:
            item_date = datetime.strptime(
                f"{short_date}/{target_date.year}", "%m/%d/%Y"
            ).date()
        except ValueError:
            continue
        if item_date != target_date:
            continue
        for elevation in _elevation_descriptions(description):
            digest = hashlib.sha256(elevation.encode("utf-8")).hexdigest()[:20]
            players = extract_players(elevation)
            transactions.append(
                Transaction(
                    transaction_id=(
                        f"ravens-official:{target_date.isoformat()}:{digest}"
                    ),
                    date=target_date,
                    description=elevation,
                    type_text=transaction_action(elevation),
                    athlete=players[0].name if players else None,
                    players=players,
                    team=RAVENS,
                )
            )
    return transactions


def merge_standard_elevations(
    transactions: list[Transaction], elevations: list[Transaction]
) -> list[Transaction]:
    """Add official elevations that another transaction feed already did not carry."""
    seen = {normalize_name(item.description) for item in transactions}
    merged = list(transactions)
    for item in elevations:
        key = normalize_name(item.description)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


class OfficialTransactionsClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def fetch_standard_elevations(
        self, target_date: date
    ) -> list[Transaction]:
        try:
            async with self._session.get(
                TRANSACTIONS_URL.format(year=target_date.year),
                headers={"User-Agent": "The-Castle Ravens Discord bot"},
            ) as response:
                response.raise_for_status()
                page = await response.text()
        except (aiohttp.ClientError, UnicodeError) as exc:
            raise OfficialTransactionsError(
                "The official Ravens transaction log could not be fetched."
            ) from exc
        return parse_standard_elevations(page, target_date)
