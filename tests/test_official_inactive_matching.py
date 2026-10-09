from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest

from ravens_bot.official_inactives import (
    INACTIVES_URL,
    OfficialInactivesClient,
    article_matches,
)


@pytest.mark.parametrize("headline", ["Week 10", "Week 11", "Week 18"])
def test_headline_fallback_matches_the_whole_week_number(headline: str) -> None:
    assert not article_matches(
        f"Ravens Inactives: {headline}", None, date(2026, 9, 13), "Week 1"
    )


@pytest.mark.parametrize("stamp", ['datetime="2025-09-13"', ""])
def test_an_undated_listing_cannot_supply_another_seasons_inactives(stamp: str) -> None:
    listing = '<a href="/news/old-inactives">Ravens Inactives: Week 1</a>'
    article = (
        f"<article><time {stamp}></time><h2>Ravens Inactives</h2>"
        "<ul><li>WR Zay Flowers</li></ul></article>"
    )
    client = OfficialInactivesClient(None)  # type: ignore[arg-type]
    client._page = AsyncMock(side_effect=[listing, article])

    assert asyncio.run(client.fetch_inactives(date(2026, 9, 13), "Week 1")) == ()


def test_an_undated_listing_uses_the_article_publication_date() -> None:
    listing = '<a href="/news/current-inactives">Ravens Inactives: Week 1</a>'
    article = (
        '<article><time datetime="2026-09-13"></time><h2>Ravens Inactives</h2>'
        "<ul><li>WR Zay Flowers</li></ul></article>"
    )
    client = OfficialInactivesClient(None)  # type: ignore[arg-type]
    client._page = AsyncMock(side_effect=[listing, article])

    players = asyncio.run(client.fetch_inactives(date(2026, 9, 13), "Week 1"))

    assert tuple(player.name for player in players) == ("Zay Flowers",)
    client._page.assert_any_await(INACTIVES_URL)


def test_a_listing_date_on_the_link_is_respected() -> None:
    listing = (
        '<a href="/news/old-inactives" data-date="2025-09-13">'
        "Ravens Inactives: Week 1</a>"
    )
    client = OfficialInactivesClient(None)  # type: ignore[arg-type]
    client._page = AsyncMock(return_value=listing)

    assert asyncio.run(client.fetch_inactives(date(2026, 9, 13), "Week 1")) == ()
    client._page.assert_awaited_once_with(INACTIVES_URL)
