from __future__ import annotations

import hashlib
import html
import logging
import time
from dataclasses import dataclass
from dataclasses import replace
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Callable, Mapping
from urllib.parse import urljoin

import aiohttp

from .chart import (
    ALT_ROW_GLASS_ALPHA,
    ArtworkLoader,
    CELL_PADDING,
    ChartSection,
    DISPLAY_HEADERS,
    FONT_DIRECTORY,
    GLASS_SUPERSAMPLE,
    GRID_COLOR,
    HEADER_BACKGROUND,
    HEADSHOT_GAP,
    HEADSHOT_SIZE,
    MARGIN,
    MAX_IMAGE_WIDTH,
    MIN_COLUMN_WIDTH,
    MIN_IMAGE_WIDTH,
    NFL_PRIMARY_COLORS,
    OUTPUT_SCALE,
    PAGE_BACKGROUND,
    RAVENS_PURPLE,
    ROW_GLASS_ALPHA,
    ROW_HEIGHT,
    TEXT_COLOR,
    _contrasting_text_color,
    _display_header,
    _draw_headshot,
    _draw_logo,
    _fit_text,
    _font,
    render_chart,
    team_color as _team_color,
)
from .models import Game, RAVENS_NAME, TeamRef


INJURY_REPORT_URL = "https://www.baltimoreravens.com/team/injury-report/"
REPORT_SETTLE_SECONDS = 300
LOGGER = logging.getLogger(__name__)


class InjuryReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class InjuryTable:
    team: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    headshots: tuple[str | None, ...] = ()
    logo_url: str | None = None


@dataclass(frozen=True)
class InjuryMatchup:
    away: str
    home: str
    away_team: str
    home_team: str
    away_color: str
    home_color: str
    away_logo: str | None
    home_logo: str | None
    opponent: str
    opponent_color: str
    kickoff: datetime | None


@dataclass(frozen=True)
class OfficialInjuryReport:
    week: str
    tables: tuple[InjuryTable, ...]
    path: str | None = None
    matchup: InjuryMatchup | None = None

    @property
    def title(self) -> str:
        if self.matchup is None:
            return f"Ravens Injury Report | {self.week.title()}"
        return (
            f"{self.matchup.away} @ {self.matchup.home} Injury Report"
            f" | {self.week.title()}"
        )

    @property
    def url(self) -> str:
        return urljoin(INJURY_REPORT_URL, self.path or "")

    @property
    def teams_are_synchronized(self) -> bool:
        """Both clubs have published the same latest practice-day column."""
        days = tuple(_latest_practice_day(table) for table in self.tables)
        return len(days) == 2 and days[0] is not None and days[0] == days[1]

    @property
    def latest_practice_day(self) -> str | None:
        if not self.teams_are_synchronized:
            return None
        return _latest_practice_day(self.tables[0])

    @property
    def announcement_key(self) -> str:
        parts = [self.week]
        for table in self.tables:
            parts.extend((table.team, *table.headers))
            parts.extend(cell for row in table.rows for cell in row)
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:20]
        return f"official-injury:{self.week.lower().replace(' ', '-')}:{digest}"


def _classes(attributes: list[tuple[str, str | None]]) -> set[str]:
    value = next((value for name, value in attributes if name == "class"), None)
    return set((value or "").split())


class _ReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.week = ""
        self.path: str | None = None
        self.tables: list[InjuryTable] = []
        self._team = ""
        self._team_logo: str | None = None
        self._in_team_header = False
        self._capture_team = False
        self._team_text: list[str] = []
        self._in_table = False
        self._headers: list[str] = []
        self._rows: list[tuple[str, ...]] = []
        self._headshots: list[str | None] = []
        self._cell_tag: str | None = None
        self._cell_text: list[str] = []
        self._row: list[str] = []
        self._row_headshot: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        classes = _classes(attrs)
        if tag == "option" and any(name == "selected" for name, _ in attrs):
            self._cell_tag = "option"
            self._cell_text = []
            self.path = next(
                (value for name, value in attrs if name == "value" and value),
                None,
            )
        elif tag == "div" and "nfl-o-injury-report__title" in classes:
            self._in_team_header = True
            self._team_logo = None
        elif self._in_team_header and tag in {"source", "img"}:
            source = _image_source(attrs)
            if source and not source.startswith("data:") and self._team_logo is None:
                self._team_logo = _largest_image_url(source)
        elif tag == "span" and "nfl-o-injury-report__club-name" in classes:
            self._capture_team = True
            self._team_text = []
        elif tag == "table" and self._team:
            self._in_team_header = False
            self._in_table = True
            self._headers = []
            self._rows = []
            self._headshots = []
        elif self._in_table and tag == "tr":
            self._row = []
            self._row_headshot = None
        elif self._in_table and tag in {"th", "td"}:
            self._cell_tag = tag
            self._cell_text = []
        elif self._in_table and self._cell_tag == "td" and tag in {"source", "img"}:
            source = _image_source(attrs)
            if source and not source.startswith("data:") and self._row_headshot is None:
                self._row_headshot = _largest_image_url(source)

    def handle_data(self, data: str) -> None:
        if self._capture_team:
            self._team_text.append(data)
        if self._cell_tag:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._cell_tag == "option":
            self.week = _clean_text(self._cell_text)
            self._cell_tag = None
        elif tag == "span" and self._capture_team:
            self._team = _clean_text(self._team_text)
            self._capture_team = False
        elif self._in_table and tag in {"th", "td"} and self._cell_tag == tag:
            text = _clean_text(self._cell_text)
            if tag == "th":
                self._headers.append(text)
            else:
                self._row.append(text or "-")
            self._cell_tag = None
        elif self._in_table and tag == "tr" and self._row:
            self._rows.append(tuple(self._row))
            self._headshots.append(self._row_headshot)
            self._row = []
        elif self._in_table and tag == "table":
            if self._headers:
                self.tables.append(
                    InjuryTable(
                        team=self._team,
                        headers=tuple(self._headers),
                        rows=tuple(self._rows),
                        headshots=tuple(self._headshots),
                        logo_url=self._team_logo,
                    )
                )
            self._in_table = False


def _clean_text(parts: list[str]) -> str:
    return " ".join(html.unescape("".join(parts)).split())


def _image_source(attributes: list[tuple[str, str | None]]) -> str | None:
    return next(
        (
            value
            for name, value in attributes
            if name in {"data-srcset", "srcset", "data-src", "src"} and value
        ),
        None,
    )


def _largest_image_url(source: str) -> str:
    candidates = [item.strip().split()[0] for item in source.split(",") if item.strip()]
    selected = candidates[-1] if candidates else source
    # t_lazy is Cloudinary's intentionally blurred placeholder transformation.
    return selected.replace("/t_lazy", "")


def _latest_practice_day(table: InjuryTable) -> str | None:
    try:
        injury_column = table.headers.index("Injury")
        game_status_column = table.headers.index("Game Status")
    except ValueError:
        return None
    for column in range(game_status_column - 1, injury_column, -1):
        if any(
            column < len(row) and row[column] not in {"", "-"} for row in table.rows
        ):
            return table.headers[column]
    return None


class OfficialReportGate:
    """Release a synchronized report after its contents stay quiet for five minutes."""

    def __init__(
        self,
        delay_seconds: float = REPORT_SETTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._delay_seconds = delay_seconds
        self._clock = clock
        self._pending_key: str | None = None
        self._pending_since = 0.0

    def ready(self, report: OfficialInjuryReport) -> bool:
        if not report.teams_are_synchronized:
            self._pending_key = None
            return False
        if report.announcement_key != self._pending_key:
            self._pending_key = report.announcement_key
            self._pending_since = self._clock()
            return False
        return self._clock() - self._pending_since >= self._delay_seconds


def parse_injury_report(page: str) -> OfficialInjuryReport:
    parser = _ReportParser()
    parser.feed(page)
    report = OfficialInjuryReport(
        parser.week,
        tuple(parser.tables),
        path=parser.path,
    )
    if not report.week:
        raise InjuryReportError("The Ravens injury report did not identify its week.")
    if not report.tables:
        raise InjuryReportError(
            f"The Ravens have not published an injury chart for {report.week.title()}."
        )
    return report


class InjuryReportClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._artwork = ArtworkLoader(session)

    async def fetch(self) -> OfficialInjuryReport:
        try:
            async with self._session.get(
                INJURY_REPORT_URL,
                headers={"User-Agent": "The-Castle Ravens Discord bot"},
            ) as response:
                response.raise_for_status()
                page = await response.text()
        except (aiohttp.ClientError, UnicodeError) as exc:
            raise InjuryReportError(
                "The official Ravens injury report could not be fetched."
            ) from exc
        return parse_injury_report(page)

    async def fetch_artwork(
        self, report: OfficialInjuryReport
    ) -> dict[str, bytes]:
        return await self._artwork.fetch(
            url
            for table in report.tables
            for url in (*table.headshots, _table_logo_url(report, table))
        )


def add_matchup(
    report: OfficialInjuryReport, games: list[Game]
) -> OfficialInjuryReport:
    week_number = _week_number(report.week)
    game = next(
        (
            game
            for game in games
            if game.season_type == 2
            and game.week_number == week_number
            and game.away is not None
            and game.home is not None
        ),
        None,
    )
    if game is None or game.away is None or game.home is None:
        return report
    opponent = game.opponent
    if opponent is None:
        return report
    return replace(
        report,
        matchup=InjuryMatchup(
            away=_nickname(game.away.team.name),
            home=_nickname(game.home.team.name),
            away_team=game.away.team.name,
            home_team=game.home.team.name,
            away_color=_team_color(game.away.team),
            home_color=_team_color(game.home.team),
            away_logo=game.away.team.logo_url,
            home_logo=game.home.team.logo_url,
            opponent=opponent.team.name,
            opponent_color=_team_color(opponent.team),
            kickoff=game.start_time,
        ),
    )


def is_scheduled_report_date(
    report: OfficialInjuryReport,
    target_date: date,
    time_zone: ZoneInfo,
) -> bool:
    """Whether the report's newest practice column belongs to this date.

    Matching the column's weekday back from kickoff follows ESPN's shifted
    Mon/Tue/Wed and Thu/Fri/Sat report schedules without hard-coding game days.
    """
    weekdays = {
        name.casefold(): index
        for index, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    }
    matchup = report.matchup
    if matchup is None or matchup.kickoff is None:
        return False
    kickoff = matchup.kickoff
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=time_zone)
    kickoff_date = kickoff.astimezone(time_zone).date()
    for table in report.tables:
        day = (_latest_practice_day(table) or "").strip()[:3].casefold()
        if day not in weekdays:
            continue
        days_before = (kickoff_date.weekday() - weekdays[day]) % 7
        if kickoff_date - timedelta(days=days_before) == target_date:
            return True
    return False


def _week_number(week: str) -> int | None:
    last = week.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else None


def _nickname(team: str) -> str:
    return team.rsplit(" ", 1)[-1]


def _table_logo_url(
    report: OfficialInjuryReport, table: InjuryTable
) -> str | None:
    matchup = report.matchup
    if matchup is None:
        return table.logo_url
    if table.team == matchup.away_team:
        return matchup.away_logo
    if table.team == matchup.home_team:
        return matchup.home_logo
    return table.logo_url


def _table_color(report: OfficialInjuryReport, table: InjuryTable) -> str:
    if table.team == RAVENS_NAME:
        return RAVENS_PURPLE
    matchup = report.matchup
    if matchup is None:
        return HEADER_BACKGROUND
    if table.team == matchup.away_team:
        return matchup.away_color
    if table.team == matchup.home_team:
        return matchup.home_color
    return HEADER_BACKGROUND


def render_injury_report(
    report: OfficialInjuryReport,
    headshots: Mapping[str, bytes] | None = None,
) -> bytes:
    """The report as a chart, in the shared chart style."""
    return render_chart(
        _chart_sections(report),
        headshots,
        report.matchup.away_color if report.matchup else RAVENS_PURPLE,
        report.matchup.home_color if report.matchup else RAVENS_PURPLE,
    )


def _chart_sections(report: OfficialInjuryReport) -> tuple[ChartSection, ...]:
    return tuple(
        ChartSection(
            title=table.team,
            color=_table_color(report, table),
            headers=table.headers,
            rows=table.rows,
            headshots=table.headshots,
            logo_url=_table_logo_url(report, table),
        )
        for table in report.tables
    )
