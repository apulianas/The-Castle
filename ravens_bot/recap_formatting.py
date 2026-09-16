"""Bounded plain-text sections for the postgame recap."""

from __future__ import annotations

from .recap import Efficiency, Production, RecapReport


def format_efficiency(label: str, value: Efficiency) -> str:
    if not value.measured:
        return f"{label}: unavailable ({value.plays} eligible plays)"
    return (
        f"{label}: {value.epa / value.measured:+.3f} EPA/play | "
        f"{value.successes / value.measured:.1%} success "
        f"({value.measured}/{value.plays} plays measured)"
    )


def format_production(value: Production, passing: bool) -> str:
    if (not value.attempts and not value.yards and not value.touchdowns) or not value.complete:
        return "unavailable" if not value.complete else "no recorded attempts"
    if passing:
        return (
            f"{value.completions}/{value.attempts}, {value.yards:g} gross pass yds, "
            f"{value.touchdowns} TD, {value.interceptions} INT"
        )
    return f"{value.attempts} carries, {value.yards:g} rush yds, {value.touchdowns} TD"


def recap_fields(report: RecapReport) -> list[tuple[str, str]]:
    data = report.data
    if data is None:
        return [("Not published", "NFLverse postgame play-by-play is not yet published for this game. ESPN's final score is shown above; this is not a live feed. Publication can lag the final whistle. The cache refreshes within 1 hour.")]
    fields: list[tuple[str, str]] = []
    reasons = data.partial_reasons(report.game)
    if reasons:
        fields.append(("Partial recap", "; ".join(reasons) + ". Numbers below cover only the available records."))
    fields.append(("Ravens offensive efficiency", "\n".join((
        format_efficiency("All", data.offense),
        format_efficiency("Dropbacks", data.dropbacks),
        format_efficiency("Designed runs", data.designed_rushes),
        "EPA > 0 is a success. Dropbacks include sacks/scrambles; excludes no-plays, kneels, spikes and two-point tries.",
    ))))
    production = [
        "Passing: " + format_production(data.passing, True),
        "Rushing: " + format_production(data.rushing, False),
    ]
    if data.passing.attempts and data.passing.complete and data.sacks_complete:
        production.append(f"Team net passing: {data.passing.yards + data.sack_yards:g} yds ({data.sacks} sacks)")
    else:
        production.append("Team net passing: unavailable")
    production.append("PBP-derived production includes spikes/kneels; passing yards exclude sack losses except in team net passing.")
    fields.append(("Ravens production", "\n".join(production)))
    for label, players, passing in (
        ("Passing leaders", data.passers, True), ("Rushing leaders", data.rushers, False),
    ):
        leaders = sorted(players.values(), key=lambda item: item[1].yards, reverse=True)[:3]
        fields.append((label, "\n".join(
            f"{name}: {format_production(value, passing)}" for name, value in leaders
        ) or "Player production unavailable."))
    fields.append(("Biggest win-probability swings (Ravens)", "\n\n".join(
        f"{item.wpa * 100:+.1f} percentage points | {item.clock}\n{item.description[:220]}"
        for item in data.swings
    ) or "Win-probability data unavailable."))
    fields.append(("Postgame source", "NFLverse / nflfastR batch PBP, not live data. WPA excludes kneels/spikes (not modeled). Publication may lag the final whistle; corrections can change these numbers. Cached for up to 1 hour."))
    return fields
