from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from .chart import ChartSection, RAVENS_PURPLE, render_chart, team_color
from .models import LiveGameReport, PlayerGameStats, TeamRef


LIVE_CHART_FILENAME = "ravens-live-stats.png"
KEY_TEAM_STATS = ("Total Yards", "Turnovers", "3rd down efficiency", "Possession")
ROWS_PER_PAGE = 12


def chart_sections(
    report: LiveGameReport, *, all_stats: bool = False
) -> tuple[ChartSection, ...]:
    """Player leaders first, Ravens first, followed by a small team comparison."""
    groups: dict[tuple[TeamRef | None, str], list[PlayerGameStats]] = {}
    players = report.players if all_stats and report.players else report.leaders
    for line in sorted(players, key=lambda line: not line.is_ravens):
        category = line.category if all_stats else "player leaders"
        groups.setdefault((line.team, category), []).append(line)
    sections = [
        ChartSection(
            title=f"{team.short_name} {category}" if team else category.capitalize(),
            color=team_color(team) if team else RAVENS_PURPLE,
            headers=("Player", "Stats"),
            rows=tuple(
                (line.player.name, f"{line.category}: {line.detail}") for line in lines
            ),
            headshots=tuple(line.player.photo_url() for line in lines),
            logo_url=team.logo_url if team else None,
            wrap_cells=True,
        )
        for (team, category), lines in groups.items()
    ]
    teams = sorted(report.teams, key=lambda entry: not entry.is_ravens)
    if teams:
        labels = list(report.stat_labels)
        preferred = [
            label for wanted in KEY_TEAM_STATS for label in labels
            if label.casefold() == wanted.casefold()
        ]
        chosen = (preferred + [label for label in labels if label not in preferred])[:4]
        rows = tuple(
            (label, " | ".join(entry.value(label) or "-" for entry in teams))
            for label in chosen
        )
        if all_stats and len(teams) == 2 and any(entry.value("Turnovers") for entry in teams):
            rows += ((
                "Takeaways",
                " | ".join(entry.value("Turnovers") or "-" for entry in reversed(teams)),
            ),)
        sections.append(
            ChartSection(
                title="Team snapshot",
                color=RAVENS_PURPLE,
                headers=("Stat", " | ".join(entry.team.short_name for entry in teams)),
                rows=rows,
                wrap_cells=True,
            )
        )
    return tuple(sections)


def artwork_urls(report: LiveGameReport, *, all_stats: bool = False) -> list[str]:
    return list(dict.fromkeys(
        url for section in chart_sections(report, all_stats=all_stats)
        for url in (section.logo_url, *section.headshots) if url
    ))


def chart_pages(report: LiveGameReport) -> tuple[tuple[ChartSection, ...], ...]:
    """Bound expanded graphics by rows, repeating headings on continuations."""
    pages: list[tuple[ChartSection, ...]] = []
    current: list[ChartSection] = []
    used = 0
    for section in chart_sections(report, all_stats=True):
        offset = 0
        while offset < len(section.rows):
            if used == ROWS_PER_PAGE:
                pages.append(tuple(current))
                current = []
                used = 0
            # Count headings too, so many small categories stay readable.
            if ROWS_PER_PAGE - used < 3:
                pages.append(tuple(current))
                current = []
                used = 0
            count = min(len(section.rows) - offset, ROWS_PER_PAGE - used - 2)
            current.append(replace(
                section,
                title=section.title + (" (cont.)" if offset else ""),
                rows=section.rows[offset:offset + count],
                headshots=section.headshots[offset:offset + count],
            ))
            used += count + 2
            offset += count
    if current:
        pages.append(tuple(current))
    return tuple(pages)


def expanded_stats_text(report: LiveGameReport) -> str:
    return "\n\n".join(
        "\n".join((
            section.title,
            " | ".join(section.headers),
            *(" | ".join(row) for row in section.rows),
        ))
        for section in chart_sections(report, all_stats=True)
    )


def render_live_pages(
    report: LiveGameReport, artwork: Mapping[str, bytes] | None = None
) -> tuple[bytes, ...]:
    opponent = report.game.opponent
    return tuple(
        render_chart(
            page, artwork, RAVENS_PURPLE,
            team_color(opponent.team) if opponent else RAVENS_PURPLE,
        )
        for page in chart_pages(report)
    )


def render_live_report(
    report: LiveGameReport, artwork: Mapping[str, bytes] | None = None
) -> bytes:
    opponent = report.game.opponent
    return render_chart(
        chart_sections(report),
        artwork,
        RAVENS_PURPLE,
        team_color(opponent.team) if opponent else RAVENS_PURPLE,
    )
