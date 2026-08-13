from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from ravens_bot.dates import (
    MAX_SCHEDULE_DAYS,
    espn_dates,
    today_in_zone,
    upcoming_window,
)


EASTERN = ZoneInfo("America/New_York")


def test_upcoming_window_covers_the_days_asked_for() -> None:
    window = upcoming_window(7, EASTERN)

    assert window.start == today_in_zone(EASTERN)
    assert window.end == window.start + timedelta(days=6)


def test_upcoming_window_reaches_a_full_year() -> None:
    window = upcoming_window(MAX_SCHEDULE_DAYS, EASTERN)

    assert window.end == window.start + timedelta(days=MAX_SCHEDULE_DAYS - 1)
    assert espn_dates(window).count("-") == 1


@pytest.mark.parametrize("days", [0, MAX_SCHEDULE_DAYS + 1])
def test_upcoming_window_rejects_a_window_it_cannot_answer(days: int) -> None:
    with pytest.raises(ValueError):
        upcoming_window(days, EASTERN)
