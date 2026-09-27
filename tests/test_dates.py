from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from ravens_bot.dates import (
    MAX_SCHEDULE_DAYS,
    POSTSEASON,
    REGULAR_SEASON,
    DateWindow,
    WeekRequest,
    espn_dates,
    league_year,
    parse_user_date_or_week,
    today_in_zone,
    upcoming_window,
)


EASTERN = ZoneInfo("America/New_York")


def test_espn_single_day_is_not_a_degenerate_range() -> None:
    day = date(2026, 9, 20)
    assert espn_dates(DateWindow(day, day)) == "20260920"


def test_espn_multi_day_keeps_both_dates() -> None:
    assert espn_dates(
        DateWindow(date(2026, 9, 20), date(2026, 9, 27))
    ) == "20260920-20260927"


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("week 5", WeekRequest(5, REGULAR_SEASON)),
        ("Week 5", WeekRequest(5, REGULAR_SEASON)),
        ("wk5", WeekRequest(5, REGULAR_SEASON)),
        ("wk 5", WeekRequest(5, REGULAR_SEASON)),
        ("5", WeekRequest(5, REGULAR_SEASON)),
        ("week #12", WeekRequest(12, REGULAR_SEASON)),
        ("18", WeekRequest(18, REGULAR_SEASON)),
        ("wild card", WeekRequest(1, POSTSEASON)),
        ("Wildcard", WeekRequest(1, POSTSEASON)),
        ("divisional", WeekRequest(2, POSTSEASON)),
        ("conference championship", WeekRequest(3, POSTSEASON)),
        ("super bowl", WeekRequest(5, POSTSEASON)),
    ],
)
def test_a_week_can_be_written_the_way_it_is_said(raw: str, expected: WeekRequest) -> None:
    assert parse_user_date_or_week(raw, EASTERN) == expected


def test_a_date_still_reads_as_a_date() -> None:
    assert parse_user_date_or_week("2026-09-20", EASTERN) == date(2026, 9, 20)


def test_no_argument_still_means_today() -> None:
    assert parse_user_date_or_week(None, EASTERN) == today_in_zone(EASTERN)
    assert parse_user_date_or_week("today", EASTERN) == today_in_zone(EASTERN)


@pytest.mark.parametrize("raw", ["week 19", "0", "week nineteen", "preseason", "last week"])
def test_a_week_nobody_plays_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_user_date_or_week(raw, EASTERN)


def test_the_league_year_does_not_roll_over_with_the_calendar() -> None:
    assert league_year(date(2027, 1, 10)) == 2026
    assert league_year(date(2027, 2, 8)) == 2026
    assert league_year(date(2026, 9, 20)) == 2026
    assert league_year(date(2027, 3, 1)) == 2027
