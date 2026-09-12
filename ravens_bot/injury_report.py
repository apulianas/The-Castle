from __future__ import annotations

import asyncio
import hashlib
import html
import io
import logging
import time
from dataclasses import dataclass
from dataclasses import replace
from html.parser import HTMLParser
from math import ceil
from typing import Callable, Mapping
from urllib.parse import urljoin

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .models import Game, RAVENS_NAME, TeamRef


INJURY_REPORT_URL = "https://www.baltimoreravens.com/team/injury-report/"
# The chart is read on a phone, so the type is large and the page is only as
# wide as the columns need; the cap keeps a long name from stretching it.
MAX_IMAGE_WIDTH = 1100
MIN_IMAGE_WIDTH = 520
MARGIN = 28
CELL_PADDING = 14
MIN_COLUMN_WIDTH = 76
ROW_HEIGHT = 60
HEADSHOT_SIZE = 44
HEADSHOT_GAP = 10
PAGE_BACKGROUND = "#f4f4f4"
RAVENS_PURPLE = "#24125f"
HEADER_BACKGROUND = "#111111"
ROW_BACKGROUND = "#ffffff"
ALT_ROW_BACKGROUND = "#ececec"
TEXT_COLOR = "#111111"
REPORT_SETTLE_SECONDS = 300
LOGGER = logging.getLogger(__name__)
NFL_PRIMARY_COLORS = {
    "ARI": "#97233f",
    "ATL": "#a71930",
    "BAL": RAVENS_PURPLE,
    "BUF": "#00338d",
    "CAR": "#0085ca",
    "CHI": "#0b162a",
    "CIN": "#fb4f14",
    "CLE": "#311d00",
    "DAL": "#003594",
    "DEN": "#fb4f14",
    "DET": "#0076b6",
    "GB": "#203731",
    "HOU": "#03202f",
    "IND": "#002c5f",
    "JAX": "#006778",
    "KC": "#e31837",
    "LV": "#000000",
    "LAC": "#0080c6",
    "LAR": "#003594",
    "MIA": "#008e97",
    "MIN": "#4f2683",
    "NE": "#002244",
    "NO": "#101820",
    "NYG": "#0b2265",
    "NYJ": "#125740",
    "PHI": "#004c54",
    "PIT": "#ffb612",
    "SEA": "#002244",
    "SF": "#aa0000",
    "TB": "#d50a0a",
    "TEN": "#0c2340",
    "WSH": "#5a1414",
}


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
        self._headshot_cache: dict[str, bytes] = {}

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
        urls = {
            url
            for table in report.tables
            for url in (*table.headshots, _table_logo_url(report, table))
            if url and url not in self._headshot_cache
        }
        if urls:
            pending_urls = sorted(urls)
            results = await asyncio.gather(
                *(self._fetch_headshot(url) for url in pending_urls)
            )
            for url, image in zip(pending_urls, results):
                if image is not None:
                    self._headshot_cache[url] = image
        return {
            url: self._headshot_cache[url]
            for table in report.tables
            for url in (*table.headshots, _table_logo_url(report, table))
            if url in self._headshot_cache
        }

    async def _fetch_headshot(self, url: str) -> bytes | None:
        try:
            async with self._session.get(url) as response:
                response.raise_for_status()
                if not response.content_type.startswith("image/"):
                    LOGGER.warning("Ignoring non-image player headshot from %s", url)
                    return None
                return await response.read()
        except aiohttp.ClientError as exc:
            LOGGER.warning("Could not fetch player headshot %s: %s", url, exc)
            return None


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
        ),
    )


def _week_number(week: str) -> int | None:
    last = week.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else None


def _nickname(team: str) -> str:
    return team.rsplit(" ", 1)[-1]


def _team_color(team: TeamRef) -> str:
    if team.name == RAVENS_NAME:
        return RAVENS_PURPLE
    return team.color or NFL_PRIMARY_COLORS.get(
        team.abbreviation or "", HEADER_BACKGROUND
    )


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


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default(size=size)


def render_injury_report(
    report: OfficialInjuryReport,
    headshots: Mapping[str, bytes] | None = None,
) -> bytes:
    """The report as a chart, sized so a phone can read it without zooming.

    The chart is read on a screen a few inches wide, so the type is set large
    and every column is only as wide as the longest thing in it. A fixed width
    stretched the practice-status columns — which never hold more than a dash or
    a letter — across half the image and left the type small to fit.
    """
    headshots = headshots or {}
    title_font = _font(38, bold=True)
    team_font = _font(30, bold=True)
    header_font = _font(24, bold=True)
    cell_font = _font(24)
    margin = MARGIN
    title_height = 96
    team_height = 60
    row_height = ROW_HEIGHT
    table_gap = 26

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    column_widths = _column_widths(
        measure, report.tables, header_font, cell_font, headshots
    )
    width = margin * 2 + sum(column_widths) if column_widths else MIN_IMAGE_WIDTH
    width = max(width, MIN_IMAGE_WIDTH)
    height = title_height + margin
    for table in report.tables:
        height += team_height + row_height * (len(table.rows) + 1) + table_gap

    image = Image.new("RGB", (width, height), PAGE_BACKGROUND)
    draw = ImageDraw.Draw(image)
    left_color = report.matchup.away_color if report.matchup else RAVENS_PURPLE
    right_color = report.matchup.home_color if report.matchup else RAVENS_PURPLE
    _draw_gradient(draw, width, title_height, left_color, right_color)
    draw.text(
        (margin, 23),
        report.title,
        fill="white",
        font=title_font,
    )

    y = title_height + 24
    for table in report.tables:
        team_color = (
            RAVENS_PURPLE
            if table.team == RAVENS_NAME
            else report.matchup.opponent_color
            if report.matchup and table.team == report.matchup.opponent
            else HEADER_BACKGROUND
        )
        team_text_x = margin
        logo_url = _table_logo_url(report, table)
        if logo_url and logo_url in headshots:
            _draw_logo(image, headshots[logo_url], margin, y + 5)
            team_text_x += 54
        draw.text((team_text_x, y + 10), table.team, fill=team_color, font=team_font)
        y += team_height
        x = margin
        for heading, column_width in zip(table.headers, column_widths):
            draw.rectangle(
                (x, y, x + column_width, y + row_height),
                fill=HEADER_BACKGROUND,
            )
            draw.text(
                (x + CELL_PADDING, _text_top(draw, y, row_height, header_font)),
                _fit_text(draw, heading, header_font, column_width - CELL_PADDING * 2),
                fill="white",
                font=header_font,
            )
            x += column_width
        y += row_height

        for index, row in enumerate(table.rows):
            x = margin
            background = ROW_BACKGROUND if index % 2 == 0 else ALT_ROW_BACKGROUND
            for column, (cell, column_width) in enumerate(zip(row, column_widths)):
                draw.rectangle(
                    (x, y, x + column_width, y + row_height),
                    fill=background,
                    outline="#d0d0d0",
                )
                text_x = x + CELL_PADDING
                if column == 0 and _row_headshot(table, index, headshots) is not None:
                    _draw_headshot(
                        image,
                        headshots[_row_headshot(table, index, headshots)],
                        x + CELL_PADDING,
                        y + (row_height - HEADSHOT_SIZE) // 2,
                    )
                    text_x += HEADSHOT_SIZE + HEADSHOT_GAP
                draw.text(
                    (text_x, _text_top(draw, y, row_height, cell_font)),
                    _fit_text(
                        draw,
                        cell,
                        cell_font,
                        x + column_width - CELL_PADDING - text_x,
                    ),
                    fill=TEXT_COLOR,
                    font=cell_font,
                )
                x += column_width
            y += row_height
        y += table_gap

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _draw_gradient(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    left: str,
    right: str,
) -> None:
    start = tuple(bytes.fromhex(left.lstrip("#")))
    end = tuple(bytes.fromhex(right.lstrip("#")))
    for x in range(width):
        ratio = x / max(width - 1, 1)
        color = tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))
        draw.line((x, 0, x, height), fill=color)


def _draw_headshot(
    canvas: Image.Image, data: bytes, x: int, y: int
) -> None:
    try:
        with Image.open(io.BytesIO(data)) as source:
            fitted = ImageOps.fit(
                source.convert("RGBA"), (HEADSHOT_SIZE, HEADSHOT_SIZE)
            )
            photo = Image.new("RGB", fitted.size, "white")
            photo.paste(fitted, mask=fitted.getchannel("A"))
    except (OSError, UnidentifiedImageError) as exc:
        LOGGER.warning("Could not render a player headshot: %s", exc)
        return
    mask = Image.new("L", photo.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, HEADSHOT_SIZE - 1, HEADSHOT_SIZE - 1), radius=7, fill=255
    )
    canvas.paste(photo, (x, y), mask)


def _draw_logo(canvas: Image.Image, data: bytes, x: int, y: int) -> None:
    try:
        with Image.open(io.BytesIO(data)) as source:
            logo = ImageOps.contain(source.convert("RGBA"), (42, 42))
    except (OSError, UnidentifiedImageError) as exc:
        LOGGER.warning("Could not render a team logo: %s", exc)
        return
    canvas.paste(logo, (x + (42 - logo.width) // 2, y), logo)


def _row_headshot(
    table: InjuryTable, index: int, headshots: Mapping[str, bytes]
) -> str | None:
    """The headshot a row can actually draw, if one was downloaded for it."""
    if index >= len(table.headshots):
        return None
    url = table.headshots[index]
    return url if url and url in headshots else None


def _column_widths(
    draw: ImageDraw.ImageDraw,
    tables: tuple[InjuryTable, ...],
    header_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    cell_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    headshots: Mapping[str, bytes],
) -> list[int]:
    """A width per column, measured from the widest thing that column holds.

    Both teams' tables share one set of widths so the two charts line up, and a
    column whose cells only ever say "DNP" takes the room that word needs rather
    than a share of a fixed page width.
    """
    columns = max((len(table.headers) for table in tables), default=0)
    if not columns:
        return []
    widths = [0] * columns
    for table in tables:
        for column, heading in enumerate(table.headers[:columns]):
            widths[column] = max(
                widths[column], ceil(draw.textlength(heading, font=header_font))
            )
        for index, row in enumerate(table.rows):
            for column, cell in enumerate(row[:columns]):
                text = ceil(draw.textlength(cell, font=cell_font))
                if column == 0 and _row_headshot(table, index, headshots):
                    text += HEADSHOT_SIZE + HEADSHOT_GAP
                widths[column] = max(widths[column], text)
    widths = [
        max(width + CELL_PADDING * 2, MIN_COLUMN_WIDTH) for width in widths
    ]
    return _fit_columns(widths)


def _fit_columns(widths: list[int]) -> list[int]:
    """Columns trimmed to a width a phone shows without shrinking the type.

    Only the widest column gives room up, since it is the one holding names, and
    a name that no longer fits is shortened rather than set in smaller type.
    """
    available = MAX_IMAGE_WIDTH - MARGIN * 2
    overflow = sum(widths) - available
    while overflow > 0:
        widest = max(range(len(widths)), key=lambda index: widths[index])
        room = widths[widest] - MIN_COLUMN_WIDTH
        if room <= 0:
            break
        trimmed = min(room, overflow)
        widths[widest] -= trimmed
        overflow -= trimmed
    return widths


def _text_top(
    draw: ImageDraw.ImageDraw,
    y: int,
    row_height: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    """The baseline-free top of text centred in a row of the chart."""
    top, bottom = draw.textbbox((0, 0), "Ag", font=font)[1::2]
    return y + (row_height - (bottom - top)) // 2 - top


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    shortened = text
    while shortened and draw.textlength(f"{shortened}…", font=font) > max_width:
        shortened = shortened[:-1]
    return f"{shortened.rstrip()}…"
