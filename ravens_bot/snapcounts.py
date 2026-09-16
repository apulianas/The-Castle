"""Pro Football Reference snap counts and player IDs, published by nflverse."""

from __future__ import annotations

import csv
import logging
import math
from collections import Counter
from dataclasses import dataclass, replace
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
# Published games can receive corrections; new games are added during the week.
SNAP_COUNTS_TTL_SECONDS = 21600.0
PLAYERS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
)
PLAYERS_TTL_SECONDS = 86400.0
LOGGER = logging.getLogger(__name__)
# Team codes where the snap count file and ESPN disagree.
TEAM_CODE_ALIASES = {"LAR": "LA", "WSH": "WAS", "LVR": "LV", "JAC": "JAX"}
REGULAR_SEASON_TYPE = "REG"
# A season is seventeen games plus up to four in the playoffs, and a request
# made early in one reaches back into the season before it, so a snap count
# request can cover two full seasons rather than a single regular season.
MAX_SNAP_GAMES = 42


class SnapCountError(RuntimeError):
    """Raised when the snap count source cannot be read."""


class SnapCountUnavailable(SnapCountError):
    """An upstream transport failure, distinct from a malformed data contract."""


@dataclass(frozen=True)
class PlayerCrosswalk:
    by_pfr: dict[str, PlayerRef]
    by_name: dict[str, tuple[str, ...]]


def _reader(text: str, required: set[str], source: str) -> csv.DictReader:
    reader = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise SnapCountError(f"{source} CSV missing required columns: {', '.join(sorted(missing))}")
    return reader


def parse_players(text: str) -> PlayerCrosswalk:
    reader = _reader(text, {"pfr_id", "espn_id", "display_name"}, "Players")
    by_pfr: dict[str, PlayerRef] = {}
    names: dict[str, set[str]] = {}
    for row in reader:
        pfr_id = (row.get("pfr_id") or "").strip()
        if not pfr_id:
            continue
        name = (row.get("display_name") or "").strip()
        espn_id = (row.get("espn_id") or "").strip() or None
        if espn_id is not None and (not espn_id.isascii() or not espn_id.isdigit()):
            raise SnapCountError(f"Players CSV has invalid ESPN ID for {pfr_id}")
        if not name:
            raise SnapCountError(f"Players CSV has no display name for {pfr_id}")
        player = PlayerRef(name=name, athlete_id=espn_id, position=row.get("position") or None)
        if pfr_id in by_pfr and by_pfr[pfr_id] != player:
            raise SnapCountError(f"Players CSV has conflicting records for {pfr_id}")
        by_pfr[pfr_id] = player
        names.setdefault(normalize_name(name), set()).add(pfr_id)
    return PlayerCrosswalk(by_pfr, {name: tuple(sorted(ids)) for name, ids in names.items()})


def team_code(value: str | None) -> str | None:
    """A team abbreviation in the form the snap count file uses."""
    text = (value or "").strip().upper()
    if not text:
        return None
    return TEAM_CODE_ALIASES.get(text, text)


def _as_int(value: Any) -> int:
    try:
        number = float(str(value).strip())
        if not math.isfinite(number) or number < 0 or not number.is_integer():
            raise ValueError
        return int(number)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SnapCountError(f"Snap count CSV has invalid count: {value!r}") from exc


def _as_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(str(value).strip())
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError
        return number
    except (TypeError, ValueError) as exc:
        raise SnapCountError(f"Snap count CSV has invalid share: {value!r}") from exc


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
    return 0


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

    Malformed schemas and measurements are source errors, not unpublished games.
    """
    wanted = team_code(team)
    rows: dict[str, list[dict[str, str]]] = {}
    reader = _reader(
        csv_text,
        {"game_id", "season", "week", "game_type", "team", "opponent", "player",
         "pfr_player_id", "position", "offense_snaps", "offense_pct",
         "defense_snaps", "defense_pct", "st_snaps", "st_pct"},
        "Snap count",
    )
    for row in reader:
        if team_code(row.get("team")) != wanted:
            continue
        game_id = (row.get("game_id") or "").strip()
        name = (row.get("player") or "").strip()
        if not game_id or not name:
            raise SnapCountError("Snap count CSV has a Ravens row without a game or player")
        if not (row.get("opponent") or "").strip() or not (row.get("game_type") or "").strip():
            raise SnapCountError("Snap count CSV has incomplete game metadata")
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
                pfr_id=(row.get("pfr_player_id") or "").strip() or None,
                offense_share=_as_float(row.get("offense_pct")),
                defense_share=_as_float(row.get("defense_pct")),
                special_teams_share=_as_float(row.get("st_pct")),
            )
            for row in entries
        )
        identities = [player.identity for player in players]
        if len(identities) != len(set(identities)):
            raise SnapCountError(f"Snap count CSV has duplicate players in {game_id}")
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
        for row in entries:
            if any(row.get(key) != first.get(key) for key in ("season", "week", "game_type", "opponent")):
                raise SnapCountError(f"Snap count CSV has conflicting game metadata for {game_id}")
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
    if opponent is None or ravens is None or game.season is None or game.season_type not in (2, 3):
        return None
    wanted_opponent = team_code(opponent.team.abbreviation)
    if wanted_opponent is None:
        return None
    postseason = game.season_type == 3
    matches = []
    for entry in games.values():
        if entry.season != game.season or entry.opponent != wanted_opponent:
            continue
        if entry.is_home != ravens.is_home:
            continue
        if entry.is_regular_season == postseason:
            continue
        matches.append(entry)
    if len(matches) > 1:
        raise SnapCountError(f"Multiple snap count games match ESPN event {game.event_id}")
    return matches[0] if matches else None


def build_report(
    game: Game, snaps: GameSnaps, roster: dict[str, PlayerRef] | None = None,
    crosswalk: PlayerCrosswalk | None = None,
) -> SnapCountReport:
    """A report for one game, with roster art and links applied where known."""
    players = tuple(_resolve(entry, roster or {}, crosswalk) for entry in snaps.players)
    return SnapCountReport(
        game=game,
        players=players,
        offense_total=snaps.totals.get(OFFENSE, 0),
        defense_total=snaps.totals.get(DEFENSE, 0),
        special_teams_total=snaps.totals.get(SPECIAL_TEAMS, 0),
    )


def _resolve(
    entry: PlayerSnaps, roster: dict[str, PlayerRef],
    crosswalk: PlayerCrosswalk | None = None,
) -> PlayerSnaps:
    pfr_id = entry.pfr_id
    match = crosswalk.by_pfr.get(pfr_id) if crosswalk and pfr_id else None
    name = normalize_name(entry.name)
    if match is None and crosswalk:
        candidates = crosswalk.by_name.get(name, ())
        # A name must never override a known, conflicting PFR identity.
        if len(candidates) > 1 or (pfr_id and candidates and pfr_id not in candidates):
            return entry
        if len(candidates) == 1:
            pfr_id = candidates[0]
            match = crosswalk.by_pfr[pfr_id]
    if match is not None and match.athlete_id:
        art = next((player for player in roster.values() if player.athlete_id == match.athlete_id), match)
        match = replace(match, headshot=art.headshot, link=art.link)
    else:
        if crosswalk and len(crosswalk.by_name.get(name, ())) > 1:
            return replace(entry, pfr_id=pfr_id)
        candidates = [player for player in roster.values() if normalize_name(player.name) == name]
        unique = {player.athlete_id: player for player in candidates}
        if len(unique) == 1:
            match = next(iter(unique.values()))
    if match is None:
        return replace(entry, pfr_id=pfr_id)
    return replace(
        entry,
        pfr_id=pfr_id,
        player=PlayerRef(
            # The snap count file and the roster spell some names differently;
            # the file's spelling is what the report was built from.
            name=entry.player.name,
            athlete_id=match.athlete_id,
            position=entry.player.position or match.position,
            headshot=match.headshot,
            link=match.link,
        ),
    )


def aggregate(reports: Iterable[SnapCountReport]) -> list[PlayerSnapTotals]:
    """Per-player totals across several games, most snaps first."""
    all_reports = list(reports)
    ordered: list[str] = []
    collected: dict[str, list[tuple[Game, PlayerSnaps]]] = {}
    for report in all_reports:
        for entry in report.players:
            key = entry.identity
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
        denominators = {
            unit: sum(report.total(unit) for report in played)
            if all(report.total(unit) > 0 for report in played) else 0
            for unit in SNAP_UNITS
        }
        totals.append(
            PlayerSnapTotals(
                player=best,
                entries=tuple(entries),
                offense=sum(entry.offense for _, entry in entries),
                defense=sum(entry.defense for _, entry in entries),
                special_teams=sum(entry.special_teams for _, entry in entries),
                offense_total=denominators[OFFENSE],
                defense_total=denominators[DEFENSE],
                special_teams_total=denominators[SPECIAL_TEAMS],
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
        self._players_cache: AsyncTtlCache[str, PlayerCrosswalk] = AsyncTtlCache(
            PLAYERS_TTL_SECONDS, max_entries=1
        )

    async def _csv(self, url: str, allow_missing: bool = False) -> str | None:
        try:
            async with self.session.get(url, timeout=30) as response:
                if response.status == 404 and allow_missing:
                    # A season with no published file yet is an empty season,
                    # not an outage.
                    return None
                if response.status >= 400:
                    raise SnapCountUnavailable(
                        f"nflverse data returned HTTP {response.status}"
                    )
                return await response.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SnapCountUnavailable(f"Could not reach nflverse data: {exc}") from exc

    async def fetch_players(self) -> PlayerCrosswalk:
        async def load() -> PlayerCrosswalk:
            text = await self._csv(PLAYERS_URL)
            return parse_players(text or "")

        return await self._players_cache.get_or_fetch("players", load)

    async def fetch_season(self, season: int) -> dict[str, GameSnaps]:
        async def load() -> dict[str, GameSnaps]:
            text = await self._csv(SNAP_COUNTS_URL.format(season=season), allow_missing=True)
            if text is None:
                return {}
            return parse_snap_counts(text)

        return await self._cache.get_or_fetch(season, load)

    async def fetch_reports(
        self, games: Iterable[Game], roster: dict[str, PlayerRef] | None = None
    ) -> list[SnapCountReport]:
        """Reports for consecutive completed games, oldest first.

        Unpublished games stay in the sequence and never become zero-snap games.
        Callers include one extra predecessor, then slice the requested window.
        """
        reports: list[SnapCountReport] = []
        seasons: dict[int, dict[str, GameSnaps]] = {}
        try:
            crosswalk = await self.fetch_players()
        except SnapCountUnavailable as exc:
            LOGGER.warning("Snap counts posted without nflverse player crosswalk: %s", exc)
            crosswalk = None
        previous: SnapCountReport | None = None
        for game in games:
            if not game.completed:
                raise SnapCountError("Snap reports require completed games")
            if game.season is None:
                raise SnapCountError("Completed game has no season for snap count lookup")
            if game.season not in seasons:
                seasons[game.season] = await self.fetch_season(game.season)
            snaps = match_game(seasons[game.season], game)
            report = build_report(game, snaps, roster, crosswalk) if snaps else SnapCountReport(game=game)
            reports.append(replace(report, previous=previous))
            previous = report
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
