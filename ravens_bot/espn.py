from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from .dates import DateWindow, espn_dates
from .models import (
    RAVENS_NAME,
    RAVENS_SLUG,
    RAVENS_TEAM_ID,
    Game,
    InactivePlayer,
    InactiveReport,
    Standing,
    Transaction,
)


SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"


class EspnApiError(RuntimeError):
    pass


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _display_name(value: Any) -> str | None:
    data = _as_dict(value)
    for key in ("displayName", "shortDisplayName", "name", "fullName"):
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _status_text(value: Any) -> str:
    data = _as_dict(value)
    status_type = _as_dict(data.get("type"))
    for item in (status_type, data):
        text = _display_name(item)
        if text:
            return text
    return "Scheduled"


def _team_id(team: Any) -> str | None:
    data = _as_dict(team)
    raw = data.get("id")
    if raw is not None:
        return str(raw)
    ref = data.get("$ref")
    if isinstance(ref, str):
        parts = ref.rstrip("/").split("/")
        if parts:
            return parts[-1].split("?")[0]
    return None


def _team_name(team: Any) -> str | None:
    data = _as_dict(team)
    return _display_name(data) or _display_name(data.get("team"))


def _game_from_event(event: dict[str, Any]) -> Game:
    competition = _as_dict(_as_list(event.get("competitions"))[0] if event.get("competitions") else {})
    venue = _display_name(competition.get("venue"))
    return Game(
        event_id=str(event.get("id", "")),
        name=str(event.get("name") or event.get("shortName") or "Ravens game"),
        short_name=str(event.get("shortName") or event.get("name") or "BAL"),
        start_time=parse_datetime(event.get("date")),
        status=_status_text(event.get("status")),
        venue=venue,
    )


def event_has_ravens(event: dict[str, Any]) -> bool:
    for competition in _as_list(event.get("competitions")):
        for competitor in _as_list(_as_dict(competition).get("competitors")):
            team = _as_dict(competitor).get("team")
            if _team_id(team) == RAVENS_TEAM_ID:
                return True
    return False


def parse_schedule(payload: dict[str, Any]) -> list[Game]:
    games = []
    for event in _as_list(payload.get("events")):
        event_data = _as_dict(event)
        if event_has_ravens(event_data):
            games.append(_game_from_event(event_data))
    return games


def _transaction_date(payload: dict[str, Any]) -> date | None:
    for key in ("date", "lastModified", "createDate"):
        raw = payload.get(key)
        if isinstance(raw, str):
            parsed = parse_datetime(raw)
            if parsed:
                return parsed.date()
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                pass
    return None


def _transaction_team_ids(payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("team", "teams"):
        value = payload.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            team_id = _team_id(item)
            if team_id:
                ids.add(team_id)
    return ids


def parse_transactions(payload: dict[str, Any], target_date: date) -> list[Transaction]:
    raw_items = _as_list(payload.get("items")) or _as_list(payload.get("transactions"))
    transactions: list[Transaction] = []
    for raw in raw_items:
        item = _as_dict(raw)
        item_date = _transaction_date(item) or target_date
        if item_date != target_date:
            continue
        team_ids = _transaction_team_ids(item)
        if team_ids and RAVENS_TEAM_ID not in team_ids:
            continue
        athlete = _display_name(item.get("athlete")) or _display_name(item.get("player"))
        type_text = _display_name(item.get("type"))
        description = str(
            item.get("description")
            or item.get("text")
            or " ".join(part for part in (type_text, athlete) if part)
            or "Ravens roster transaction"
        )
        transactions.append(
            Transaction(
                transaction_id=str(item.get("id") or f"{item_date}:{description}"),
                date=item_date,
                description=description,
                type_text=type_text,
                athlete=athlete,
            )
        )
    return transactions


def _player_from_item(item: dict[str, Any], fallback_team: str | None = None) -> InactivePlayer | None:
    name = (
        _display_name(item.get("athlete"))
        or _display_name(item.get("player"))
        or _display_name(item)
    )
    if not name:
        return None
    team = _team_name(item.get("team")) or fallback_team
    reason = _display_name(item.get("reason")) or _display_name(item.get("status"))
    return InactivePlayer(name=name, team=team, reason=reason)


def _collect_inactives(value: Any, players: list[InactivePlayer], team: str | None = None) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_inactives(item, players, team)
        return
    if not isinstance(value, dict):
        return

    current_team = _team_name(value.get("team")) or team
    for key, nested in value.items():
        lowered = key.lower()
        if lowered in {"inactives", "inactiveplayers", "inactive_players"}:
            for item in _as_list(nested):
                player = _player_from_item(_as_dict(item), current_team)
                if player:
                    players.append(player)
            continue
        if lowered == "status":
            status = (_display_name(nested) or str(nested)).lower()
            if "inactive" in status:
                player = _player_from_item(value, current_team)
                if player:
                    players.append(player)
    for nested in value.values():
        _collect_inactives(nested, players, current_team)


def parse_inactive_report(summary: dict[str, Any], game: Game) -> InactiveReport:
    players: list[InactivePlayer] = []
    _collect_inactives(summary, players)
    seen: set[tuple[str, str | None]] = set()
    unique: list[InactivePlayer] = []
    for player in players:
        key = (player.name, player.team)
        if key not in seen:
            seen.add(key)
            unique.append(player)
    return InactiveReport(game=game, players=tuple(unique))


def parse_standings(payload: dict[str, Any]) -> list[Standing]:
    groups = _as_list(payload.get("standings")) or _as_list(payload.get("children"))
    entries: list[Any] = []
    for group in groups:
        entries.extend(_as_list(_as_dict(group).get("entries")))
    entries.extend(_as_list(payload.get("entries")))

    standings: list[Standing] = []
    for index, entry in enumerate(entries, start=1):
        data = _as_dict(entry)
        team = _team_name(data.get("team")) or _display_name(data) or "NFL team"
        stats = _as_list(data.get("stats"))
        wins = losses = ties = pct = gb = streak = None
        for stat in stats:
            stat_data = _as_dict(stat)
            name = str(stat_data.get("name") or "").lower()
            value = stat_data.get("displayValue") or stat_data.get("value")
            if name == "wins":
                wins = value
            elif name == "losses":
                losses = value
            elif name == "ties":
                ties = value
            elif name in {"winpercent", "winpercentage"}:
                pct = value
            elif name in {"gamesbehind", "gamesback"}:
                gb = value
            elif name == "streak":
                streak = value
        record = "-".join(str(part) for part in (wins, losses, ties) if part is not None)
        if not record:
            record = str(data.get("displayName") or data.get("summary") or "—")
        details = []
        if pct is not None:
            details.append(f"Pct {pct}")
        if gb is not None:
            details.append(f"GB {gb}")
        if streak is not None:
            details.append(f"Streak {streak}")
        standings.append(
            Standing(
                team=team,
                record=record,
                summary=" • ".join(details),
                rank=int(data.get("rank") or index) if str(data.get("rank") or index).isdigit() else None,
            )
        )
    return standings


class EspnClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def _json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with self.session.get(url, params=params, timeout=20) as response:
                if response.status >= 400:
                    raise EspnApiError(f"ESPN API returned HTTP {response.status}")
                data = await response.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise EspnApiError(f"Could not reach ESPN API: {exc}") from exc
        if not isinstance(data, dict):
            raise EspnApiError("ESPN API returned an unexpected response")
        return data

    async def _resolve_refs(self, payload: dict[str, Any]) -> dict[str, Any]:
        resolved: list[dict[str, Any]] = []
        for item in _as_list(payload.get("items")):
            item_data = _as_dict(item)
            ref = item_data.get("$ref")
            if isinstance(ref, str):
                try:
                    item_data = await self._json(ref)
                except EspnApiError:
                    continue
            resolved.append(item_data)
        return {**payload, "items": resolved}

    async def fetch_transactions(self, target_date: date) -> list[Transaction]:
        payload = await self._json(
            f"{CORE_BASE}/transactions",
            {
                "limit": "100",
                "teams": RAVENS_TEAM_ID,
                "dates": f"{target_date:%Y%m%d}",
            },
        )
        if any("$ref" in _as_dict(item) for item in _as_list(payload.get("items"))):
            payload = await self._resolve_refs(payload)
        return parse_transactions(payload, target_date)

    async def fetch_schedule(self, window: DateWindow) -> list[Game]:
        payload = await self._json(
            f"{SITE_BASE}/scoreboard",
            {"dates": espn_dates(window), "limit": "100"},
        )
        return parse_schedule(payload)

    async def fetch_inactives(self, target_date: date) -> list[InactiveReport]:
        games = await self.fetch_schedule(DateWindow(target_date, target_date))
        reports: list[InactiveReport] = []
        for game in games:
            summary = await self._json(f"{SITE_BASE}/summary", {"event": game.event_id})
            reports.append(parse_inactive_report(summary, game))
        return reports

    async def fetch_standings(self) -> list[Standing]:
        payload = await self._json(f"{SITE_BASE}/standings", {"groups": "12"})
        return parse_standings(payload)

    async def fetch_next_game(self, today: date) -> Game | None:
        games = await self.fetch_schedule(DateWindow(today, today + timedelta(days=370)))
        future_games = [game for game in games if game.start_time is None or game.start_time.date() >= today]
        return future_games[0] if future_games else None
