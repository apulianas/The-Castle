"""Every fourth down of a game as a chart, in the injury report's style.

The live ``/fourthdown`` command answers one down while it is on the screen.
This is the argument afterwards: the whole game's fourth downs, both clubs side
by side, with what the model would have done next to what the coach did. Each
row is numbered per club so a follow-up can name one — "BAL 3" — without
quoting a clock reading back at the bot.
"""

from __future__ import annotations

from typing import Mapping

from .chart import (
    HEADER_BACKGROUND,
    RAVENS_PURPLE,
    ChartSection,
    render_chart,
    team_color,
)
from .espn import team_matches
from .fourthdown import advise
from .models import (
    FourthDownGameReport,
    FourthDownPlay,
    GameTeam,
    TeamRef,
    down_text,
)


FOURTH_DOWN_CHART_FILENAME = "ravens-fourth-downs.png"
# No team has faced more fourth downs than this in a game, so a larger number
# names no row of the chart.
MAX_FOURTH_DOWN_INSTANCE = 30
CHART_HEADERS = ("#", "Drive", "Situation", "Recommendation", "Actual")
NO_FOURTH_DOWNS_ROW = "No fourth downs"
MISSING = "-"


def render_fourth_down_chart(
    report: FourthDownGameReport,
    artwork: Mapping[str, bytes] | None = None,
) -> bytes:
    """Both clubs' fourth downs as a chart, away side first."""
    away, home = report.game.away, report.game.home
    return render_chart(
        chart_sections(report),
        artwork,
        team_color(away.team) if away else RAVENS_PURPLE,
        team_color(home.team) if home else RAVENS_PURPLE,
    )


def chart_sections(report: FourthDownGameReport) -> tuple[ChartSection, ...]:
    """A section per club, so the chart reads like the matchup it came from."""
    sections: list[ChartSection] = []
    for side in _sides(report):
        if side is None:
            continue
        sections.append(
            ChartSection(
                title=side.team.name,
                color=team_color(side.team),
                headers=CHART_HEADERS,
                rows=_rows(report.for_team(side.team)),
                logo_url=side.team.logo_url,
                wrap_cells=True,
            )
        )
    if sections:
        return tuple(sections)
    # A game whose competitors ESPN did not name still has fourth downs worth
    # showing, grouped by whoever the plays said had the ball.
    grouped: dict[str, list[FourthDownPlay]] = {}
    for play in report.plays:
        grouped.setdefault(play.team.name, []).append(play)
    return tuple(
        ChartSection(
            title=team,
            color=HEADER_BACKGROUND,
            headers=CHART_HEADERS,
            rows=_rows(tuple(plays)),
            wrap_cells=True,
        )
        for team, plays in grouped.items()
    )


def artwork_urls(report: FourthDownGameReport) -> list[str]:
    urls: list[str] = []
    for section in chart_sections(report):
        if section.logo_url and section.logo_url not in urls:
            urls.append(section.logo_url)
    return urls


def find_team(report: FourthDownGameReport, query: str) -> TeamRef | None:
    """The club a person named, matched against either side of this game."""
    for side in report.game.teams:
        if team_matches(side.team, query):
            return side.team
    for play in report.plays:
        if team_matches(play.team, query):
            return play.team
    return None


def team_names(report: FourthDownGameReport) -> list[str]:
    names = [side.team.short_name for side in report.game.teams]
    if names:
        return names
    return sorted({play.team.short_name for play in report.plays})


def _sides(report: FourthDownGameReport) -> tuple[GameTeam | None, GameTeam | None]:
    return report.game.away, report.game.home


def _rows(plays: tuple[FourthDownPlay, ...]) -> tuple[tuple[str, ...], ...]:
    if not plays:
        return ((MISSING, MISSING, NO_FOURTH_DOWNS_ROW, MISSING, MISSING),)
    return tuple(
        (
            str(play.instance),
            str(play.drive),
            situation_text(play),
            recommendation_text(play),
            play.actual,
        )
        for play in plays
    )


def situation_text(play: FourthDownPlay) -> str:
    """Clock, down and distance, and the ball spot, in that reading order."""
    situation = play.situation
    parts = [
        text
        for text in (
            situation.clock_text,
            situation.down_distance or down_text(4),
            f"at {situation.spot}" if situation.spot else None,
        )
        if text
    ]
    return " ".join(parts) or MISSING


def recommendation_text(play: FourthDownPlay) -> str:
    """The model's call, hedged when its top two options are a coin flip."""
    advice = advise(play.situation)
    best = advice.best
    if best is None:
        return MISSING
    if advice.is_close:
        return f"{best.label} (close)"
    return best.label


def agreed(play: FourthDownPlay) -> bool | None:
    """Whether the coach and the model chose the same thing, when both spoke."""
    advice = advise(play.situation)
    if advice.best is None:
        return None
    return advice.best.kind == play.choice
