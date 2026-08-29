"""Reading a trade out of ESPN's transaction prose.

A trade is the only roster move where the verb does not say which way a player
travelled. ESPN writes the same deal several ways — "Traded <player> to <team>
for <pick>", "Traded <pick> to <team> for <player>", "Acquired <player> from
<team> in exchange for <pick>", "Acquired <pick> from <team> for <player>",
"Received <player> from <team> in exchange for <pick>", "Received <pick> in a
trade with <team>" — so a parser that trusted the verb would call half of them
arrivals and the other half departures. What settles the direction is which side
of the sentence an asset sits on, which is what this module reads.

The transaction feed carries a team reference for the Ravens only, never for the
other club, so the partner is recovered from the words as well.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .models import PlayerRef, TeamRef, normalize_name


# ESPN names a partner by city ("to Chicago"), in full ("to Kansas City
# Chiefs"), or by nickname, so the directory is keyed every way it writes them.
# The abbreviation doubles as the slug ESPN's logo and clubhouse URLs expect.
_TEAM_TABLE: tuple[tuple[str, str, str, str], ...] = (
    ("1", "Atlanta", "Falcons", "ATL"),
    ("2", "Buffalo", "Bills", "BUF"),
    ("3", "Chicago", "Bears", "CHI"),
    ("4", "Cincinnati", "Bengals", "CIN"),
    ("5", "Cleveland", "Browns", "CLE"),
    ("6", "Dallas", "Cowboys", "DAL"),
    ("7", "Denver", "Broncos", "DEN"),
    ("8", "Detroit", "Lions", "DET"),
    ("9", "Green Bay", "Packers", "GB"),
    ("10", "Tennessee", "Titans", "TEN"),
    ("11", "Indianapolis", "Colts", "IND"),
    ("12", "Kansas City", "Chiefs", "KC"),
    ("13", "Las Vegas", "Raiders", "LV"),
    ("14", "Los Angeles", "Rams", "LAR"),
    ("15", "Miami", "Dolphins", "MIA"),
    ("16", "Minnesota", "Vikings", "MIN"),
    ("17", "New England", "Patriots", "NE"),
    ("18", "New Orleans", "Saints", "NO"),
    ("19", "New York", "Giants", "NYG"),
    ("20", "New York", "Jets", "NYJ"),
    ("21", "Philadelphia", "Eagles", "PHI"),
    ("22", "Arizona", "Cardinals", "ARI"),
    ("23", "Pittsburgh", "Steelers", "PIT"),
    ("24", "Los Angeles", "Chargers", "LAC"),
    ("25", "San Francisco", "49ers", "SF"),
    ("26", "Seattle", "Seahawks", "SEA"),
    ("27", "Tampa Bay", "Buccaneers", "TB"),
    ("28", "Washington", "Commanders", "WSH"),
    ("29", "Carolina", "Panthers", "CAR"),
    ("30", "Jacksonville", "Jaguars", "JAX"),
    ("33", "Baltimore", "Ravens", "BAL"),
    ("34", "Houston", "Texans", "HOU"),
)


NFL_TEAMS: tuple[TeamRef, ...] = tuple(
    TeamRef(
        name=f"{location} {nickname}",
        team_id=team_id,
        abbreviation=abbreviation,
        slug=abbreviation.lower(),
    )
    for team_id, location, nickname, abbreviation in _TEAM_TABLE
)


# Names a club has traded under before. An old transaction is still readable on
# a date query, and ESPN keeps the wording it published at the time.
_TEAM_ALIASES: dict[str, str] = {
    "Oakland": "LV",
    "Oakland Raiders": "LV",
    "San Diego": "LAC",
    "San Diego Chargers": "LAC",
    "St. Louis": "LAR",
    "St. Louis Rams": "LAR",
    "Washington Football Team": "WSH",
    "Washington Redskins": "WSH",
}


@lru_cache(maxsize=1)
def _team_index() -> dict[str, TeamRef]:
    """Every unambiguous way ESPN writes a team, pointing at that team.

    "New York" and "Los Angeles" name two clubs each, so a bare city that could
    mean either is left out rather than resolved to whichever came first; ESPN
    spells those two out in full anyway.
    """
    candidates: dict[str, list[TeamRef]] = {}
    for (team_id, location, nickname, abbreviation), team in zip(
        _TEAM_TABLE, NFL_TEAMS
    ):
        for value in (team.name, location, nickname, abbreviation):
            key = normalize_name(value)
            if not key:
                continue
            known = candidates.setdefault(key, [])
            if team not in known:
                known.append(team)
    index = {key: teams[0] for key, teams in candidates.items() if len(teams) == 1}
    by_abbreviation = {team.abbreviation: team for team in NFL_TEAMS}
    for alias, abbreviation in _TEAM_ALIASES.items():
        index.setdefault(normalize_name(alias), by_abbreviation[abbreviation])
    return index


_ARTICLE_RE = re.compile(r"^\s*the\s+", re.IGNORECASE)


def find_team(text: str) -> TeamRef | None:
    """The club a fragment of transaction prose names, if it names one."""
    key = normalize_name(_ARTICLE_RE.sub("", text or ""))
    if not key:
        return None
    index = _team_index()
    exact = index.get(key)
    if exact is not None:
        return exact
    # ESPN sometimes trails the name with a condition, as in "Chicago Bears
    # pending a physical", so the longest name inside the fragment wins.
    matches = [
        (name, team)
        for name, team in index.items()
        if re.search(rf"\b{re.escape(name)}\b", key)
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[0]))[1]


# Words that end a sentence in a transaction description without ending a name.
_NAME_SUFFIXES = frozenset({"jr", "sr", "st", "no"})
_SENTENCE_END_RE = re.compile(r"\.(?=\s+[A-Z])")
_INITIALS_RE = re.compile(r"(?:[A-Z]\.)*[A-Z]$")
_LAST_TOKEN_RE = re.compile(r"([A-Za-z.]+)$")


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


@dataclass(frozen=True)
class TradeSide:
    """What one club got out of a trade."""

    players: tuple[PlayerRef, ...] = ()
    # Picks and cash in ESPN's own wording. A pick is described loosely enough
    # ("a conditional 2023 sixth-round draft pick which could become a
    # fifth-round pick") that restating it as structured data would either lose
    # the condition or invent one, so the phrase is carried through as written.
    assets: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.players and not self.assets


@dataclass(frozen=True)
class Trade:
    """A trade told from the Ravens' side.

    ``incoming`` is always what Baltimore received and ``outgoing`` what it gave
    up, whichever way round ESPN happened to write the sentence.
    """

    incoming: TradeSide
    outgoing: TradeSide
    partner: TeamRef | None = None
    # Moves described in the same item that are not part of the deal, such as
    # the roster shuffle a club files alongside it.
    other_moves: str = ""

    @property
    def partner_phrase(self) -> str | None:
        """The partner as it reads mid-sentence, e.g. "the Chicago Bears".

        A club matched against the directory takes an article; a name ESPN
        wrote that no club matched is repeated exactly as it came.
        """
        if self.partner is None:
            return None
        if self.partner.team_id:
            return f"the {self.partner.name}"
        return self.partner.name

    @property
    def headline(self) -> str:
        """The move's title, leading with whoever the Ravens got."""
        partner = self.partner_phrase
        if self.incoming.players:
            who = _name_list(self.incoming.players)
            return (
                f"Trade — Ravens acquire {who} from {partner}"
                if partner
                else f"Trade — Ravens acquire {who}"
            )
        if self.outgoing.players:
            who = _name_list(self.outgoing.players)
            return (
                f"Trade — Ravens send {who} to {partner}"
                if partner
                else f"Trade — Ravens send {who}"
            )
        return f"Trade with {partner}" if partner else "Trade"


# Past three names a title stops reading as a list, the way a mass move is
# summarised rather than enumerated.
MAX_HEADLINE_PLAYERS = 3


def _name_list(players: tuple[PlayerRef, ...]) -> str:
    if len(players) > MAX_HEADLINE_PLAYERS:
        return f"{len(players)} players"
    return ", ".join(player.display_name for player in players)


_TRADE_OPENING_RE = re.compile(
    r"^(?P<verb>Traded?|Acquired|Received)\b\s*(?P<body>.+)$", re.S
)
_TO_RE = re.compile(r"\s+to\s+", re.IGNORECASE)
_FROM_RE = re.compile(r"\s+from\s+", re.IGNORECASE)
# Some entries name the partner instead of the return side: "Received a 2026
# sixth-round pick in a trade with Philadelphia." Nothing came back the other
# way in the sentence, and the words themselves prove a deal was struck, so this
# is read before the verb's own marker.
_TRADE_WITH_RE = re.compile(r"\s+(?:in|from)\s+an?\s+trade\s+with\s+", re.IGNORECASE)
# The return side opens with "in exchange for" or plain "for". No NFL club name
# contains either, so the partner never runs past this into the compensation.
# The marker itself is captured because a spelled-out exchange is proof a deal
# was struck, where a bare "for" is not.
_EXCHANGE_RE = re.compile(
    r",?\s+(in exchange for|in exchange with|in return for|for)\s+", re.IGNORECASE
)
# A waiver claim can also be written as an acquisition, and it is not a trade.
_WAIVERS_RE = re.compile(r"\bwaiver", re.IGNORECASE)
_CONNECTOR_HEAD_RE = re.compile(r"^(?:[\s,;]|\band\b|&|\bplus\b)+", re.IGNORECASE)
# Both boundaries matter: without the leading one, "Cleveland" ends in a
# connector and the partner comes back as "Clevel".
_CONNECTOR_TAIL_RE = re.compile(r"(?:[\s,;.]|\band\b|&)+$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s{2,}")


def parse_trade(description: str, players: tuple[PlayerRef, ...] = ()) -> Trade | None:
    """The two sides of a trade, or ``None`` when the move is not one.

    ``players`` are the description's players as they were already resolved
    against the roster, so the ids and headshots recovered there carry into
    whichever side of the deal each player ended up on.
    """
    return _parse_trade(description or "", tuple(players))


@lru_cache(maxsize=512)
def _parse_trade(description: str, players: tuple[PlayerRef, ...]) -> Trade | None:
    sentences = split_sentences(description)
    for index, sentence in enumerate(sentences):
        parsed = _read_trade_sentence(sentence)
        if parsed is None:
            continue
        near_text, far_text, partner, outbound = parsed
        incoming_text = far_text if outbound else near_text
        outgoing_text = near_text if outbound else far_text
        rest = " ".join(sentences[:index] + sentences[index + 1 :]).strip()
        return Trade(
            incoming=_side(incoming_text, players),
            outgoing=_side(outgoing_text, players),
            partner=partner,
            other_moves=rest,
        )
    return None


def _split_exchange(text: str) -> tuple[str, str, bool]:
    """Split a clause at the return side, saying whether the split was explicit."""
    parts = _EXCHANGE_RE.split(text, maxsplit=1)
    if len(parts) != 3:
        return text, "", False
    return parts[0], parts[2], parts[1].lower().startswith("in ")


def _read_trade_sentence(
    sentence: str,
) -> tuple[str, str, TeamRef | None, bool] | None:
    """Split one sentence into the near side, the far side, and the partner.

    The near side is whatever the opening verb takes as its object — the asset
    leaving in "Traded ...", the asset arriving in "Acquired ..." and
    "Received ..." — and the far side is what came back the other way. The
    fourth value says which of the two the Ravens gave up, since only the verb
    knows and the caller cannot tell the sides apart on their own.

    An opening verb alone proves nothing, since "Acquired X from the practice
    squad" is not a deal with anybody, so a sentence has to name a club that
    exists or spell out an exchange before it is read as a trade.
    """
    match = _TRADE_OPENING_RE.match(sentence.strip())
    if match is None or _WAIVERS_RE.search(sentence):
        return None
    body = match.group("body").strip().rstrip(".").strip()
    # "Trade" without the d is one of several spellings ESPN has published, so
    # the stem is what decides the direction rather than the whole word.
    outbound = match.group("verb").lower().startswith("trade")
    named_deal = _TRADE_WITH_RE.split(body, maxsplit=1)
    if len(named_deal) == 2:
        near, partner_text = named_deal
        far, explicit = "", True
    else:
        parts = (_TO_RE if outbound else _FROM_RE).split(body, maxsplit=1)
        if len(parts) == 2:
            near, remainder = parts
            partner_text, far, explicit = _split_exchange(remainder)
        else:
            near, far, explicit = _split_exchange(body)
            partner_text = ""
    partner = _partner(partner_text)
    known_club = partner is not None and partner.team_id is not None
    if not known_club and not explicit:
        return None
    return near.strip(), far.strip(), partner, outbound


def _partner(text: str) -> TeamRef | None:
    """The other club, matched to the directory when its name is one ESPN knows."""
    cleaned = _tidy(text)
    if not cleaned:
        return None
    return find_team(cleaned) or TeamRef(name=cleaned)


def _side(text: str, players: tuple[PlayerRef, ...]) -> TradeSide:
    """One side of the deal, split into the players named and everything else."""
    if not text.strip():
        return TradeSide()
    named = tuple(player for player in players if player.name and player.name in text)
    return TradeSide(players=named, assets=_assets(text, named))


def _assets(text: str, players: tuple[PlayerRef, ...]) -> str:
    """What changed hands besides the players, in ESPN's wording.

    The players are struck out of the phrase rather than the picks being read
    out of it, so an unfamiliar asset — cash, a conditional pick, a swap — still
    survives into the post instead of being silently dropped.
    """
    remaining = text
    for player in players:
        if player.position:
            pattern = rf"\b{re.escape(player.position)}s?\s+{re.escape(player.name)}"
        else:
            pattern = re.escape(player.name)
        remaining = re.sub(pattern, "", remaining, count=1)
    return _tidy(remaining)


def _tidy(text: str) -> str:
    """Trim the punctuation and connectors left behind by a removal."""
    cleaned = _WHITESPACE_RE.sub(" ", text or "").strip()
    cleaned = _CONNECTOR_HEAD_RE.sub("", cleaned)
    cleaned = _CONNECTOR_TAIL_RE.sub("", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()
