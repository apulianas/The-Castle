"""Reading a roster move's prose as the moves it actually describes.

ESPN's NFL transaction feed carries a sentence and nothing else, so who was
moved, what happened to them, and which unit they played for all have to be read
back out of the wording. Keeping that here rather than in the API client means
the parsing is exercised without a client, and lets the wording layer group a cut
list by unit without importing the network layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import PlayerRef, normalize_name


# Position codes ESPN uses inside transaction prose, e.g. "Waived TE Jordan Murray."
# Descriptions also pluralize them for a group, as in "Waived CBs A and B".
POSITION_CODES = (
    "QB", "RB", "FB", "HB", "WR", "TE", "OL", "OT", "OG", "OC", "C", "G", "T",
    "DL", "DE", "DT", "NT", "EDGE", "LB", "ILB", "OLB", "MLB", "DB", "CB", "S",
    "FS", "SS", "K", "PK", "P", "LS", "KR", "PR", "ATH", "SAF",
)
_POSITION_ALT = "|".join(sorted(POSITION_CODES, key=len, reverse=True))
# A name part is either an initial group like "C.J." or a plain word. Trailing
# sentence periods are deliberately excluded so a name cannot run past the end
# of its sentence into the next one, as in "... Kaimon Rucker. Placed WR ...".
_NAME_PART = r"[A-Z](?:\.[A-Z])*\.|[A-Z][A-Za-z'\u2019\-]+"
# A code only introduces players when a capitalized word follows it, which keeps
# "C.J. Okoye" from reading as the center position.
_POSITION_RE = re.compile(rf"\b(?P<position>{_POSITION_ALT})s?(?=\s+[A-Z])")
_NAME_RE = re.compile(rf"(?:{_NAME_PART})(?:\s+(?:{_NAME_PART}))+")
_SEPARATOR_RE = re.compile(r"\s*(?:,\s*and\s+|,\s*|\s+and\s+)")
# A move opens with its verb, which ESPN hyphenates in "Re-signed" and nowhere
# else; reading only the first half would leave the headline saying "Re".
_ACTION_RE = re.compile(r"^\s*([A-Z][a-z]+(?:-[a-z]+)?)")

# The units a roster is built from, in depth chart order: offense, then defense,
# then the kicking game. A cut list is read by unit, so this order is what gives
# two dozen names a shape.
POSITION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Quarterbacks", ("QB",)),
    ("Running backs", ("RB", "HB", "FB")),
    ("Wide receivers", ("WR",)),
    ("Tight ends", ("TE",)),
    ("Offensive line", ("OL", "OT", "OG", "OC", "C", "G", "T")),
    ("Defensive line", ("DL", "DE", "DT", "NT", "EDGE")),
    ("Linebackers", ("LB", "ILB", "OLB", "MLB")),
    ("Defensive backs", ("DB", "CB", "S", "SAF", "FS", "SS")),
    ("Specialists", ("K", "PK", "P", "LS", "KR", "PR")),
)
# ESPN files the odd move under a code that names no unit, such as "ATH", and a
# player is not worth dropping from a cut list over it.
OTHER_POSITION_GROUP = "Other"
GROUP_ORDER: tuple[str, ...] = tuple(name for name, _ in POSITION_GROUPS) + (
    OTHER_POSITION_GROUP,
)
_GROUP_BY_CODE = {
    code: name for name, codes in POSITION_GROUPS for code in codes
}

# Words that end a sentence in a transaction description without ending a name.
_NAME_SUFFIXES = frozenset({"jr", "sr", "st", "no"})
# Suffixes whose full stop belongs to the name rather than to the sentence, so
# a list of cuts reads "Emery Jones Jr." the way the player's own jersey does.
_GENERATIONAL_SUFFIXES = frozenset({"jr", "sr"})
_SENTENCE_END_RE = re.compile(r"\.(?=\s+[A-Z])")
_INITIALS_RE = re.compile(r"(?:[A-Z]\.)*[A-Z]$")
_LAST_TOKEN_RE = re.compile(r"([A-Za-z.]+)$")
# A name is read without the full stop that closes it, so the stop is still
# sitting in front of whatever the sentence says next: "Placed T Emery Jones Jr.
# on reserve" leaves ". on reserve" once the name is taken off.
_LEADING_PUNCTUATION_RE = re.compile(r"^[\s.,;:—–-]+")


def _ends_in_generational_suffix(name: str) -> bool:
    """Whether a name's own full stop was left behind when it was read.

    "Jr" and "Sr" are written with a stop, and the name regex stops short of it
    so a name cannot run past the end of its sentence. Dropping that stop
    renames the player, which a list of cuts shows one line at a time.
    """
    return name.rsplit(" ", 1)[-1].lower() in _GENERATIONAL_SUFFIXES


def position_group(position: str | None) -> str:
    """The unit a position code belongs to."""
    return _GROUP_BY_CODE.get((position or "").strip().upper(), OTHER_POSITION_GROUP)


def split_sentences(text: str) -> list[str]:
    """Sentences of a transaction description, keeping names intact.

    A description runs several moves together — "Traded LB A ... . Reinstated
    OLBs B and C ..." — and only the first is the trade, so the rest has to be
    separated off before the trade's own wording can be read. Splitting on every
    full stop would cut "A.J. Klein" and "Irv Smith Jr." in half, so a stop that
    closes an initial or a name suffix is not a sentence end.
    """
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text or ""):
        head = (text or "")[start : match.start()]
        token_match = _LAST_TOKEN_RE.search(head)
        token = token_match.group(1) if token_match else ""
        if _INITIALS_RE.fullmatch(token):
            continue
        if token.replace(".", "").lower() in _NAME_SUFFIXES:
            continue
        sentences.append((text or "")[start : match.end()].strip())
        start = match.end()
    tail = (text or "")[start:].strip()
    if tail:
        sentences.append(tail)
    return [sentence for sentence in sentences if sentence]


def extract_players(description: str) -> tuple[PlayerRef, ...]:
    """Players named in a transaction description.

    ESPN's NFL transaction feed carries no athlete records, only prose such as
    "Signed DT Phidarian Mathis to the active roster." Pulling positions and
    names out of the text is what makes player links and photos possible at all.
    One position code can introduce a whole list, as in "Waived CBs A and B",
    so each code is followed as far as its comma-separated run of names goes.
    """
    text = description or ""
    players: list[PlayerRef] = []
    seen: set[str] = set()

    for match in _POSITION_RE.finditer(text):
        position = match.group("position")
        cursor = match.end()
        while True:
            whitespace = re.match(r"\s+", text[cursor:])
            if whitespace:
                cursor += whitespace.end()
            # A new position code ends the current list rather than reading as a name.
            if _POSITION_RE.match(text, cursor):
                break
            found = _NAME_RE.match(text, cursor)
            if found is None:
                break
            name = found.group(0).strip()
            cursor = found.end()
            if _ends_in_generational_suffix(name) and text[cursor : cursor + 1] == ".":
                name += "."
                cursor += 1
            key = normalize_name(name)
            if key and key not in seen and len(name.split()) >= 2:
                seen.add(key)
                players.append(PlayerRef(name=name, position=position))
            separator = _SEPARATOR_RE.match(text, cursor)
            if separator is None:
                break
            cursor = separator.end()

    return tuple(players)


def transaction_action(description: str) -> str | None:
    match = _ACTION_RE.match(description or "")
    return match.group(1).strip() if match else None


@dataclass(frozen=True)
class PlayerMove:
    """One player, and what the description says happened to them."""

    player: PlayerRef
    action: str


def _sentence_action(sentence: str, players: tuple[PlayerRef, ...]) -> str:
    """What a sentence did, as a phrase that reads under a player's name.

    The verb alone loses the part that matters most on cut down day: "Placed"
    covers both injured reserve and the non-football injury list, and neither is
    a cut. Whatever the sentence says after its last name is the qualifier, so
    the verb and that remainder together state the move in full.
    """
    verb = (transaction_action(sentence) or "").lower()
    last = players[-1].name
    index = sentence.rfind(last)
    tail = sentence[index + len(last) :] if index >= 0 else ""
    tail = _LEADING_PUNCTUATION_RE.sub("", tail.strip()).strip().rstrip(".").strip()
    return " ".join(part for part in (verb, tail) if part)


def extract_player_moves(description: str) -> tuple[PlayerMove, ...]:
    """Every player a description names, each with the move that covered them.

    A cut down day description is several moves in one string — a waiver list, a
    release list, an injury settlement and a move to injured reserve — so
    reading it as one flat roll of names would report players who were not cut
    as though they had been.
    """
    moves: list[PlayerMove] = []
    seen: set[str] = set()
    for sentence in split_sentences(description or ""):
        players = extract_players(sentence)
        if not players:
            continue
        action = _sentence_action(sentence, players)
        for player in players:
            key = normalize_name(player.name)
            if not key or key in seen:
                continue
            seen.add(key)
            moves.append(PlayerMove(player=player, action=action))
    return tuple(moves)


def group_player_moves(
    moves: tuple[PlayerMove, ...],
) -> list[tuple[str, list[PlayerMove]]]:
    """A move list bucketed by unit, offense first, in depth chart order."""
    grouped: dict[str, list[PlayerMove]] = {}
    for move in moves:
        grouped.setdefault(position_group(move.player.position), []).append(move)
    return [(name, grouped[name]) for name in GROUP_ORDER if name in grouped]
