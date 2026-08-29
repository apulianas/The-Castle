from __future__ import annotations

from datetime import date

from ravens_bot.espn import parse_transactions
from ravens_bot.formatting import format_roster_cut_blocks
from ravens_bot.roster_moves import extract_player_moves, position_group

TARGET_DATE = date(2025, 8, 26)

# The wording of a real cut down day, shortened but kept in ESPN's own shape: a
# waiver list, a release list, an injury settlement and two reserve placements,
# all filed as one description.
CUT_DOWN_DAY = (
    "Waived CBs Jalyn Armour-Davis and Marquise Robinson, WRs Jahmal Banks and "
    "Malik Cunningham, Gs Darrian Dalcourt and Jared Penning, C Nick Samac, "
    "LB Kaimon Rucker and QB Devin Leary. Released RB Myles Gaskin and DL Brent "
    "Urban. Waived WR Xaivier Guillory with an injury settlement. Placed WR "
    "Dayton Wade on injured reserve. Placed T Emery Jones Jr. on "
    "reserve/non-football injury."
)


def build(description: str) -> object:
    return parse_transactions({"items": [{"description": description}]}, TARGET_DATE)[0]


def test_position_group_reads_offense_defense_and_the_kicking_game() -> None:
    assert position_group("QB") == "Quarterbacks"
    assert position_group("C") == "Offensive line"
    assert position_group("EDGE") == "Defensive line"
    assert position_group("SS") == "Defensive backs"
    assert position_group("LS") == "Specialists"
    # ESPN files the odd move under a code that names no unit.
    assert position_group("ATH") == "Other"
    assert position_group(None) == "Other"


def test_each_player_carries_the_move_that_covered_them() -> None:
    """A cut list and an injured reserve placement share one description."""
    actions = {
        move.player.name: move.action for move in extract_player_moves(CUT_DOWN_DAY)
    }

    assert actions["Devin Leary"] == "waived"
    assert actions["Myles Gaskin"] == "released"
    assert actions["Xaivier Guillory"] == "waived with an injury settlement"
    assert actions["Dayton Wade"] == "placed on injured reserve"
    assert actions["Emery Jones Jr."] == "placed on reserve/non-football injury"


def test_a_cut_list_is_grouped_offense_first_then_defense() -> None:
    blocks = format_roster_cut_blocks(build(CUT_DOWN_DAY))

    assert [name for name, _ in blocks] == [
        "Quarterbacks (1)",
        "Running backs (1)",
        "Wide receivers (4)",
        "Offensive line (4)",
        "Defensive line (1)",
        "Linebackers (1)",
        "Defensive backs (2)",
    ]


def test_every_player_gets_their_own_line() -> None:
    blocks = dict(format_roster_cut_blocks(build(CUT_DOWN_DAY)))

    assert blocks["Wide receivers (4)"] == [
        "Jahmal Banks — waived",
        "Malik Cunningham — waived",
        "Xaivier Guillory — waived with an injury settlement",
        "Dayton Wade — placed on injured reserve",
    ]


def test_a_position_leads_a_line_only_where_its_group_is_mixed() -> None:
    """A field headed "Offensive line" need not say "OL" on every line."""
    blocks = dict(format_roster_cut_blocks(build(CUT_DOWN_DAY)))

    # Two guards, a centre and a tackle, so the code tells them apart.
    assert blocks["Offensive line (4)"] == [
        "G Darrian Dalcourt — waived",
        "G Jared Penning — waived",
        "C Nick Samac — waived",
        "T Emery Jones Jr. — placed on reserve/non-football injury",
    ]
    # Every quarterback is a quarterback, so the code would only be noise.
    assert blocks["Quarterbacks (1)"] == ["Devin Leary — waived"]


def test_a_single_action_is_left_off_every_line() -> None:
    """Saying "waived" thirty times under a post titled "Waived" is an echo."""
    description = (
        "Waived CBs Jalyn Armour-Davis and Marquise Robinson, WRs Jahmal Banks "
        "and Malik Cunningham, G Darrian Dalcourt, C Nick Samac and QB Devin "
        "Leary."
    )

    blocks = dict(format_roster_cut_blocks(build(description)))

    assert blocks["Quarterbacks (1)"] == ["Devin Leary"]
    assert blocks["Defensive backs (2)"] == ["Jalyn Armour-Davis", "Marquise Robinson"]


def test_a_move_naming_nobody_has_no_blocks() -> None:
    assert format_roster_cut_blocks(build("Ravens roster transaction")) == []

def test_a_generational_suffix_keeps_the_stop_that_belongs_to_the_name() -> None:
    """"Jr" without its stop is a different name, and a list shows every one."""
    moves = extract_player_moves(CUT_DOWN_DAY)
    jones = [move for move in moves if move.player.name.startswith("Emery Jones")]

    assert [move.player.name for move in jones] == ["Emery Jones Jr."]
    assert jones[0].action == "placed on reserve/non-football injury"


def test_a_stop_that_only_ends_a_sentence_is_left_off_the_name() -> None:
    """The stop after "Leary" closes the sentence; it is not part of the name."""
    moves = extract_player_moves(CUT_DOWN_DAY)

    assert "Devin Leary" in [move.player.name for move in moves]
