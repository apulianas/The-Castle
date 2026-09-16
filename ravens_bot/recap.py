"""Batch NFLverse postgame analytics; never a source for live commands."""

from __future__ import annotations

import asyncio
import csv
import gzip
import io
import math
import tempfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import BinaryIO, TypeVar
from zoneinfo import ZoneInfo

import aiohttp

from .cache import AsyncTtlCache
from .espn import EspnClient
from .models import Game


PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.csv.gz"
)
RECAP_TTL_SECONDS = 3600.0
MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
TEAM_ALIASES = {
    "LAR": "LA", "STL": "LA", "WSH": "WAS", "LVR": "LV", "OAK": "LV",
    "JAC": "JAX", "SD": "LAC",
}
REQUIRED_COLUMNS = frozenset(
    """game_id season season_type week home_team away_team play_id posteam
    play_type play_type_nfl desc qtr time qb_dropback rush_attempt qb_kneel
    qb_spike two_point_attempt epa wpa pass_attempt sack complete_pass
    passing_yards rushing_yards yards_gained pass_touchdown rush_touchdown
    interception passer_player_id passer_player_name rusher_player_id
    rusher_player_name lateral_rush lateral_rushing_yards lateral_rusher_player_id
    lateral_rusher_player_name td_player_id total_home_score total_away_score""".split()
)


class RecapError(RuntimeError):
    """An HTTP, schema, metadata, or corrupt-file error, not publication lag."""


def _team(value: str | None) -> str:
    code = (value or "").strip().upper()
    return TEAM_ALIASES.get(code, code)


def number(value: str | None) -> float | None:
    try:
        result = float(value or "")
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


@dataclass
class Efficiency:
    plays: int = 0
    measured: int = 0
    epa: float = 0.0
    successes: int = 0

    def add(self, value: float | None) -> None:
        self.plays += 1
        if value is not None:
            self.measured += 1
            self.epa += value
            self.successes += value > 0


@dataclass
class Production:
    attempts: int = 0
    completions: int = 0
    yards: float = 0
    touchdowns: int = 0
    interceptions: int = 0
    complete: bool = True

    def add(self, row: dict[str, str], passing: bool) -> None:
        self.attempts += 1
        keys = ["complete_pass", "pass_touchdown", "interception"] if passing else ["rush_touchdown"]
        values = [number(row.get(key)) for key in keys]
        if any(value not in (0, 1) for value in values):
            self.complete = False
        else:
            if passing:
                self.completions += int(values[0])
                self.touchdowns += int(values[1])
                self.interceptions += int(values[2])
            else:
                self.touchdowns += int(values[0])
        yards = number(row.get("passing_yards" if passing else "rushing_yards"))
        # nflfastR leaves passing_yards null on incomplete passes and picks.
        if passing and number(row.get("complete_pass")) == 0:
            yards = 0
        if yards is None:
            self.complete = False
        else:
            self.yards += yards


@dataclass(frozen=True)
class Swing:
    wpa: float
    description: str
    clock: str


@dataclass
class GameRecap:
    game_id: str
    season: int
    season_type: str
    week: int
    home: str
    away: str
    offense: Efficiency = field(default_factory=Efficiency)
    dropbacks: Efficiency = field(default_factory=Efficiency)
    designed_rushes: Efficiency = field(default_factory=Efficiency)
    passing: Production = field(default_factory=Production)
    rushing: Production = field(default_factory=Production)
    passers: dict[str, tuple[str, Production]] = field(default_factory=dict)
    rushers: dict[str, tuple[str, Production]] = field(default_factory=dict)
    sacks: int = 0
    sack_yards: float = 0
    sacks_complete: bool = True
    leaders_complete: bool = True
    swings: list[Swing] = field(default_factory=list)
    wpa_missing: int = 0
    flags_missing: bool = False
    ended: bool = False
    home_score: float | None = None
    away_score: float | None = None

    def add(self, row: dict[str, str]) -> None:
        if row["play_type_nfl"] == "END_GAME" or row["desc"].strip() == "END GAME":
            self.ended = True
            self.home_score = number(row["total_home_score"])
            self.away_score = number(row["total_away_score"])
        if number(row.get("play_deleted")) == 1:
            return
        possession = _team(row["posteam"])
        if possession not in (self.home, self.away):
            if row["play_type"] in ("pass", "run"):
                self.flags_missing = True
                self.passing.complete = self.rushing.complete = self.sacks_complete = False
            return
        play_type = row["play_type"]
        # WPA is defined for the pre-play offense, even on turnovers. Using the
        # next play's possession would flip interceptions/fumble returns twice.
        if play_type not in ("", "quarter_end", "qb_kneel", "qb_spike"):
            wpa = number(row["wpa"])
            if wpa is None or abs(wpa) > 1:
                self.wpa_missing += 1
            else:
                period = number(row["qtr"])
                clock = f"Q{int(period)} {row['time']}" if period is not None else row["time"]
                self.swings.append(Swing(
                    wpa if possession == "BAL" else -wpa,
                    row["desc"][:700], clock[:40],
                ))
                self.swings.sort(key=lambda item: abs(item.wpa), reverse=True)
                del self.swings[3:]
        if possession != "BAL" or play_type in ("", "no_play", "quarter_end"):
            return
        if play_type in ("pass", "run", "qb_kneel", "qb_spike") and any(
            number(row[key]) not in (0, 1)
            for key in ("two_point_attempt", "qb_dropback", "rush_attempt", "qb_kneel", "qb_spike", "sack", "pass_attempt")
        ):
            self.flags_missing = True
            self.passing.complete = self.rushing.complete = self.sacks_complete = False
            return
        if number(row["two_point_attempt"]) == 1:
            return
        dropback = number(row["qb_dropback"]) == 1
        rush = number(row["rush_attempt"]) == 1
        if (
            play_type in ("pass", "run")
            and number(row["qb_kneel"]) == 0
            and number(row["qb_spike"]) == 0
            and (dropback or rush)
        ):
            epa = number(row["epa"])
            self.offense.add(epa)
            (self.dropbacks if dropback else self.designed_rushes).add(epa)
        if number(row["sack"]) == 1:
            self.sacks += 1
            yards = number(row["yards_gained"])
            if yards is None:
                self.sacks_complete = False
            else:
                self.sack_yards += yards
        elif number(row["pass_attempt"]) == 1:
            self._production(row, True)
        if rush:
            self._production(row, False)

    def _production(self, row: dict[str, str], passing: bool) -> None:
        total = self.passing if passing else self.rushing
        players = self.passers if passing else self.rushers
        prefix = "passer" if passing else "rusher"
        total.add(row, passing)
        player_row = row
        if not passing and number(row["lateral_rush"]) == 1:
            yards = number(row["lateral_rushing_yards"])
            lateral_name = row["lateral_rusher_player_name"].strip()
            lateral_id = row["lateral_rusher_player_id"].strip()
            if yards is None:
                total.complete = False
            else:
                total.yards += yards
            if not lateral_name or not lateral_id:
                self.leaders_complete = False
            else:
                if lateral_id not in players:
                    if len(players) >= 128:
                        raise RecapError("NFLverse PBP contains too many player identities in one game.")
                    players[lateral_id] = (lateral_name[:100], Production())
                credit = players[lateral_id][1]
                if yards is None:
                    credit.complete = False
                else:
                    credit.yards += yards
                if number(row["rush_touchdown"]) == 1:
                    if row["td_player_id"] == lateral_id:
                        credit.touchdowns += 1
                        player_row = dict(row, rush_touchdown="0")
                    elif row["td_player_id"] != row["rusher_player_id"]:
                        self.leaders_complete = False
        name = row[f"{prefix}_player_name"].strip()
        key = row[f"{prefix}_player_id"].strip() or name
        if not key or not name:
            self.leaders_complete = False
            return
        if key not in players:
            if len(players) >= 128:
                raise RecapError("NFLverse PBP contains too many player identities in one game.")
            players[key] = (name[:100], Production())
        players[key][1].add(player_row, passing)

    def partial_reasons(self, game: Game) -> list[str]:
        reasons = []
        if not self.ended:
            reasons.append("NFLverse has not published an end-of-game record")
        elif (
            game.home is None or game.away is None
            or self.home_score is None or self.away_score is None
            or self.home_score != game.home.score or self.away_score != game.away.score
        ):
            reasons.append("NFLverse's final score is missing or differs from ESPN")
        if not self.offense.measured or self.offense.measured != self.offense.plays:
            reasons.append("some offensive EPA values are missing")
        if not self.passing.complete or not self.rushing.complete or not self.sacks_complete:
            reasons.append("some production values are missing")
        if not self.leaders_complete:
            reasons.append("some player identities are missing")
        if self.flags_missing:
            reasons.append("some play classification flags are missing")
        if self.wpa_missing or not self.swings:
            reasons.append("some win-probability values are missing")
        return reasons


def parse_pbp(lines: Iterable[str], season: int) -> dict[str, GameRecap]:
    reader = csv.DictReader(lines, strict=True)
    if reader.fieldnames and len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise RecapError("NFLverse PBP schema contains duplicate columns.")
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
    if missing:
        raise RecapError("NFLverse PBP schema is missing columns: " + ", ".join(sorted(missing)))
    games: dict[str, GameRecap] = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise RecapError("NFLverse PBP contains a malformed CSV row.")
        home, away = _team(row["home_team"]), _team(row["away_team"])
        if "BAL" not in (home, away) or row["season_type"] not in ("REG", "POST"):
            continue
        row_season, week = number(row["season"]), number(row["week"])
        if row_season != season or week is None or not week.is_integer() or week < 1:
            raise RecapError("NFLverse PBP contains invalid season/week metadata.")
        game_id = row["game_id"]
        parts = game_id.split("_")
        key = f"{season}_{int(week):02d}_{away}_{home}"
        # Historical IDs retain OAK/SD/STL even when the team columns have
        # been normalized by nflfastR to their current franchise codes.
        if len(parts) != 4 or f"{parts[0]}_{parts[1]}_{_team(parts[2])}_{_team(parts[3])}" != key:
            raise RecapError("NFLverse PBP game id disagrees with game metadata.")
        if key not in games:
            if len(games) >= 32:
                raise RecapError("NFLverse PBP contains too many Ravens games in one season.")
            games[key] = GameRecap(game_id, season, row["season_type"], int(week), home, away)
        entry = games[key]
        if entry.season_type != row["season_type"]:
            raise RecapError("NFLverse PBP contains conflicting game metadata.")
        entry.add(row)
    return games


def match_game(games: dict[str, GameRecap], game: Game) -> GameRecap | None:
    if (
        game.season is None or game.week_number is None
        or game.season_type not in (2, 3) or game.home is None or game.away is None
        or game.ravens is None
        or not game.home.team.abbreviation or not game.away.team.abbreviation
    ):
        raise RecapError("ESPN game metadata is incomplete; cannot safely match a NFLverse recap.")
    # ESPN playoff weeks start at 1; nflfastR continues after regular-season weeks.
    week = game.week_number
    if game.season_type == 3:
        # ESPN reserves week 4 for the Pro Bowl; nflfastR has no Pro Bowl gap.
        if week == 5:
            week = 4
        week += 18 if game.season >= 2021 else 17
    key = f"{game.season}_{week:02d}_{_team(game.away.team.abbreviation)}_{_team(game.home.team.abbreviation)}"
    entry = games.get(key)
    if entry and entry.season_type != ("POST" if game.season_type == 3 else "REG"):
        return None
    return entry


def select_completed_game(
    games: Iterable[Game], time_zone: ZoneInfo, target_date: date | None = None,
) -> Game | None:
    eligible = [
        game for game in games
        if game.completed and game.ravens is not None and game.season_type in (2, 3)
        and game.start_time is not None
        and (target_date is None or game.start_time.astimezone(time_zone).date() == target_date)
    ]
    return max(eligible, key=lambda game: game.start_time) if eligible else None


async def find_recap_game(
    espn: EspnClient, today: date, time_zone: ZoneInfo, target_date: date | None = None,
) -> Game | None:
    async def season_games(season: int) -> list[Game]:
        regular, postseason = await asyncio.gather(
            espn.fetch_season_schedule(season, season_type=2),
            espn.fetch_season_schedule(season, season_type=3),
        )
        return list({game.event_id: game for game in (*regular, *postseason)}.values())

    if target_date is not None:
        season = target_date.year - (target_date.month < 3)
        return select_completed_game(
            await season_games(season), time_zone, target_date,
        )
    season = today.year - (today.month < 3)
    schedule = await season_games(season)
    selected = select_completed_game(schedule, time_zone)
    if selected is not None:
        return selected
    return select_completed_game(await season_games(season - 1), time_zone)


class _LimitedReader(io.RawIOBase):
    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.remaining = MAX_DECOMPRESSED_BYTES

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        chunk = self.source.read(min(len(buffer), self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise RecapError("NFLverse PBP exceeds the decompressed size limit.")
        buffer[:len(chunk)] = chunk
        return len(chunk)


def _parse_compressed(source: BinaryIO, season: int) -> dict[str, GameRecap]:
    source.seek(0)
    try:
        with gzip.GzipFile(fileobj=source) as compressed:
            with io.TextIOWrapper(io.BufferedReader(_LimitedReader(compressed)), encoding="utf-8-sig", newline="") as text:
                return parse_pbp(text, season)
    except (OSError, EOFError, UnicodeError, csv.Error, zlib.error) as exc:
        raise RecapError("NFLverse PBP is corrupt or unreadable.") from exc


T = TypeVar("T")


async def _off_loop(function: Callable[..., T], *args: object) -> T:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A worker cannot be cancelled mid-read/write; finish before closing its
        # temporary file. Cancellation still propagates to the Discord request.
        await task
        raise


@dataclass(frozen=True)
class RecapSeason:
    games: dict[str, GameRecap]
    fetched_at: datetime
    last_modified: str | None = None
    published: bool = True


@dataclass(frozen=True)
class RecapReport:
    game: Game
    data: GameRecap | None
    source: RecapSeason


class RecapClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.cache: AsyncTtlCache[int, RecapSeason] = AsyncTtlCache(RECAP_TTL_SECONDS, max_entries=3)

    async def fetch(self, game: Game) -> RecapReport:
        # Validate ESPN metadata even when the release is not published.
        match_game({}, game)
        season = game.season
        assert season is not None
        source = await self.cache.get_or_fetch(season, lambda: self._load(season))
        return RecapReport(game, match_game(source.games, game), source)

    async def _load(self, season: int) -> RecapSeason:
        try:
            async with self.session.get(
                PBP_URL.format(season=season),
                timeout=aiohttp.ClientTimeout(total=180, sock_read=45),
                auto_decompress=False,
            ) as response:
                if response.status == 404:
                    return RecapSeason({}, datetime.now(timezone.utc), published=False)
                if response.status != 200:
                    raise RecapError(f"NFLverse PBP request failed (HTTP {response.status}).")
                if response.content_length and response.content_length > MAX_COMPRESSED_BYTES:
                    raise RecapError("NFLverse PBP exceeds the download size limit.")
                with tempfile.TemporaryFile(mode="w+b") as source:
                    size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > MAX_COMPRESSED_BYTES:
                            raise RecapError("NFLverse PBP exceeds the download size limit.")
                        await _off_loop(source.write, chunk)
                    games = await _off_loop(_parse_compressed, source, season)
                return RecapSeason(games, datetime.now(timezone.utc), response.headers.get("Last-Modified"))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise RecapError(f"Could not read NFLverse postgame PBP: {exc}") from exc
