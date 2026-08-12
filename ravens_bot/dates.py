from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


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


def upcoming_window(days: int, time_zone: ZoneInfo) -> DateWindow:
    if days < 1 or days > 30:
        raise ValueError("days must be between 1 and 30")
    start = today_in_zone(time_zone)
    return DateWindow(start=start, end=start + timedelta(days=days - 1))


def espn_dates(window: DateWindow) -> str:
    return f"{window.start:%Y%m%d}-{window.end:%Y%m%d}"
