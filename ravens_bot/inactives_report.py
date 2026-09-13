from __future__ import annotations

from typing import Mapping

from .chart import (
    HEADER_BACKGROUND,
    RAVENS_PURPLE,
    ChartSection,
    render_chart,
    team_color,
)
from .espn_urls import HEADSHOT_FEATURE_WIDTH, headshot_url
from .models import GameTeam, InactivePlayer, InactiveReport


NO_INACTIVES_ROW = "None listed"
MISSING_REASON = "-"


def render_inactive_report(
    report: InactiveReport,
    artwork: Mapping[str, bytes] | None = None,
) -> bytes:
    """Both clubs' inactive lists as a chart in the injury report's style."""
    sections = chart_sections(report)
    away, home = _sides(report)
    return render_chart(
        sections,
        artwork,
        team_color(away.team) if away else RAVENS_PURPLE,
        team_color(home.team) if home else RAVENS_PURPLE,
    )


def chart_sections(report: InactiveReport) -> tuple[ChartSection, ...]:
    """A section per club, away first, so the chart reads like the matchup.

    Each row is a headshot, a position, and a name in one column so the chart
    stays as narrow as a phone screen, with the reason alongside; the columns
    carry no headings because a name and a reason need no labelling.
    """
    sections: list[ChartSection] = []
    claimed: set[int] = set()
    for side in _sides(report):
        if side is None:
            continue
        players = _players_for(report, side, claimed)
        sections.append(
            ChartSection(
                title=side.team.name,
                color=team_color(side.team),
                headers=(),
                rows=_rows(players),
                headshots=_headshots(players),
                logo_url=side.team.logo_url,
            )
        )
    for team, players in _unclaimed(report, claimed).items():
        sections.append(
            ChartSection(
                title=team,
                color=HEADER_BACKGROUND,
                headers=(),
                rows=_rows(players),
                headshots=_headshots(players),
            )
        )
    return tuple(sections)


def artwork_urls(report: InactiveReport) -> list[str]:
    """Every logo and headshot the chart would draw, in a stable order."""
    urls: list[str] = []
    for section in chart_sections(report):
        for url in (section.logo_url, *section.headshots):
            if url and url not in urls:
                urls.append(url)
    return urls


def has_players(report: InactiveReport) -> bool:
    return bool(report.players)


def _sides(report: InactiveReport) -> tuple[GameTeam | None, GameTeam | None]:
    return report.game.away, report.game.home


def _players_for(
    report: InactiveReport, side: GameTeam, claimed: set[int]
) -> list[InactivePlayer]:
    players: list[InactivePlayer] = []
    for index, player in enumerate(report.players):
        if index in claimed:
            continue
        if _matches(player, side):
            claimed.add(index)
            players.append(player)
    return players


def _unclaimed(
    report: InactiveReport, claimed: set[int]
) -> dict[str, list[InactivePlayer]]:
    """Players ESPN named under a club the game did not identify."""
    leftovers: dict[str, list[InactivePlayer]] = {}
    for index, player in enumerate(report.players):
        if index in claimed:
            continue
        leftovers.setdefault(player.team or "Inactive", []).append(player)
    return leftovers


def _matches(player: InactivePlayer, side: GameTeam) -> bool:
    if player.is_ravens and side.team.is_ravens:
        return True
    team = (player.team or "").strip().casefold()
    if not team:
        return False
    candidates = {
        value.strip().casefold()
        for value in (side.team.name, side.team.abbreviation, side.team.short_name)
        if value
    }
    return team in candidates


def _rows(players: list[InactivePlayer]) -> tuple[tuple[str, ...], ...]:
    if not players:
        return ((NO_INACTIVES_ROW, MISSING_REASON),)
    return tuple(
        (_player_text(player), player.reason or MISSING_REASON) for player in players
    )


def _headshots(players: list[InactivePlayer]) -> tuple[str | None, ...]:
    if not players:
        return (None,)
    return tuple(
        headshot_url(player.athlete_id, HEADSHOT_FEATURE_WIDTH) for player in players
    )


def _player_text(player: InactivePlayer) -> str:
    if player.position:
        return f"{player.position} {player.name}"
    return player.name
