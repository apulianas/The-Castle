from __future__ import annotations

import hashlib
import html
import io
from dataclasses import dataclass
from html.parser import HTMLParser

import aiohttp
from PIL import Image, ImageDraw, ImageFont


INJURY_REPORT_URL = "https://www.baltimoreravens.com/team/injury-report/"
PAGE_BACKGROUND = "#f4f4f4"
RAVENS_PURPLE = "#24125f"
HEADER_BACKGROUND = "#111111"
ROW_BACKGROUND = "#ffffff"
ALT_ROW_BACKGROUND = "#ececec"
TEXT_COLOR = "#111111"


class InjuryReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class InjuryTable:
    team: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class OfficialInjuryReport:
    week: str
    tables: tuple[InjuryTable, ...]

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
        self.tables: list[InjuryTable] = []
        self._team = ""
        self._capture_team = False
        self._team_text: list[str] = []
        self._in_table = False
        self._headers: list[str] = []
        self._rows: list[tuple[str, ...]] = []
        self._cell_tag: str | None = None
        self._cell_text: list[str] = []
        self._row: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        classes = _classes(attrs)
        if tag == "option" and any(name == "selected" for name, _ in attrs):
            self._cell_tag = "option"
            self._cell_text = []
        elif tag == "span" and "nfl-o-injury-report__club-name" in classes:
            self._capture_team = True
            self._team_text = []
        elif tag == "table" and self._team:
            self._in_table = True
            self._headers = []
            self._rows = []
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag in {"th", "td"}:
            self._cell_tag = tag
            self._cell_text = []

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
            self._row = []
        elif self._in_table and tag == "table":
            if self._headers:
                self.tables.append(
                    InjuryTable(
                        team=self._team,
                        headers=tuple(self._headers),
                        rows=tuple(self._rows),
                    )
                )
            self._in_table = False


def _clean_text(parts: list[str]) -> str:
    return " ".join(html.unescape("".join(parts)).split())


def parse_injury_report(page: str) -> OfficialInjuryReport:
    parser = _ReportParser()
    parser.feed(page)
    report = OfficialInjuryReport(parser.week, tuple(parser.tables))
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


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default(size=size)


def render_injury_report(report: OfficialInjuryReport) -> bytes:
    title_font = _font(34, bold=True)
    team_font = _font(25, bold=True)
    header_font = _font(17, bold=True)
    cell_font = _font(17)
    width = 1400
    margin = 42
    title_height = 88
    team_height = 54
    row_height = 48
    table_gap = 28
    height = title_height + margin
    for table in report.tables:
        height += team_height + row_height * (len(table.rows) + 1) + table_gap

    image = Image.new("RGB", (width, height), PAGE_BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, title_height), fill=RAVENS_PURPLE)
    draw.text(
        (margin, 23),
        f"BALTIMORE RAVENS INJURY REPORT  |  {report.week.upper()}",
        fill="white",
        font=title_font,
    )

    y = title_height + 24
    for table in report.tables:
        draw.text((margin, y + 10), table.team, fill=RAVENS_PURPLE, font=team_font)
        y += team_height
        column_widths = _column_widths(table.headers, width - margin * 2)
        x = margin
        for heading, column_width in zip(table.headers, column_widths):
            draw.rectangle(
                (x, y, x + column_width, y + row_height),
                fill=HEADER_BACKGROUND,
            )
            draw.text((x + 10, y + 13), heading, fill="white", font=header_font)
            x += column_width
        y += row_height

        for index, row in enumerate(table.rows):
            x = margin
            background = ROW_BACKGROUND if index % 2 == 0 else ALT_ROW_BACKGROUND
            for cell, column_width in zip(row, column_widths):
                draw.rectangle(
                    (x, y, x + column_width, y + row_height),
                    fill=background,
                    outline="#d0d0d0",
                )
                draw.text(
                    (x + 10, y + 13),
                    _fit_text(draw, cell, cell_font, column_width - 20),
                    fill=TEXT_COLOR,
                    font=cell_font,
                )
                x += column_width
            y += row_height
        y += table_gap

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _column_widths(headers: tuple[str, ...], available: int) -> list[int]:
    if not headers:
        return []
    weights = [2.3, 0.8, 1.8, *([0.8] * max(0, len(headers) - 4)), 1.4]
    weights = weights[: len(headers)]
    if len(weights) < len(headers):
        weights.extend([1.0] * (len(headers) - len(weights)))
    unit = available / sum(weights)
    widths = [round(weight * unit) for weight in weights]
    widths[-1] += available - sum(widths)
    return widths


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
