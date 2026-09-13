from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Iterable, Mapping, Sequence

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

from .models import RAVENS_NAME, TeamRef


LOGGER = logging.getLogger(__name__)
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


@dataclass(frozen=True)
class ChartSection:
    """One team's block of the chart: a heading, a logo, and a table."""

    title: str
    color: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    headshots: tuple[str | None, ...] = ()
    logo_url: str | None = None


class ArtworkLoader:
    """Images for a chart, downloaded once and kept for later renders."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._cache: dict[str, bytes] = {}

    async def fetch(self, urls: Iterable[str | None]) -> dict[str, bytes]:
        wanted = {url for url in urls if url}
        pending = sorted(url for url in wanted if url not in self._cache)
        if pending:
            results = await asyncio.gather(
                *(self._fetch(url) for url in pending)
            )
            for url, image in zip(pending, results):
                if image is not None:
                    self._cache[url] = image
        return {url: self._cache[url] for url in wanted if url in self._cache}

    async def _fetch(self, url: str) -> bytes | None:
        try:
            async with self._session.get(url) as response:
                response.raise_for_status()
                if not response.content_type.startswith("image/"):
                    LOGGER.warning("Ignoring non-image artwork from %s", url)
                    return None
                return await response.read()
        except aiohttp.ClientError as exc:
            LOGGER.warning("Chart artwork could not be fetched from %s: %s", url, exc)
            return None


def team_color(team: TeamRef) -> str:
    if team.name == RAVENS_NAME:
        return RAVENS_PURPLE
    return team.color or NFL_PRIMARY_COLORS.get(
        team.abbreviation or "", HEADER_BACKGROUND
    )


def render_chart(
    sections: Iterable[ChartSection],
    artwork: Mapping[str, bytes] | None = None,
    left_color: str = RAVENS_PURPLE,
    right_color: str = RAVENS_PURPLE,
) -> bytes:
    """A table per team as a chart, sized so a phone can read it without zooming.

    The chart is read on a screen a few inches wide, so the type is set large
    and every column is only as wide as the longest thing in it. A fixed width
    stretched the practice-status columns — which never hold more than a dash or
    a letter — across half the image and left the type small to fit.
    """
    sections = tuple(sections)
    headshots = artwork or {}
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
        measure, sections, header_font, cell_font, headshots, scale
    )
    width = (
        margin * 2 + sum(column_widths)
        if column_widths
        else MIN_IMAGE_WIDTH * scale
    )
    width = max(width, MIN_IMAGE_WIDTH * scale)
    height = margin
    for table in sections:
        height += (
            team_height + row_height * _drawn_rows(table) + table_gap
        )

    image = Image.new("RGB", (width, height), PAGE_BACKGROUND)
    _draw_page_background(image, left_color, right_color)
    draw = ImageDraw.Draw(image)

    y = margin
    for table in sections:
        team_color = table.color
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
                y + team_height + row_height * _drawn_rows(table) + 8 * scale,
            ),
            team_color,
            scale,
        )
        team_text_x = margin
        logo_url = table.logo_url
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
            table.title,
            fill=team_text_color,
            font=team_font,
        )
        y += team_height
        if table.headers:
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
                    (
                        x + cell_padding,
                        _text_top(draw, y, row_height, header_font),
                    ),
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
    table: ChartSection, index: int, headshots: Mapping[str, bytes]
) -> str | None:
    """The headshot a row can actually draw, if one was downloaded for it."""
    if index >= len(table.headshots):
        return None
    url = table.headshots[index]
    return url if url and url in headshots else None


def _drawn_rows(section: ChartSection) -> int:
    """Rows the section takes up, counting its headings when it has any."""
    return len(section.rows) + (1 if section.headers else 0)


def _display_header(heading: str) -> str:
    return DISPLAY_HEADERS.get(heading.strip().upper(), heading)


def _column_widths(
    draw: ImageDraw.ImageDraw,
    tables: Sequence[ChartSection],
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
    columns = max(
        (
            max((len(table.headers), *(len(row) for row in table.rows)), default=0)
            for table in tables
        ),
        default=0,
    )
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
