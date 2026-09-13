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
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urljoin

import aiohttp
from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

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
PAGE_BACKGROUND = "#08050f"
RAVENS_PURPLE = "#24125f"
HEADER_BACKGROUND = "#111111"
ROW_GLASS_ALPHA = 38
ALT_ROW_GLASS_ALPHA = 54
TEXT_COLOR = "#f8f6ff"
GRID_COLOR = "#756d85"
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
DISPLAY_HEADERS = {
    "POSITION": "Pos",
}
FONT_DIRECTORY = Path(__file__).with_name("fonts")
GLASS_SUPERSAMPLE = 4
OUTPUT_SCALE = 2


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


def _contrasting_text_color(background: str) -> str:
    red, green, blue = bytes.fromhex(background.lstrip("#"))

    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    luminance = (
        0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
    )
    return "#111111" if luminance > 0.179 else "#ffffff"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "D-DIN-Bold.ttf" if bold else "D-DIN.ttf"
    return ImageFont.truetype(FONT_DIRECTORY / name, size)


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
    scale = OUTPUT_SCALE
    team_font = _font(30 * scale, bold=True)
    header_font = _font(24 * scale, bold=True)
    cell_font = _font(24 * scale)
    margin = MARGIN * scale
    cell_padding = CELL_PADDING * scale
    headshot_size = HEADSHOT_SIZE * scale
    headshot_gap = HEADSHOT_GAP * scale
    team_height = 60 * scale
    row_height = ROW_HEIGHT * scale
    table_gap = 26 * scale

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    column_widths = _column_widths(
        measure, report.tables, header_font, cell_font, headshots, scale
    )
    width = (
        margin * 2 + sum(column_widths)
        if column_widths
        else MIN_IMAGE_WIDTH * scale
    )
    width = max(width, MIN_IMAGE_WIDTH * scale)
    height = margin
    for table in report.tables:
        height += team_height + row_height * (len(table.rows) + 1) + table_gap

    left_color = report.matchup.away_color if report.matchup else RAVENS_PURPLE
    right_color = report.matchup.home_color if report.matchup else RAVENS_PURPLE
    image = Image.new("RGB", (width, height), PAGE_BACKGROUND)
    _draw_page_background(image, left_color, right_color)
    draw = ImageDraw.Draw(image)

    y = margin
    for table in report.tables:
        team_color = _table_color(report, table)
        header_text_color = _contrasting_text_color(team_color)
        team_text_color = (
            team_color
            if header_text_color == "#111111"
            else _rgb_hex(_blend_color(team_color, "#ffffff", 0.48))
        )
        table_width = sum(column_widths)
        _draw_liquid_glass_panel(
            image,
            (
                margin - 8 * scale,
                y - 6 * scale,
                margin + table_width + 8 * scale,
                y
                + team_height
                + row_height * (len(table.rows) + 1)
                + 8 * scale,
            ),
            team_color,
            scale,
        )
        team_text_x = margin
        logo_url = _table_logo_url(report, table)
        if logo_url and logo_url in headshots:
            _draw_logo(
                image,
                headshots[logo_url],
                margin,
                y + 5 * scale,
                42 * scale,
            )
            team_text_x += 54 * scale
        draw.text(
            (team_text_x, y + 10 * scale),
            table.team,
            fill=team_text_color,
            font=team_font,
        )
        y += team_height
        x = margin
        _draw_glass_bar(
            image,
            (margin, y, margin + sum(column_widths), y + row_height),
            team_color,
            scale,
        )
        for heading, column_width in zip(table.headers, column_widths):
            heading = _display_header(heading)
            draw.line(
                (
                    x + column_width,
                    y + scale,
                    x + column_width,
                    y + row_height - scale,
                ),
                fill=_blend_color(team_color, "#ffffff", 0.28),
                width=scale,
            )
            draw.text(
                (x + cell_padding, _text_top(draw, y, row_height, header_font)),
                _fit_text(
                    draw,
                    heading,
                    header_font,
                    column_width - cell_padding * 2,
                ),
                fill=header_text_color,
                font=header_font,
            )
            x += column_width
        y += row_height

        for index, row in enumerate(table.rows):
            x = margin
            glass_alpha = (
                ROW_GLASS_ALPHA if index % 2 == 0 else ALT_ROW_GLASS_ALPHA
            )
            for column, (cell, column_width) in enumerate(zip(row, column_widths)):
                _draw_glass_cell(
                    image,
                    (x, y, x + column_width, y + row_height),
                    glass_alpha,
                    scale,
                )
                draw.rectangle(
                    (x, y, x + column_width, y + row_height),
                    outline=GRID_COLOR,
                    width=scale,
                )
                text_x = x + cell_padding
                if column == 0 and _row_headshot(table, index, headshots) is not None:
                    _draw_headshot(
                        image,
                        headshots[_row_headshot(table, index, headshots)],
                        x + cell_padding,
                        y + (row_height - headshot_size) // 2,
                        headshot_size,
                    )
                    text_x += headshot_size + headshot_gap
                draw.text(
                    (text_x, _text_top(draw, y, row_height, cell_font)),
                    _fit_text(
                        draw,
                        cell,
                        cell_font,
                        x + column_width - cell_padding - text_x,
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


def _draw_page_background(
    canvas: Image.Image,
    left_color: str,
    right_color: str,
) -> None:
    width, height = canvas.size
    dark_left = _rgb_hex(_blend_color(left_color, "#000000", 0.76))
    dark_right = _rgb_hex(_blend_color(right_color, "#000000", 0.58))
    _draw_gradient(ImageDraw.Draw(canvas), width, height, dark_left, dark_right)

    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    left_rgb = tuple(bytes.fromhex(left_color.lstrip("#")))
    right_rgb = tuple(bytes.fromhex(right_color.lstrip("#")))
    glow_draw.ellipse(
        (-round(width * 0.35), round(height * 0.08), round(width * 0.55), height),
        fill=(*left_rgb, 68),
    )
    glow_draw.ellipse(
        (round(width * 0.52), -round(height * 0.05), round(width * 1.25), height),
        fill=(*right_rgb, 86),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=max(36, width // 12)))
    canvas.paste(glow, (0, 0), glow)

    light = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    light_draw = ImageDraw.Draw(light)
    light_draw.ellipse(
        (
            -round(width * 0.45),
            -round(height * 0.55),
            round(width * 0.70),
            round(height * 0.62),
        ),
        fill=(255, 255, 255, 54),
    )
    light = light.filter(ImageFilter.GaussianBlur(radius=max(42, width // 10)))
    canvas.paste(light, (0, 0), light)


def _draw_glass_bar(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    color: str,
    render_scale: int,
) -> None:
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    glass = Image.new("RGBA", (width, height))
    glass_draw = ImageDraw.Draw(glass)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        shade = _blend_color(color, "#ffffff", 0.13 * (1 - ratio))
        shade = _blend_color(_rgb_hex(shade), "#000000", 0.18 * ratio)
        glass_draw.line((0, y, width, y), fill=(*shade, 218))
    _draw_specular_highlights(glass, (0, 0, width, height), render_scale)
    canvas.paste(glass, (left, top), glass)


def _draw_liquid_glass_panel(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    tint: str,
    render_scale: int,
) -> None:
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    radius = 18 * render_scale
    scale = GLASS_SUPERSAMPLE

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (
            left + 3 * render_scale,
            top + 7 * render_scale,
            right + 3 * render_scale,
            bottom + 7 * render_scale,
        ),
        radius=radius,
        fill=(0, 0, 0, 115),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(12 * render_scale))
    canvas.paste(shadow, (0, 0), shadow)

    high_resolution_size = (width * scale, height * scale)
    mask = Image.new("L", high_resolution_size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale,
        fill=255,
    )
    mask = mask.resize((width, height), Image.Resampling.LANCZOS)
    refraction_inset = 4 * render_scale
    refracted = canvas.crop(
        (
            left + refraction_inset,
            top + refraction_inset,
            right - refraction_inset,
            bottom - refraction_inset,
        )
    )
    frosted = refracted.resize((width, height), Image.Resampling.LANCZOS).filter(
        ImageFilter.GaussianBlur(8 * render_scale)
    )
    canvas.paste(frosted, (left, top), mask)

    surface = Image.new("RGBA", high_resolution_size, (0, 0, 0, 0))
    surface_draw = ImageDraw.Draw(surface)
    tint_rgb = tuple(bytes.fromhex(tint.lstrip("#")))
    surface_draw.rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale,
        fill=(*tint_rgb, 26),
        outline=(255, 255, 255, 88),
        width=2 * scale,
    )
    surface_draw.rounded_rectangle(
        (3 * scale, 3 * scale, (width - 4) * scale, (height - 4) * scale),
        radius=(radius - 3) * scale,
        outline=(255, 255, 255, 28),
        width=scale,
    )

    bloom = Image.new("RGBA", high_resolution_size, (0, 0, 0, 0))
    bloom_draw = ImageDraw.Draw(bloom)
    bloom_draw.line(
        (radius * scale, scale, (width - radius) * scale, scale),
        fill=(255, 255, 255, 105),
        width=4 * scale,
    )
    bloom_draw.arc(
        (scale, scale, radius * 2 * scale, radius * 2 * scale),
        180,
        270,
        fill=(255, 255, 255, 105),
        width=4 * scale,
    )
    bloom_draw.line(
        (scale, radius * scale, scale, (height - radius) * scale),
        fill=(255, 255, 255, 70),
        width=3 * scale,
    )
    bloom = bloom.filter(ImageFilter.GaussianBlur(2.5 * scale))
    surface = Image.alpha_composite(surface, bloom)
    surface_draw = ImageDraw.Draw(surface)

    surface_draw.line(
        (radius * scale, scale, (width - radius) * scale, scale),
        fill=(255, 255, 255, 180),
        width=scale,
    )
    surface_draw.arc(
        (scale, scale, radius * 2 * scale, radius * 2 * scale),
        180,
        270,
        fill=(255, 255, 255, 180),
        width=scale,
    )
    surface_draw.line(
        (scale, radius * scale, scale, (height - radius) * scale),
        fill=(255, 255, 255, 112),
        width=scale,
    )
    surface_draw.line(
        (
            radius * scale,
            (height - 2) * scale,
            (width - radius) * scale,
            (height - 2) * scale,
        ),
        fill=(0, 0, 0, 110),
        width=2 * scale,
    )
    surface_draw.arc(
        (
            (width - radius * 2) * scale,
            (height - radius * 2) * scale,
            (width - 2) * scale,
            (height - 2) * scale,
        ),
        0,
        90,
        fill=(0, 0, 0, 110),
        width=2 * scale,
    )
    surface_draw.line(
        (
            (width - 2) * scale,
            radius * scale,
            (width - 2) * scale,
            (height - radius) * scale,
        ),
        fill=(0, 0, 0, 82),
        width=2 * scale,
    )
    surface = surface.resize((width, height), Image.Resampling.LANCZOS)
    canvas.paste(surface, (left, top), surface)


def _draw_glass_cell(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    alpha: int,
    render_scale: int,
) -> None:
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    glass = Image.new("RGBA", (width, height), (255, 255, 255, alpha))
    glass_draw = ImageDraw.Draw(glass)
    glass_draw.line(
        (0, 0, width, 0),
        fill=(255, 255, 255, 52),
        width=render_scale,
    )
    glass_draw.line(
        (0, height - 1, width, height - 1),
        fill=(0, 0, 0, 72),
        width=render_scale,
    )
    canvas.paste(glass, (left, top), glass)


def _draw_specular_highlights(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    render_scale: int,
) -> None:
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    shine = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shine_draw = ImageDraw.Draw(shine)
    for y in range(max(1, height // 2)):
        alpha = round(24 * (1 - y / max(height // 2, 1)))
        shine_draw.line((0, y, width, y), fill=(255, 255, 255, alpha))
    shine_draw.line(
        (0, 0, width, 0),
        fill=(255, 255, 255, 105),
        width=2 * render_scale,
    )
    shine_draw.line(
        (0, height - 1, width, height - 1),
        fill=(0, 0, 0, 95),
        width=2 * render_scale,
    )
    canvas.paste(shine, (left, top), shine)


def _blend_color(
    color: str,
    target: str,
    ratio: float,
) -> tuple[int, int, int]:
    start = tuple(bytes.fromhex(color.lstrip("#")))
    end = tuple(bytes.fromhex(target.lstrip("#")))
    return tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))


def _rgb_hex(color: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color)


def _draw_headshot(
    canvas: Image.Image,
    data: bytes,
    x: int,
    y: int,
    size: int,
) -> None:
    try:
        with Image.open(io.BytesIO(data)) as source:
            photo = ImageOps.fit(
                source.convert("RGBA"),
                (size, size),
                method=Image.Resampling.LANCZOS,
            )
    except (OSError, UnidentifiedImageError) as exc:
        LOGGER.warning("Could not render a player headshot: %s", exc)
        return
    rounded_mask = Image.new("L", photo.size, 0)
    ImageDraw.Draw(rounded_mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=round(size * 7 / HEADSHOT_SIZE),
        fill=255,
    )
    mask = ImageChops.multiply(photo.getchannel("A"), rounded_mask)
    canvas.paste(photo, (x, y), mask)


def _draw_logo(
    canvas: Image.Image,
    data: bytes,
    x: int,
    y: int,
    size: int,
) -> None:
    try:
        with Image.open(io.BytesIO(data)) as source:
            logo = ImageOps.contain(source.convert("RGBA"), (size, size))
    except (OSError, UnidentifiedImageError) as exc:
        LOGGER.warning("Could not render a team logo: %s", exc)
        return
    canvas.paste(logo, (x + (size - logo.width) // 2, y), logo)


def _row_headshot(
    table: InjuryTable, index: int, headshots: Mapping[str, bytes]
) -> str | None:
    """The headshot a row can actually draw, if one was downloaded for it."""
    if index >= len(table.headshots):
        return None
    url = table.headshots[index]
    return url if url and url in headshots else None


def _display_header(heading: str) -> str:
    return DISPLAY_HEADERS.get(heading.strip().upper(), heading)


def _column_widths(
    draw: ImageDraw.ImageDraw,
    tables: tuple[InjuryTable, ...],
    header_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    cell_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    headshots: Mapping[str, bytes],
    scale: int,
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
            heading = _display_header(heading)
            widths[column] = max(
                widths[column], ceil(draw.textlength(heading, font=header_font))
            )
        for index, row in enumerate(table.rows):
            for column, cell in enumerate(row[:columns]):
                text = ceil(draw.textlength(cell, font=cell_font))
                if column == 0 and _row_headshot(table, index, headshots):
                    text += (HEADSHOT_SIZE + HEADSHOT_GAP) * scale
                widths[column] = max(widths[column], text)
    widths = [
        max(width + CELL_PADDING * 2 * scale, MIN_COLUMN_WIDTH * scale)
        for width in widths
    ]
    return _fit_columns(widths, scale)


def _fit_columns(widths: list[int], scale: int) -> list[int]:
    """Columns trimmed to a width a phone shows without shrinking the type.

    Only the widest column gives room up, since it is the one holding names, and
    a name that no longer fits is shortened rather than set in smaller type.
    """
    available = (MAX_IMAGE_WIDTH - MARGIN * 2) * scale
    overflow = sum(widths) - available
    while overflow > 0:
        widest = max(range(len(widths)), key=lambda index: widths[index])
        room = widths[widest] - MIN_COLUMN_WIDTH * scale
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
