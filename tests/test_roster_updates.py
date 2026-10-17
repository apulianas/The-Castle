from dataclasses import replace
from datetime import date

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


def transaction(description):
    return parse_transactions(
        {"items": [{"description": description}]}, date(2026, 10, 17),
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
