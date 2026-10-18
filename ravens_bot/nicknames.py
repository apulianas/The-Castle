"""Common short forms of given names.

The club's graphics and ESPN's roster do not always write a player's given name
the same way: the Week 6 inactives graphic printed "MATTHEW HIBNER" for the
roster's "Matt Hibner". Each form here is one step from another (Matt to
Matthew, Matthew to Matt or Matty), so two formal names that share a short form,
such as Albert and Alan for "Al", are never treated as the same name.
"""
from __future__ import annotations

from .models import normalize_name


# Formal given names, and the short forms written in their place.
SHORT_FORMS: dict[str, tuple[str, ...]] = {
    "abraham": ("abe",),
    "alan": ("al",),
    "albert": ("al", "bert"),
    "alexander": ("al", "alex"),
    "alfred": ("al", "fred"),
    "allen": ("al",),
    "andrew": ("andy", "drew"),
    "anthony": ("tony",),
    "antonio": ("tony",),
    "arthur": ("art",),
    "benjamin": ("ben", "benny"),
    "bradley": ("brad",),
    "cameron": ("cam",),
    "charles": ("charlie", "chuck"),
    "christian": ("chris",),
    "christopher": ("chris",),
    "clifford": ("cliff",),
    "daniel": ("dan", "danny"),
    "david": ("dave",),
    "dominic": ("dom",),
    "dominick": ("dom",),
    "donald": ("don", "donnie"),
    "douglas": ("doug",),
    "edward": ("ed", "eddie", "ted"),
    "edwin": ("ed",),
    "elijah": ("eli",),
    "eugene": ("gene",),
    "ezekiel": ("zeke",),
    "francis": ("frank",),
    "franklin": ("frank",),
    "frederick": ("fred", "freddie", "freddy"),
    "gabriel": ("gabe",),
    "geoffrey": ("geoff",),
    "gerald": ("jerry",),
    "gregory": ("greg",),
    "harold": ("hal", "harry"),
    "henry": ("hank", "harry"),
    "isaac": ("ike",),
    "isaiah": ("zay",),
    "jacob": ("jake",),
    "james": ("jamie", "jim", "jimmy"),
    "jeffery": ("jeff",),
    "jeffrey": ("jeff",),
    "jerome": ("jerry",),
    "john": ("johnny",),
    "jonathan": ("johnny", "jon"),
    "jonathon": ("jon",),
    "joseph": ("joe", "joey"),
    "joshua": ("josh",),
    "kenneth": ("ken", "kenny"),
    "lawrence": ("larry",),
    "leonard": ("len", "lenny"),
    "louis": ("lou",),
    "matthew": ("matt", "matty"),
    "maxwell": ("max",),
    "michael": ("mike", "mikey"),
    "mitchell": ("mitch",),
    "nathan": ("nate",),
    "nathaniel": ("nate",),
    "nicholas": ("nick", "nicky"),
    "nicolas": ("nick",),
    "patrick": ("pat",),
    "peter": ("pete",),
    "philip": ("phil",),
    "phillip": ("phil",),
    "randall": ("randy",),
    "randolph": ("randy",),
    "raymond": ("ray",),
    "reginald": ("reggie",),
    "richard": ("rich", "richie", "rick", "ricky"),
    "robert": ("bob", "bobby", "rob", "robbie"),
    "rodney": ("rod",),
    "ronald": ("ron", "ronnie"),
    "samuel": ("sam", "sammy"),
    "stanley": ("stan",),
    "stephen": ("steve",),
    "steven": ("steve",),
    "terence": ("terry",),
    "terrance": ("terry",),
    "terrence": ("terry",),
    "theodore": ("ted", "teddy", "theo"),
    "thomas": ("tom", "tommy"),
    "timothy": ("tim", "timmy"),
    "tyler": ("ty",),
    "vincent": ("vince",),
    "walter": ("walt",),
    "william": ("bill", "billy", "will", "willie"),
    "xavier": ("zay",),
    "zachariah": ("zach",),
    "zachary": ("zach", "zack", "zak"),
}


def _one_step_forms() -> dict[str, tuple[str, ...]]:
    forms: dict[str, set[str]] = {}
    for formal, shorts in SHORT_FORMS.items():
        for short in shorts:
            forms.setdefault(formal, set()).add(short)
            forms.setdefault(short, set()).add(formal)
    return {name: tuple(sorted(others)) for name, others in forms.items()}


_FORMS = _one_step_forms()


def given_name_forms(given_name: str) -> tuple[str, ...]:
    """The other common ways of writing ``given_name``, in lower case."""
    return _FORMS.get(normalize_name(given_name), ())


def name_variants(name: str) -> tuple[str, ...]:
    """``name`` with its given name written each other common way.

    "Matt Hibner" also reads "Matthew Hibner", and "Mike Hall Jr." also reads
    "Michael Hall Jr."; the surname and any suffix stay as written.
    """
    given, _, rest = name.strip().partition(" ")
    rest = rest.strip()
    if not rest:
        return ()
    return tuple(f"{form.capitalize()} {rest}" for form in given_name_forms(given))
