"""Match ordinary roster announcements without conflating separate moves."""

from __future__ import annotations

import re

from .models import Transaction, normalize_name
from .roster_moves import (
    MOVE_VERB_PATTERN, extract_player_moves, split_sentences, transaction_action,
)


_ACTION_BOUNDARY = re.compile(
    r"(?:\s+and\s+|;\s*|,\s*(?:and\s+)?)(?:also\s+)?"
    rf"(?=(?:{MOVE_VERB_PATTERN})\b)",
    re.IGNORECASE,
)
# One action under the spellings the sources trade between.
_SAME_ACTION = {"resigned": "signed", "waiving": "waived"}
# Suffixes the sources write or leave off as they please: ESPN has filed one
# linebacker as both "Carl Jones" and "Carl Jones Jr.".
_GENERATIONAL_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def _player_key(name: str) -> str:
    words = normalize_name(name).split()
    while len(words) > 2 and words[-1] in _GENERATIONAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def move_details(transaction: Transaction) -> frozenset[tuple[str, str, str]]:
    details = set()
    clauses = (
        clause.strip()
        for sentence in split_sentences(transaction.description)
        for clause in _ACTION_BOUNDARY.split(sentence)
    )
    for sentence in clauses:
        sentence = sentence[:1].upper() + sentence[1:]
        verb = normalize_name(transaction_action(sentence) or "")
        text = sentence.lower()
        if "elevat" in text and (verb == "elevated" or "standard" in text):
            verb, location = "elevated", "practice squad"
        else:
            verb = _SAME_ACTION.get(verb, verb)
            if verb in {"signed", "promoted"}:
                destination = re.search(r"\bto\s+(?:the\s+)?", text)
                if destination is not None:
                    text = text[destination.end():]
                # "From the practice squad" is an origin, not a signing destination.
                text = re.split(r"\bfrom\b", text, maxsplit=1)[0]
            location = next((
                label for pattern, label in (
                    (r"injured reserve|reserve.injured|\bir\b", "injured reserve"),
                    (r"non.?football injury|\bnfi\b", "non-football injury"),
                    (r"physically unable|\bpup\b", "physically unable"),
                    (r"practice squad", "practice squad"),
                    (r"active roster|53.man roster", "active roster"),
                )
                if re.search(pattern, text)
            ), "")
        for move in extract_player_moves(sentence):
            details.add((_player_key(move.player.name), verb, location))
    return frozenset(details)


def _covers(
    larger: frozenset[tuple[str, str, str]],
    smaller: frozenset[tuple[str, str, str]],
    *,
    preserve_locations: bool = False,
) -> bool:
    return all(
        any(
            name == other_name and verb == other_verb
            and (
                not location or location == other_location
                or (not other_location and not preserve_locations)
            )
            for other_name, other_verb, other_location in larger
        )
        for name, verb, location in smaller
    )


def same_roster_move(left: Transaction, right: Transaction) -> bool:
    """Whether two reports describe the same moves, whichever source wrote them.

    Reports of one move share its day, with one exception: a standard elevation
    is announced by the club and ESPN the day before the game and filed by the
    official log on game day. So a report a day later matches only when it
    holds nothing but elevations the earlier one already announced; any other
    repeat a day later can be a new move, such as a practice squad signing
    undone and redone, and must not vanish into an edit of yesterday's post.
    """
    gap = abs((left.date - right.date).days)
    if gap > 1:
        return False
    a, b = move_details(left), move_details(right)
    if not (a and b):
        return False
    if gap:
        earlier, later = (a, b) if left.date < right.date else (b, a)
        return later <= earlier and all(verb == "elevated" for _, verb, _ in later)
    return _covers(a, b) or _covers(b, a)


def prefer_roster_move(candidate: Transaction, previous: Transaction) -> bool:
    """Accept added detail, but do not replace a compound move with one fragment."""
    new, old = move_details(candidate), move_details(previous)
    if candidate.transaction_id == previous.transaction_id:
        return len(new) >= len(old) and sum(bool(item[2]) for item in new) >= sum(
            bool(item[2]) for item in old
        )
    if old and (not _covers(new, old, preserve_locations=True) or len(new) < len(old)):
        return False
    if len(new) != len(old):
        return len(new) > len(old)
    if sum(bool(item[2]) for item in new) > sum(bool(item[2]) for item in old):
        return True
    old_words = set(normalize_name(previous.description).split())
    new_words = set(normalize_name(candidate.description).split())
    if old_words < new_words:
        return True
    if new_words < old_words:
        return False
    return sum(bool(p.athlete_id) for p in candidate.players) > sum(
        bool(p.athlete_id) for p in previous.players
    )
