"""Snap counts for Ravens games.

Snap counts originate in the NFL's GSIS game book, whose player participation
page is published per game at
``https://nflgsis.com/{season}/{Reg|Post}/{week:02d}/{gamekey}/Gamebook.pdf``.
That file is a PDF keyed by a GSIS game key that ESPN never exposes, and its
participation page has no stable machine-readable layout, so reading it would
mean shipping a PDF text extractor and a mapping table for the game key, then
re-deriving the percentages by hand. The sibling ``Gamebook.xml`` is no help:
it lists starters, substitutions, and inactives, but carries no snap totals.

nflverse publishes the same game book participation numbers as a per-season CSV
keyed by season, week, and team, which is the form this module reads. It needs
no PDF dependency, no GSIS game key, and it carries the unit percentages the
game book prints alongside the counts.
"""

from __future__ import annotations

import csv
from collections import Counter
from io import StringIO
from typing import Any, Iterable

import aiohttp

from .cache import AsyncTtlCache
from .espn import normalize_name
from .models import (
    DEFENSE,
    OFFENSE,
    RAVENS_ABBREVIATION,
    SNAP_UNITS,
    SPECIAL_TEAMS,
    Game,
    PlayerRef,
    PlayerSnaps,
    PlayerSnapTotals,
    SnapCountReport,
)


SNAP_COUNTS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/"
    "snap_counts_{season}.csv"
)
# A finished game's snaps never change, so the season file is held far longer
# than the live ESPN endpoints; the tail of the file grows once a week.
SNAP_COUNTS_TTL_SECONDS = 21600.0
# Team codes where the snap count file and ESPN disagree.
TEAM_CODE_ALIASES = {"LAR": "LA", "WSH": "WAS", "LVR": "LV", "JAC": "JAX"}
REGULAR_SEASON_TYPE = "REG"
# A season is seventeen games plus up to four in the playoffs, and a request
# made early in one reaches back into the season before it, so a snap count
# request can cover two full seasons rather than a single regular season.
MAX_SNAP_GAMES = 42


class SnapCountError(RuntimeError):
    """Raised when the snap count source cannot be read."""


def team_code(value: str | None) -> str | None:
    """A team abbreviation in the form the snap count file uses."""
    text = (value or "").strip().upper()
    if not text:
        return None
    return TEAM_CODE_ALIASES.get(text, text)


def _as_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _unit_total(measurements: list[tuple[int, float | None]]) -> int:
    """The team's snaps for a unit, recovered from each player's count and share.

    The file states a share rather than the denominator, so the denominator is
    rebuilt per player and the value most players agree on is used. Rounding
    makes a lone player's share unreliable; agreement across a unit is not.
    """
    candidates = Counter(
        round(snaps / share)
        for snaps, share in measurements
        if snaps > 0 and share is not None and share > 0
    )
    if candidates:
        best = max(candidates.items(), key=lambda item: (item[1], item[0]))
        return best[0]
    return max((snaps for snaps, _ in measurements), default=0)


class GameSnaps:
    """One team's snap counts for one game, as published for that season."""

    __slots__ = ("game_id", "season", "week", "game_type", "team", "opponent", "players", "totals")

    def __init__(
        self,
        game_id: str,
        season: int,
        week: int,
        game_type: str,
        team: str,
        opponent: str,
        players: tuple[PlayerSnaps, ...],
        totals: dict[str, int],
    ) -> None:
        self.game_id = game_id
        self.season = season
        self.week = week
        self.game_type = game_type
        self.team = team
        self.opponent = opponent
        self.players = players
        self.totals = totals

    @property
    def is_regular_season(self) -> bool:
        return self.game_type.upper() == REGULAR_SEASON_TYPE

    @property
    def is_home(self) -> bool:
        """Home teams are named last in the game id, e.g. ``2024_02_LV_BAL``."""
        return self.game_id.rsplit("_", 1)[-1].upper() == self.team


def parse_snap_counts(
    csv_text: str, team: str = RAVENS_ABBREVIATION
) -> dict[str, GameSnaps]:
    """Snap counts for one team, keyed by the file's game id.

    A layout change upstream should leave the bot saying no snaps are published
    rather than raising, so unreadable rows are skipped instead of rejected.
    """
    wanted = team_code(team)
    rows: dict[str, list[dict[str, str]]] = {}
    reader = csv.DictReader(StringIO(csv_text))
    for row in reader:
        if team_code(row.get("team")) != wanted:
            continue
        game_id = (row.get("game_id") or "").strip()
        name = (row.get("player") or "").strip()
        if not game_id or not name:
            continue
        rows.setdefault(game_id, []).append(row)

    games: dict[str, GameSnaps] = {}
    for game_id, entries in rows.items():
        players = tuple(
            PlayerSnaps(
                player=PlayerRef(
                    name=(row.get("player") or "").strip(),
                    position=(row.get("position") or "").strip() or None,
                ),
                offense=_as_int(row.get("offense_snaps")),
                defense=_as_int(row.get("defense_snaps")),
                special_teams=_as_int(row.get("st_snaps")),
            )
            for row in entries
        )
        totals = {
            OFFENSE: _unit_total(
                [
                    (_as_int(row.get("offense_snaps")), _as_float(row.get("offense_pct")))
                    for row in entries
                ]
            ),
            DEFENSE: _unit_total(
                [
                    (_as_int(row.get("defense_snaps")), _as_float(row.get("defense_pct")))
                    for row in entries
                ]
            ),
            SPECIAL_TEAMS: _unit_total(
                [(_as_int(row.get("st_snaps")), _as_float(row.get("st_pct"))) for row in entries]
            ),
        }
        first = entries[0]
        games[game_id] = GameSnaps(
            game_id=game_id,
            season=_as_int(first.get("season")),
            week=_as_int(first.get("week")),
            game_type=(first.get("game_type") or "").strip(),
            team=wanted or "",
            opponent=team_code(first.get("opponent")) or "",
            players=players,
            totals=totals,
        )
    return games


def match_game(games: dict[str, GameSnaps], game: Game) -> GameSnaps | None:
    """The snap count entry for an ESPN game.

    Season, opponent, and home or away identify a game everywhere except a
    playoff rematch of a game with the same host, which the regular season flag
    separates.
    """
    opponent = game.opponent
    ravens = game.ravens
    if opponent is None or ravens is None or game.season is None:
        return None
    wanted_opponent = team_code(opponent.team.abbreviation)
    if wanted_opponent is None:
        return None
    postseason = game.season_type == 3
    for entry in games.values():
        if entry.season != game.season or entry.opponent != wanted_opponent:
            continue
        if entry.is_home != ravens.is_home:
            continue
        if entry.is_regular_season == postseason:
            continue
        return entry
    return None


def build_report(
    game: Game, snaps: GameSnaps, roster: dict[str, PlayerRef] | None = None
) -> SnapCountReport:
    """A report for one game, with roster art and links applied where known."""
    players = tuple(_resolve(entry, roster or {}) for entry in snaps.players)
    return SnapCountReport(
        game=game,
        players=players,
        offense_total=snaps.totals.get(OFFENSE, 0),
        defense_total=snaps.totals.get(DEFENSE, 0),
        special_teams_total=snaps.totals.get(SPECIAL_TEAMS, 0),
    )


def _resolve(entry: PlayerSnaps, roster: dict[str, PlayerRef]) -> PlayerSnaps:
    match = roster.get(normalize_name(entry.player.name))
    if match is None:
        return entry
    return PlayerSnaps(
        player=PlayerRef(
            # The snap count file and the roster spell some names differently;
            # the file's spelling is what the report was built from.
            name=entry.player.name,
            athlete_id=match.athlete_id,
            position=entry.player.position or match.position,
            headshot=match.headshot,
            link=match.link,
        ),
        offense=entry.offense,
        defense=entry.defense,
        special_teams=entry.special_teams,
    )


def aggregate(reports: Iterable[SnapCountReport]) -> list[PlayerSnapTotals]:
    """Per-player totals across several games, most snaps first."""
    all_reports = list(reports)
    ordered: list[str] = []
    collected: dict[str, list[tuple[Game, PlayerSnaps]]] = {}
    for report in all_reports:
        for entry in report.players:
            key = normalize_name(entry.player.name)
            if key not in collected:
                collected[key] = []
                ordered.append(key)
            collected[key].append((report.game, entry))

    totals: list[PlayerSnapTotals] = []
    for key in ordered:
        entries = collected[key]
        best = max(entries, key=lambda item: item[1].total)[1].player
        # A player's share is measured only over the games they were part of,
        # so a mid-season signing is not diluted by games before they arrived.
        played = _reports_for(all_reports, entries)
        totals.append(
            PlayerSnapTotals(
                player=best,
                entries=tuple(entries),
                offense=sum(entry.offense for _, entry in entries),
                defense=sum(entry.defense for _, entry in entries),
                special_teams=sum(entry.special_teams for _, entry in entries),
                offense_total=sum(report.offense_total for report in played),
                defense_total=sum(report.defense_total for report in played),
                special_teams_total=sum(
                    report.special_teams_total for report in played
                ),
            )
        )
    totals.sort(
        key=lambda item: (
            -(item.offense + item.defense + item.special_teams),
            item.player.name,
        )
    )
    return totals


def _reports_for(
    reports: list[SnapCountReport], entries: list[tuple[Game, PlayerSnaps]]
) -> list[SnapCountReport]:
    """The reports a player appeared in, so their share uses only those games."""
    played = {game.event_id for game, _ in entries}
    return [report for report in reports if report.game.event_id in played]


def match_players(totals: Iterable[PlayerSnapTotals], query: str) -> list[PlayerSnapTotals]:
    """Players whose name matches a search, exact matches first."""
    wanted = normalize_name(query)
    if not wanted:
        return []
    items = list(totals)
    exact = [item for item in items if normalize_name(item.player.name) == wanted]
    if exact:
        return exact
    return [
        item
        for item in items
        if wanted in normalize_name(item.player.name)
        or normalize_name(item.player.name).endswith(f" {wanted}")
    ]


class SnapCountClient:
    """Reads the published snap counts for a season and matches them to games."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._cache: AsyncTtlCache[int, dict[str, GameSnaps]] = AsyncTtlCache(
            SNAP_COUNTS_TTL_SECONDS, max_entries=8
        )

    async def _csv(self, url: str) -> str:
        try:
            async with self.session.get(url, timeout=30) as response:
                if response.status == 404:
                    # A season with no published file yet is an empty season,
                    # not an outage.
                    return ""
                if response.status >= 400:
                    raise SnapCountError(
                        f"Snap count data returned HTTP {response.status}"
                    )
                return await response.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SnapCountError(f"Could not reach the snap count data: {exc}") from exc

    async def fetch_season(self, season: int) -> dict[str, GameSnaps]:
        async def load() -> dict[str, GameSnaps]:
            text = await self._csv(SNAP_COUNTS_URL.format(season=season))
            if not text.strip():
                return {}
            return parse_snap_counts(text)

        return await self._cache.get_or_fetch(season, load)

    async def fetch_reports(
        self, games: Iterable[Game], roster: dict[str, PlayerRef] | None = None
    ) -> list[SnapCountReport]:
        """Reports for the games whose snaps have been published."""
        reports: list[SnapCountReport] = []
        seasons: dict[int, dict[str, GameSnaps]] = {}
        for game in games:
            if game.season is None:
                continue
            if game.season not in seasons:
                seasons[game.season] = await self.fetch_season(game.season)
            snaps = match_game(seasons[game.season], game)
            if snaps is None or not snaps.players:
                continue
            reports.append(build_report(game, snaps, roster))
        return reports


__all__ = [
    "SNAP_COUNTS_TTL_SECONDS",
    "SNAP_COUNTS_URL",
    "GameSnaps",
    "SnapCountClient",
    "SnapCountError",
    "aggregate",
    "build_report",
    "match_game",
    "match_players",
    "parse_snap_counts",
    "team_code",
    "SNAP_UNITS",
]
