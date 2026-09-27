from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


# A whole season runs from September into February, and asking in the offseason
# means reaching further still, so a schedule window covers a full year rather
# than the month a scoreboard query is comfortable with.
MAX_SCHEDULE_DAYS = 366

# ESPN's season types, as the schedule endpoint numbers them.
REGULAR_SEASON = 2
POSTSEASON = 3
# The regular season is 18 weeks long, so a larger number is a typo rather than
# a game nobody has heard about yet.
MAX_REGULAR_SEASON_WEEK = 18
# ESPN numbers the postseason as its own weeks, with the Pro Bowl sitting at
# four between the conference championships and the Super Bowl.
POSTSEASON_WEEKS: dict[str, int] = {
    "wild card": 1,
    "wildcard": 1,
    "wc": 1,
    "divisional": 2,
    "divisional round": 2,
    "conference": 3,
    "conference championship": 3,
    "championship": 3,
    "afc championship": 3,
    "super bowl": 5,
    "superbowl": 5,
    "sb": 5,
}
_WEEK_RE = re.compile(r"^(?:week|wk)?\s*#?\s*(\d{1,2})$")
DATE_HELP = (
    "Date must be today, YYYY-MM-DD, a week such as week 5, "
    "or a round such as wild card"
)


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date


def now_in_zone(time_zone: ZoneInfo) -> datetime:
    return datetime.now(time_zone)


def today_in_zone(time_zone: ZoneInfo) -> date:
    return now_in_zone(time_zone).date()


def parse_user_date(raw: str | None, time_zone: ZoneInfo) -> date:
    if raw is None or not raw.strip() or raw.strip().lower() == "today":
        return today_in_zone(time_zone)
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError("Date must be today or YYYY-MM-DD") from exc


@dataclass(frozen=True)
class WeekRequest:
    """A week of the current league year, which only a schedule can date."""

    week_number: int
    season_type: int = REGULAR_SEASON

    @property
    def label(self) -> str:
        if self.season_type == REGULAR_SEASON:
            return f"week {self.week_number}"
        names = {1: "wild card", 2: "divisional", 3: "conference championship", 5: "Super Bowl"}
        return names.get(self.week_number, f"postseason week {self.week_number}")


def parse_week(raw: str | None) -> WeekRequest | None:
    """A week request, or nothing when the text does not name a week."""
    text = " ".join((raw or "").split()).casefold()
    if not text:
        return None
    round_week = POSTSEASON_WEEKS.get(text)
    if round_week is not None:
        return WeekRequest(round_week, POSTSEASON)
    match = _WEEK_RE.match(text)
    if match is None:
        return None
    number = int(match.group(1))
    if not 1 <= number <= MAX_REGULAR_SEASON_WEEK:
        return None
    return WeekRequest(number, REGULAR_SEASON)


def parse_user_date_or_week(
    raw: str | None, time_zone: ZoneInfo
) -> date | WeekRequest:
    """A date, or the week to look one up from.

    A week cannot be turned into a date here: only the season schedule knows
    which day the Ravens played that week, so the request is passed on intact.
    """
    if raw is None or not raw.strip() or raw.strip().lower() == "today":
        return today_in_zone(time_zone)
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        pass
    week = parse_week(raw)
    if week is None:
        raise ValueError(DATE_HELP)
    return week


def league_year(today: date) -> int:
    """The season a date belongs to, counting January and February as last year."""
    return today.year if today.month >= 3 else today.year - 1


def upcoming_window(days: int, time_zone: ZoneInfo) -> DateWindow:
    if days < 1 or days > MAX_SCHEDULE_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_SCHEDULE_DAYS}")
    start = today_in_zone(time_zone)
    return DateWindow(start=start, end=start + timedelta(days=days - 1))


def espn_dates(window: DateWindow) -> str:
    if window.start == window.end:
        return f"{window.start:%Y%m%d}"
    return f"{window.start:%Y%m%d}-{window.end:%Y%m%d}"
