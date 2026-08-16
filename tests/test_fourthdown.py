from __future__ import annotations

import pytest

from ravens_bot.fourthdown import (
    FIELD_GOAL,
    GO,
    MAX_FIELD_GOAL_YARDS,
    PUNT,
    advise,
    conversion_rate,
    expected_points,
    field_goal_distance,
    field_goal_rate,
    punt_result,
)
from ravens_bot.models import GameSituation, TeamRef


RAVENS = TeamRef(name="Baltimore Ravens", team_id="33", abbreviation="BAL", slug="bal")
BENGALS = TeamRef(name="Cincinnati Bengals", team_id="4", abbreviation="CIN", slug="cin")


def situation(
    distance: int | None,
    yards_to_goal: int | None,
    down: int | None = 4,
    period: int = 1,
    score_differential: int | None = 0,
    clock: str | None = "8:12",
) -> GameSituation:
    return GameSituation(
        possession=RAVENS,
        defense=BENGALS,
        down=down,
        distance=distance,
        yards_to_goal=yards_to_goal,
        period=period,
        clock=clock,
        score_differential=score_differential,
    )


def best_kind(distance: int, yards_to_goal: int) -> str:
    advice = advise(situation(distance, yards_to_goal))
    assert advice.best is not None
    return advice.best.kind


def test_fourth_and_short_at_midfield_goes_for_it() -> None:
    assert best_kind(1, 50) == GO


def test_fourth_and_long_from_deep_punts() -> None:
    assert best_kind(15, 80) == PUNT


def test_fourth_and_three_in_range_kicks() -> None:
    assert best_kind(3, 10) == FIELD_GOAL


def test_fourth_and_goal_from_the_two_goes_for_it() -> None:
    assert best_kind(1, 2) == GO


def test_options_are_ranked_by_expected_points() -> None:
    advice = advise(situation(5, 60))

    values = [option.expected_points for option in advice.options]
    assert values == sorted(values, reverse=True)
    assert {option.kind for option in advice.options} == {GO, FIELD_GOAL, PUNT}


def test_margin_and_close_call_track_the_top_two() -> None:
    advice = advise(situation(5, 60))

    assert advice.margin == pytest.approx(
        advice.options[0].expected_points - advice.options[1].expected_points
    )
    assert advice.is_close is (advice.margin < 0.15)


def test_conversion_rate_falls_as_the_distance_grows() -> None:
    rates = [conversion_rate(distance) for distance in range(1, 21)]

    assert all(later <= earlier for earlier, later in zip(rates, rates[1:]))
    assert rates[0] > rates[-1]


def test_goal_to_go_converts_less_often_than_open_field() -> None:
    assert conversion_rate(3, goal_to_go=True) < conversion_rate(3)


def test_field_goal_rate_falls_with_distance_and_ends_out_of_range() -> None:
    rates = [field_goal_rate(distance) for distance in range(20, 65)]

    assert all(later <= earlier for earlier, later in zip(rates, rates[1:]))
    assert field_goal_rate(MAX_FIELD_GOAL_YARDS + 1) == 0.0


def test_field_goal_distance_adds_the_snap_and_the_spot() -> None:
    assert field_goal_distance(20) == 37


def test_out_of_range_field_goal_is_never_recommended() -> None:
    advice = advise(situation(4, 70))

    kicks = [option for option in advice.options if option.kind == FIELD_GOAL]
    assert kicks[0].expected_points == float("-inf")
    assert advice.best is not None and advice.best.kind != FIELD_GOAL
    assert "out of range" in kicks[0].detail


def test_expected_points_rise_towards_the_goal_line() -> None:
    values = [expected_points(yards) for yards in range(1, 100)]

    assert all(later <= earlier for earlier, later in zip(values, values[1:]))
    assert expected_points(1) > expected_points(50) > expected_points(99)


def test_punt_from_deep_gains_more_field_than_a_punt_near_the_goal() -> None:
    assert punt_result(80) > punt_result(40)
    # Punting from inside the opponent's forty pins them, and no further.
    assert punt_result(35) == pytest.approx(punt_result(30), abs=2.5)


def test_punting_inside_the_opponent_forty_is_not_the_call() -> None:
    assert best_kind(2, 35) != PUNT


def test_late_game_answer_reads_the_clock_rather_than_apologising() -> None:
    advice = advise(situation(2, 40, period=4, score_differential=-9))

    assert advice.can_advise
    assert advice.ranked_by_win_probability
    assert advice.caveats == ()
    assert all(option.win_probability is not None for option in advice.options[:2])


def test_a_down_with_no_clock_falls_back_and_says_so() -> None:
    advice = advise(situation(2, 40, period=4, score_differential=-9, clock=None))

    assert advice.can_advise
    assert not advice.ranked_by_win_probability
    assert all(option.win_probability is None for option in advice.options)
    assert any("clock" in caveat for caveat in advice.caveats)


def test_a_down_with_no_score_falls_back_to_expected_points() -> None:
    advice = advise(situation(5, 60, score_differential=None))
    points = advise(situation(5, 60, score_differential=None, clock=None))

    assert not advice.ranked_by_win_probability
    assert [option.kind for option in advice.options] == [
        option.kind for option in points.options
    ]


def test_first_half_call_carries_no_fourth_quarter_caveat() -> None:
    advice = advise(situation(2, 40, period=1, clock=None))

    assert advice.caveats == ()


def test_trailing_late_goes_for_it_since_nothing_else_can_win() -> None:
    advice = advise(
        situation(3, 3, period=4, clock="1:00", score_differential=-8)
    )

    assert advice.best is not None and advice.best.kind == GO
    assert not advice.is_close
    # A field goal leaves them two scores short with a minute to find them.
    kick = next(option for option in advice.options if option.kind == FIELD_GOAL)
    assert kick.expected_points > 0
    assert advice.best.win_probability is not None
    assert kick.win_probability is not None
    assert kick.win_probability < advice.best.win_probability


def test_a_field_goal_that_cannot_tie_ranks_below_going_for_it() -> None:
    advice = advise(
        situation(5, 5, period=4, clock="0:15", score_differential=-4)
    )

    kinds = [option.kind for option in advice.options]
    assert kinds.index(GO) < kinds.index(FIELD_GOAL)


def test_leading_late_punts_rather_than_risking_the_ball() -> None:
    advice = advise(
        situation(8, 80, period=4, clock="2:00", score_differential=3)
    )

    assert advice.best is not None and advice.best.kind == PUNT


def test_win_probabilities_answer_from_the_offence_point_of_view() -> None:
    trailing = advise(situation(1, 50, period=4, clock="5:00", score_differential=-7))
    leading = advise(situation(1, 50, period=4, clock="5:00", score_differential=7))

    best_trailing = trailing.best
    best_leading = leading.best
    assert best_trailing is not None and best_trailing.win_probability is not None
    assert best_leading is not None and best_leading.win_probability is not None
    assert best_trailing.win_probability < 0.5 < best_leading.win_probability


def test_the_expected_points_fallback_is_unchanged_by_the_clock() -> None:
    """A situation ESPN published no clock for answers exactly as it always did."""
    advice = advise(situation(3, 10, clock=None))

    assert not advice.ranked_by_win_probability
    assert [option.kind for option in advice.options] == [FIELD_GOAL, GO, PUNT]
    assert advice.margin == pytest.approx(
        advice.options[0].expected_points - advice.options[1].expected_points
    )
    assert advice.is_close is (advice.margin < 0.15)


def test_third_down_cannot_be_advised() -> None:
    advice = advise(situation(2, 40, down=3))

    assert not advice.can_advise
    assert advice.reason == "This is not a fourth down."


def test_missing_distance_cannot_be_advised() -> None:
    advice = advise(situation(None, 40))

    assert not advice.can_advise
    assert advice.reason is not None and "distance" in advice.reason


def test_missing_down_cannot_be_advised() -> None:
    advice = advise(situation(2, 40, down=None))

    assert not advice.can_advise
    assert advice.reason is not None and "down" in advice.reason


def test_impossible_ball_spot_cannot_be_advised() -> None:
    advice = advise(situation(2, 0))

    assert not advice.can_advise
    assert advice.reason is not None and "spot" in advice.reason
