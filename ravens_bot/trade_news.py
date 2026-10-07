"""Matching successive reports of a deal without confusing them with new moves."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, replace
from datetime import date, datetime

from .models import (
    InjuryUpdate, PlayerRef, RosterNews, TeamRef, Transaction, normalize_name, same_player,
)
from .roster_moves import split_sentences
from .trades import Trade, parse_trade


_AGREEMENT = re.compile(
    r"^Agreed(?:\s+in\s+principle)?(?:\s+to\s+terms)?\s+(?:on|to)\s+"
    r"(?P<action>a\s+trade\s+with|trade|acquire)\s+",
    re.IGNORECASE,
)
_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh")


def announcement_trade(transaction: Transaction) -> Trade | None:
    trade = transaction.trade
    if trade is not None:
        return trade
    match = _AGREEMENT.match(transaction.description)
    if match is None:
        return None
    body = transaction.description[match.end():]
    action = match.group("action").lower()
    if action.startswith("a trade"):
        parts = re.split(r"\s+for\s+", body, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            return None
        body = f"Acquired {parts[1]} from {parts[0]}"
    else:
        body = f"{'Traded' if action == 'trade' else 'Acquired'} {body}"
    # Normalize only for matching; the post must still say the deal is pending.
    return parse_trade(body, transaction.players)


def _asset_key(text: str) -> str:
    text = text.lower().replace("-", " ")
    text = re.sub(r"\bpending\b.*", "", text)
    for number, word in enumerate(_ORDINALS, 1):
        text = re.sub(rf"\b{word}\b|\b{number}(?:st|nd|rd|th)\b", str(number), text)
    text = re.sub(r"\b(?:a|an|the|draft)\b", "", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _asset_detail(text: str) -> tuple[int, int]:
    key = _asset_key(text)
    return len(re.findall(r"\b20\d{2}\b", key)), len(re.findall(r"\b[1-7]\b", key))


def same_trade(left: Transaction, right: Transaction) -> bool:
    if abs((left.date - right.date).days) > 1:
        return False
    a, b = announcement_trade(left), announcement_trade(right)
    if a is None or b is None:
        return False
    if a.partner and b.partner and a.partner != b.partner:
        return False
    for near, far in ((a.incoming, b.outgoing), (a.outgoing, b.incoming)):
        if any(same_player(x, y) for x in near.players for y in far.players):
            return False
    if any(
        same_player(x, y)
        for near, far in ((a.incoming, b.incoming), (a.outgoing, b.outgoing))
        for x in near.players for y in far.players
    ):
        return True
    # Picks alone need both a known partner and a matching side of the exchange.
    if not (a.partner and b.partner and a.partner.team_id
            and a.partner.team_id == b.partner.team_id):
        return False
    if a.incoming.players or a.outgoing.players or b.incoming.players or b.outgoing.players:
        return False
    pairs = ((a.incoming.assets, b.incoming.assets), (a.outgoing.assets, b.outgoing.assets))
    known = [(x, y) for x, y in pairs if x and y]
    return bool(known) and all(_asset_key(x) == _asset_key(y) for x, y in known)


def trade_quality(transaction: Transaction) -> tuple[int, ...]:
    trade = announcement_trade(transaction)
    if trade is None:
        return ()
    sides = (trade.incoming, trade.outgoing)
    players = (*trade.incoming.players, *trade.outgoing.players)
    assets = " ".join(side.assets for side in sides)
    return (
        sum(not side.is_empty for side in sides),
        len(players),
        sum(bool(_asset_key(side.assets)) for side in sides),
        len(re.findall(r"\b(?:20\d{2}|[1-7](?:st|nd|rd|th)?|conditional)\b", _asset_key(assets))),
        int(_AGREEMENT.match(transaction.description) is None),
        sum(bool(player.athlete_id) + bool(player.position) for player in players),
        int(not transaction.transaction_id.startswith("bluesky:")),
    )


def prefer_trade(candidate: Transaction, previous: Transaction) -> bool:
    current, saved = announcement_trade(candidate), announcement_trade(previous)
    if current is None or saved is None:
        return False
    for new, old in ((current.incoming, saved.incoming), (current.outgoing, saved.outgoing)):
        if _asset_key(old.assets) and not _asset_key(new.assets):
            return False
        if any(new_count < old_count for new_count, old_count in zip(
            _asset_detail(new.assets), _asset_detail(old.assets)
        )):
            return False
        if any(not any(same_player(x, y) for y in new.players) for x in old.players):
            return False
    newer, older = trade_quality(candidate), trade_quality(previous)
    return newer > older or (
        newer == older and candidate.transaction_id == previous.transaction_id
    )


def retain_trade_context(selected: Transaction, other: Transaction) -> Transaction:
    """Keep accompanying roster moves when replacing the terms of a deal."""
    trade = announcement_trade(other)
    if trade is None or not trade.other_moves:
        return selected
    known = {normalize_name(sentence) for sentence in split_sentences(selected.description)}
    extra = [
        sentence for sentence in split_sentences(trade.other_moves)
        if normalize_name(sentence) not in known
    ]
    if not extra:
        return selected
    players = list(selected.players)
    for player in other.players:
        if any(player.name in sentence for sentence in extra) and not any(
            same_player(player, known) for known in players
        ):
            players.append(player)
    return replace(
        selected,
        description=selected.description.rstrip(".") + ". " + " ".join(extra),
        players=tuple(players),
    )


def encode_news(news: RosterNews) -> str:
    return json.dumps(asdict(news), default=lambda value: value.isoformat(), sort_keys=True)


def decode_news(raw: str) -> RosterNews:
    data = json.loads(raw)
    transaction = data["transaction"]
    transaction["date"] = date.fromisoformat(transaction["date"])
    transaction["players"] = tuple(PlayerRef(**player) for player in transaction["players"])
    if transaction["team"] is not None:
        transaction["team"] = TeamRef(**transaction["team"])
    injuries = []
    for update in data["injuries"]:
        update["player"] = PlayerRef(**update["player"])
        if update["updated"] is not None:
            update["updated"] = datetime.fromisoformat(update["updated"])
        injuries.append(InjuryUpdate(**update))
    return RosterNews(Transaction(**transaction), tuple(injuries))


def trade_version(transaction: Transaction) -> str:
    digest = hashlib.sha256(encode_news(RosterNews(transaction)).encode("utf-8")).hexdigest()
    return f"trade-version:{digest}"
