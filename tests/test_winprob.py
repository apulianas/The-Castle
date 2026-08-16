from __future__ import annotations

import pytest

from ravens_bot.models import GameSituation, TeamRef
from ravens_bot.winprob import (
    GAME_SECONDS,
    parse_clock_seconds,
    possession_value,
    seconds_remaining_in_game,
    seconds_remaining_in_half,
    win_probability,
)


RAVENS = TeamRef(name="Baltimore Ravens", team_id="33", abbreviation="BAL", slug="bal")


def test_the_display_clock_reads_as_seconds() -> None:
    assert parse_clock_seconds("5:21") == 321
    assert parse_clock_seconds("15:00") == 900
    assert parse_clock_seconds("0:04") == 4
    assert parse_clock_seconds("0:04.3") == 4
    assert parse_clock_seconds("12") == 12


def test_a_clock_that_cannot_be_read_is_no_clock_rather_than_zero() -> None:
    assert parse_clock_seconds(None) is None
    assert parse_clock_seconds("") is None
    assert parse_clock_seconds("Halftime") is None
    assert parse_clock_seconds("20:00") is None
    assert parse_clock_seconds("5:75") is None


def test_time_left_counts_the_periods_still_to_come() -> None:
    assert seconds_remaining_in_game(1, 900) == GAME_SECONDS
    assert seconds_remaining_in_game(3, 321) == 900 + 321
    assert seconds_remaining_in_game(4, 0) == 0
    # Overtime has no stated length, so its clock stands for itself.
    assert seconds_remaining_in_game(5, 300) == 300
    assert seconds_remaining_in_game(None, 300) is None
    assert seconds_remaining_in_game(2, None) is None


def test_time_left_in_the_half_ends_at_the_break() -> None:
    assert seconds_remaining_in_half(1, 900) == 1800
    assert seconds_remaining_in_half(2, 321) == 321
    assert seconds_remaining_in_half(3, 900) == 1800
    assert seconds_remaining_in_half(5, 120) == 120


def test_a_lead_is_worth_more_as_the_clock_runs_down() -> None:
    early = win_probability(7, GAME_SECONDS)
    late = win_probability(7, 120)

    assert 0.5 < early < late < 1.0


def test_a_tied_game_is_a_coin_toss_and_trailing_is_the_mirror_of_leading() -> None:
    assert win_probability(0, 900) == pytest.approx(0.5)
    assert win_probability(-7, 600) == pytest.approx(1 - win_probability(7, 600))


def test_no_time_left_is_decided_by_the_scoreboard() -> None:
    assert win_probability(3, 0) > 0.99
    assert win_probability(-3, 0) < 0.01
    # Tied at zero is overtime, which has not started yet.
    assert win_probability(0, 0) == pytest.approx(0.5)


def test_having_the_ball_helps_and_helps_less_with_no_time_to_use_it() -> None:
    with_ball = win_probability(0, 600, possession_points=2.0)

    assert with_ball > 0.5
    assert possession_value(4.0, 600) == pytest.approx(4.0)
    assert possession_value(4.0, 15) < 1.0
    assert possession_value(4.0, 0) == 0.0


def test_a_situation_reads_its_own_clock_when_the_parser_did_not() -> None:
    situation = GameSituation(possession=RAVENS, period=3, clock="5:21")

    assert situation.period_seconds_remaining == 321
    assert situation.seconds_remaining == 900 + 321
    assert situation.half_seconds_remaining == 900 + 321


def test_a_situation_with_no_readable_clock_has_no_time_left_to_report() -> None:
    situation = GameSituation(possession=RAVENS, period=2, clock="Halftime")

    assert situation.period_seconds_remaining is None
    assert situation.seconds_remaining is None
    assert situation.half_seconds_remaining is None
