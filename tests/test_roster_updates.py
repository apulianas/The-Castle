from dataclasses import replace
from datetime import date, timedelta

import pytest

from ravens_bot.espn import parse_transactions
from ravens_bot.models import PlayerRef
from ravens_bot.roster_updates import move_details, prefer_roster_move, same_roster_move


FULL_REPORT = (
    "Activated WR Ja'Kobi Lane from Injured Reserve and signed LB Carl Jones "
    "from the practice squad to the 53-man roster. Activated T Chuma Edoga and "
    "G Kyle Hergel as standard practice squad elevations for tomorrow's game."
)
SHORT_REPORT = (
    "Activated WR Ja'Kobi Lane from injured reserve. "
    "Signed LB Carl Jones from the practice squad."
)
# The club's X post and ESPN's entry, hours apart, for the same two moves.
CLUB_POST = "Signed K Jake Moody to the Practice Squad and waived LB Carl Jones Jr."
ESPN_ENTRY = "Waived LB Carl Jones Jr. Signed K Jake Moody to the practice squad."
SATURDAY, SUNDAY = date(2026, 10, 10), date(2026, 10, 11)


def transaction(description, day=date(2026, 10, 17)):
    return parse_transactions(
        {"items": [{"description": description}]}, day,
    )[0]


@pytest.mark.parametrize("separator", [" and signed ", ", and signed ", "; signed ", ", signed "])
def test_compound_report_assigns_each_player_their_own_action(separator):
    report = FULL_REPORT.replace(" and signed ", separator)
    assert move_details(transaction(report)) == frozenset({
        ("jakobi lane", "activated", "injured reserve"),
        ("carl jones", "signed", "active roster"),
        ("chuma edoga", "elevated", "practice squad"),
        ("kyle hergel", "elevated", "practice squad"),
    })


def test_screenshot_reports_match_and_keep_the_fuller_report():
    full, short = transaction(FULL_REPORT), transaction(SHORT_REPORT)
    assert same_roster_move(full, short)
    assert same_roster_move(short, full)
    assert prefer_roster_move(full, short)
    assert not prefer_roster_move(short, full)


def test_signing_origin_is_not_its_destination():
    assert move_details(transaction("Signed LB Carl Jones from the practice squad.")) == {
        ("carl jones", "signed", ""),
    }
    active = transaction("Signed LB Carl Jones from the practice squad to the active roster.")
    practice = transaction("Signed LB Carl Jones to the practice squad.")
    assert not same_roster_move(active, practice)


def test_added_destination_is_preferred_without_losing_known_destination():
    short = transaction("Signed LB Carl Jones from the practice squad.")
    full = transaction("Signed LB Carl Jones to the active roster.")
    enriched_short = replace(short, players=(PlayerRef("Carl Jones", athlete_id="123"),))
    assert prefer_roster_move(full, short)
    assert not prefer_roster_move(enriched_short, full)


def test_added_player_does_not_justify_losing_known_destination():
    previous = transaction("Signed LB Carl Jones to the active roster.")
    candidate = transaction("Signed LB Carl Jones. Released WR Chris Moore.")
    assert same_roster_move(candidate, previous)
    assert not prefer_roster_move(candidate, previous)


@pytest.mark.parametrize("description", [
    "Released LB Carl Jones.",
    "Activated LB Carl Jones from injured reserve.",
    "Signed LB Carl Jones to the practice squad.",
])
def test_compound_report_does_not_hide_a_different_move(description):
    assert not same_roster_move(transaction(FULL_REPORT), transaction(description))


@pytest.mark.parametrize("entry", [ESPN_ENTRY, ESPN_ENTRY.replace(". Signed", ". signed")])
def test_a_suffix_ending_one_move_does_not_pass_that_move_to_the_next(entry):
    assert move_details(transaction(entry)) == {
        ("carl jones", "waived", ""),
        ("jake moody", "signed", "practice squad"),
    }


def test_espn_s_later_entry_matches_the_club_s_post_of_the_same_moves():
    post = replace(transaction(CLUB_POST), transaction_id="x:2112636664657146271")
    entry = transaction(ESPN_ENTRY)
    assert same_roster_move(entry, post)
    assert same_roster_move(post, entry)
    assert not same_roster_move(entry, transaction("Released LB Carl Jones."))


@pytest.mark.parametrize("left,right", [
    ("Signed LB Carl Jones to the practice squad.", "Signed LB Carl Jones Jr. to the practice squad."),
    ("Waiving QB Austin Reid.", "Waived QB Austin Reid."),
    ("Placed C Ethan Pocic on Reserve/Injured.", "Placed C Ethan Pocic on injured reserve."),
])
def test_spellings_the_sources_trade_between_are_one_move(left, right):
    assert move_details(transaction(left)) == move_details(transaction(right))
    assert same_roster_move(transaction(left), transaction(right))


def test_a_saturday_elevation_post_is_the_official_log_s_sunday_entry():
    post = transaction(
        "Activated T Chuma Edoga and G Kyle Hergel as standard practice squad "
        "elevations for tomorrow's game.", SATURDAY,
    )
    log = transaction(
        "Activated OL Chuma Edoga and G Kyle Hergel from the practice squad "
        "(standard elevation) at Atl.", SUNDAY,
    )
    espn = transaction(
        "Elevated LB Carl Jones and T Chuma Edoga from the practice squad. "
        "Released DB Lardarius Webb from the practice squad.", SATURDAY,
    )
    assert same_roster_move(post, log)
    assert same_roster_move(log, post)
    assert same_roster_move(espn, transaction(
        "Activated T Chuma Edoga from the practice squad (standard elevation).", SUNDAY,
    ))
    assert not same_roster_move(post, replace(log, date=SUNDAY + timedelta(days=1)))


@pytest.mark.parametrize("saturday,sunday", [
    # Practice squad churn: signed, released, and signed again a day later.
    ("Signed WR Chris Moore to the practice squad. Released WR Chris Moore "
     "from the practice squad.", "Signed WR Chris Moore to the practice squad."),
    # ESPN writes a promotion without its destination.
    ("Signed LB Carl Jones to the practice squad.", "Signed LB Carl Jones from the practice squad."),
    (ESPN_ENTRY, "Waived LB Carl Jones Jr."),
    # An elevation a day apart is matched, but not the other move it arrives with.
    ("Elevated T Chuma Edoga from the practice squad.",
     "Activated T Chuma Edoga (standard elevation). Placed WR Chris Moore on injured reserve."),
])
def test_a_day_apart_only_elevations_are_one_report(saturday, sunday):
    assert not same_roster_move(transaction(saturday, SATURDAY), transaction(sunday, SUNDAY))
    assert not same_roster_move(transaction(sunday, SUNDAY), transaction(saturday, SATURDAY))
