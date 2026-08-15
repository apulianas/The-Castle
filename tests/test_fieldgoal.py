from __future__ import annotations

from datetime import datetime, timezone

from ravens_bot.embeds import field_goal_embed, no_field_goal_embed
from ravens_bot.formatting import (
    format_field_goal,
    format_field_goal_call,
    format_field_goal_detail,
    format_no_ball_spot,
    format_no_field_goal_spot,
)
from ravens_bot.fourthdown import (
    MAX_FIELD_GOAL_YARDS,
    field_goal_outlook,
    field_goal_rate,
    yards_to_goal_for_kick,
)
from ravens_bot.models import Game, GameSituation, GameTeam, TeamRef


RAVENS = TeamRef(team_id="33", name="Baltimore Ravens", abbreviation="BAL", slug="bal")
BROWNS = TeamRef(team_id="5", name="Cleveland Browns", abbreviation="CLE", slug="cle")


def build_situation(yards_to_goal: int = 25) -> GameSituation:
    return GameSituation(
        possession=RAVENS,
        defense=BROWNS,
        down=4,
        distance=6,
        yards_to_goal=yards_to_goal,
        period=3,
        clock="2:00",
        spot="CLE 25",
    )


def build_game(situation: GameSituation | None = None) -> Game:
    return Game(
        event_id="401",
        name="Cleveland Browns at Baltimore Ravens",
        short_name="CLE @ BAL",
        start_time=datetime(2025, 11, 23, 18, 0, tzinfo=timezone.utc),
        status="3rd Quarter",
        home=GameTeam(team=RAVENS, is_home=True, score=17),
        away=GameTeam(team=BROWNS, score=13),
        state="in",
        situation=situation,
    )


def test_a_named_distance_is_read_straight() -> None:
    outlook = field_goal_outlook(kick_distance=52)

    assert outlook.kick_distance == 52
    assert outlook.make_rate == field_goal_rate(52)
    assert outlook.yards_to_goal == 35
    assert outlook.expected_points is not None


def test_a_ball_spot_adds_the_snap_and_the_hold() -> None:
    outlook = field_goal_outlook(yards_to_goal=25)

    assert outlook.kick_distance == 42
    assert outlook.yards_to_goal == 25


def test_a_kick_shorter_than_the_snap_and_the_hold_has_no_spot() -> None:
    assert yards_to_goal_for_kick(10) is None
    assert yards_to_goal_for_kick(18) == 1
    assert yards_to_goal_for_kick(117) is None

    outlook = field_goal_outlook(kick_distance=10)
    assert outlook.yards_to_goal is None
    assert outlook.expected_points is None


def test_an_out_of_range_kick_quotes_no_expected_points() -> None:
    outlook = field_goal_outlook(kick_distance=MAX_FIELD_GOAL_YARDS + 5)

    assert outlook.make_rate == 0.0
    assert outlook.in_range is False
    assert outlook.expected_points is None
    assert "out of range" in format_field_goal_detail(outlook)
    assert "out of range" in format_field_goal_call(outlook)


def test_a_kick_needs_a_distance_or_a_spot() -> None:
    try:
        field_goal_outlook()
    except ValueError as exc:
        assert "kick distance" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("field_goal_outlook accepted nothing to work from")


def test_the_call_states_the_distance_and_the_rate() -> None:
    call = format_field_goal_call(field_goal_outlook(kick_distance=40))

    assert call.startswith("40-yard field goal")
    assert "88% good" in call


def test_the_plain_text_answer_carries_the_game_and_the_situation() -> None:
    situation = build_situation()
    outlook = field_goal_outlook(yards_to_goal=25, situation=situation)

    text = format_field_goal(outlook, build_game(situation))

    assert "42-yard field goal" in text
    assert "Cleveland Browns at Baltimore Ravens" in text
    assert situation.summary in text
    assert "Ball at the CLE 25, which is 25 yards out" in text
    assert "expected points" in text


def test_a_kick_without_a_ball_spot_names_no_spot() -> None:
    detail = format_field_goal_detail(field_goal_outlook(kick_distance=52))

    assert "Ball at" not in detail
    assert "52 yards" in detail


def test_the_embed_uses_the_kicking_team_art_and_links_the_game() -> None:
    situation = build_situation()
    game = build_game(situation)

    embed = field_goal_embed(field_goal_outlook(yards_to_goal=25, situation=situation), game)

    assert embed.title is not None and "42-yard field goal" in embed.title
    assert embed.url is not None and "401" in embed.url
    assert embed.description is not None
    assert "Cleveland Browns at Baltimore Ravens" in embed.description


def test_a_hypothetical_kick_needs_no_game() -> None:
    embed = field_goal_embed(field_goal_outlook(kick_distance=60))

    assert embed.title is not None and "60-yard field goal" in embed.title
    assert embed.url is None


def test_the_fallback_messages_say_what_to_do_instead() -> None:
    assert "/fieldgoal" in format_no_field_goal_spot()
    assert "kick distance" in format_no_ball_spot(build_game())

    embed = no_field_goal_embed(format_no_field_goal_spot())
    assert embed.title == "Field goal"
