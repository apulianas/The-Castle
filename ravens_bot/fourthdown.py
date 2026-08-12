"""Whether the team with the ball should go for it, kick, or punt.

The published fourth down bot, ``nfl4th``, is an R package, and nflverse
distributes no per-situation decision feed, so there is nothing to read the
answer from: the arithmetic has to happen here. What follows is a deliberately
small expected points model, built from four league-average curves and no
runtime data at all, so it is pure, offline, and testable.

The four curves, and where their shape comes from:

- **Conversion rate by distance.** Fourth down conversion rates by yards to go,
  as published by nflfastR play-by-play summaries and reproduced in nfl4th's
  documentation: a little over two thirds on fourth-and-1, about half at three,
  and a slow decay to roughly one attempt in six past fifteen. Goal line tries
  convert slightly less often than the same distance in open field, because the
  defence has no space behind it to cover.
- **Field goal rate by kick distance.** The kick is snapped seven yards back and
  the ball is spotted ten yards deep in the end zone, so the attempt is the
  distance to the goal line plus seventeen. Modern league-wide rates are near
  certain inside thirty yards, a little under nine in ten at forty, seven in ten
  at fifty, and fall away past sixty, beyond which the attempt is treated as out
  of range.
- **Punt outcome by field position.** From a team's own end the punt nets about
  forty yards. Nearer the opposing end zone the punter runs out of room, so the
  return team's average start rises off the floor to around their own ten rather
  than the net continuing to grow.
- **Expected points by field position.** The value to a team of a first down at
  a given distance from the end zone, in points of the next score. This is the
  usual expected points curve: a shade below zero backed up against one's own
  goal line, around one point at the twenty, two near midfield, and rising to
  the value of a touchdown at the goal line.

Two limits are worth stating plainly, because a recommendation that hides them
is worse than none:

- The model is score-blind and clock-blind. It maximises expected points, which
  is the right objective for most of a game and the wrong one when the clock is
  about to decide the result — a team down eight with a minute left should go
  for it on fourth-and-goal from anywhere, and this model would not say so. Such
  situations are flagged as caveats rather than answered confidently. The proper
  fix is a win probability model, which is a separate piece of work.
- It knows nothing about the two teams. Every number here is a league average,
  so a great offence facing a poor defence is understated, and vice versa.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import GameSituation


GO = "go"
FIELD_GOAL = "field goal"
PUNT = "punt"

# The snap goes seven yards back and the holder spots it ten yards in front of
# the end line, so the kick is always this much longer than the yards to goal.
FIELD_GOAL_OVERHEAD = 17
# Beyond this the attempt is a coin toss with field position, not a field goal.
MAX_FIELD_GOAL_YARDS = 66
# A missed kick hands the ball back at the spot of the kick, or the twenty,
# whichever is further from the kicking team's goal line.
MISSED_FIELD_GOAL_FLOOR = 20
TOUCHDOWN_POINTS = 6.95
FIELD_GOAL_POINTS = 3.0
# Beneath this gap in expected points the two options are the same call.
CLOSE_CALL_POINTS = 0.15
# Inside this many yards a goal line try is harder than the same distance would
# be in open field, since there is no room behind the defence.
GOAL_LINE_PENALTY = 0.9

# Yards to go -> share of fourth down attempts converted.
CONVERSION_RATES: tuple[tuple[int, float], ...] = (
    (1, 0.68),
    (2, 0.55),
    (3, 0.50),
    (4, 0.45),
    (5, 0.42),
    (6, 0.38),
    (7, 0.35),
    (8, 0.33),
    (9, 0.31),
    (10, 0.30),
    (12, 0.26),
    (15, 0.20),
    (20, 0.14),
    (30, 0.08),
)

# Kick distance in yards -> share made.
FIELD_GOAL_RATES: tuple[tuple[int, float], ...] = (
    (20, 0.99),
    (25, 0.97),
    (30, 0.95),
    (35, 0.92),
    (40, 0.88),
    (45, 0.82),
    (50, 0.72),
    (55, 0.58),
    (60, 0.40),
    (63, 0.26),
    (MAX_FIELD_GOAL_YARDS, 0.15),
)

# Yards to goal at the punt -> the receiving team's average starting yard line,
# measured from their own goal.
PUNT_RESULTS: tuple[tuple[int, float], ...] = (
    (30, 8.0),
    (40, 10.0),
    (50, 13.0),
    (60, 19.0),
    (70, 29.0),
    (80, 39.0),
    (90, 49.0),
    (99, 58.0),
)

# Yards to goal -> expected points of the next score for the team in possession.
EXPECTED_POINTS: tuple[tuple[int, float], ...] = (
    (1, 6.3),
    (5, 5.8),
    (10, 5.2),
    (20, 4.3),
    (30, 3.7),
    (40, 3.0),
    (50, 2.3),
    (60, 1.9),
    (70, 1.5),
    (80, 1.0),
    (90, 0.5),
    (95, 0.1),
    (99, -0.4),
)


def _interpolate(table: tuple[tuple[int, float], ...], value: float) -> float:
    """A table read straight, with a straight line between its entries."""
    first_x, first_y = table[0]
    if value <= first_x:
        return first_y
    previous_x, previous_y = first_x, first_y
    for point_x, point_y in table[1:]:
        if value <= point_x:
            span = point_x - previous_x
            if span <= 0:
                return point_y
            share = (value - previous_x) / span
            return previous_y + share * (point_y - previous_y)
        previous_x, previous_y = point_x, point_y
    return previous_y


def conversion_rate(distance: int, goal_to_go: bool = False) -> float:
    rate = _interpolate(CONVERSION_RATES, max(distance, 1))
    return rate * GOAL_LINE_PENALTY if goal_to_go else rate


def field_goal_distance(yards_to_goal: int) -> int:
    return yards_to_goal + FIELD_GOAL_OVERHEAD


def field_goal_rate(kick_distance: int) -> float:
    if kick_distance > MAX_FIELD_GOAL_YARDS:
        return 0.0
    return _interpolate(FIELD_GOAL_RATES, kick_distance)


def expected_points(yards_to_goal: float) -> float:
    """Points of the next score for a team first and ten at this spot."""
    if yards_to_goal <= 0:
        return TOUCHDOWN_POINTS
    return _interpolate(EXPECTED_POINTS, min(yards_to_goal, 99))


def punt_result(yards_to_goal: int) -> float:
    """The receiving team's average start, as their own yard line."""
    return _interpolate(PUNT_RESULTS, yards_to_goal)


def _opponent_value(their_yards_to_goal: float) -> float:
    """What handing the ball over is worth to us, which is its negative."""
    return -expected_points(their_yards_to_goal)


@dataclass(frozen=True)
class Option:
    """One of the three things a team can do, and what it is worth."""

    kind: str
    label: str
    expected_points: float
    detail: str


@dataclass(frozen=True)
class FourthDownAdvice:
    situation: GameSituation
    options: tuple[Option, ...] = ()
    caveats: tuple[str, ...] = ()
    # Set when the situation cannot be answered at all.
    reason: str | None = None

    @property
    def can_advise(self) -> bool:
        return bool(self.options)

    @property
    def best(self) -> Option | None:
        return self.options[0] if self.options else None

    @property
    def margin(self) -> float | None:
        """The gap to the next best option, in expected points."""
        if len(self.options) < 2:
            return None
        return self.options[0].expected_points - self.options[1].expected_points

    @property
    def is_close(self) -> bool:
        margin = self.margin
        return margin is not None and margin < CLOSE_CALL_POINTS


def _go_option(yards_to_goal: int, distance: int) -> Option:
    goal_to_go = distance >= yards_to_goal
    rate = conversion_rate(distance, goal_to_go)
    if goal_to_go:
        success = TOUCHDOWN_POINTS
        detail_success = "touchdown"
    else:
        success = expected_points(yards_to_goal - distance)
        detail_success = "first down"
    # A failed attempt leaves the ball where it is, facing the other way.
    failure = _opponent_value(100 - yards_to_goal)
    value = rate * success + (1 - rate) * failure
    return Option(
        kind=GO,
        label="Go for it",
        expected_points=value,
        detail=f"{round(rate * 100)}% convert → {detail_success}",
    )


def _field_goal_option(yards_to_goal: int) -> Option:
    kick = field_goal_distance(yards_to_goal)
    rate = field_goal_rate(kick)
    if rate <= 0:
        return Option(
            kind=FIELD_GOAL,
            label="Field goal",
            expected_points=float("-inf"),
            detail=f"{kick} yards is out of range",
        )
    # A miss spots the ball at the kick, unless that is inside the twenty.
    their_start = max(MISSED_FIELD_GOAL_FLOOR, yards_to_goal + 7)
    miss = _opponent_value(100 - their_start)
    value = rate * FIELD_GOAL_POINTS + (1 - rate) * miss
    return Option(
        kind=FIELD_GOAL,
        label="Field goal",
        expected_points=value,
        detail=f"{kick}-yard attempt, {round(rate * 100)}% made",
    )


def _punt_option(yards_to_goal: int) -> Option:
    their_start = punt_result(yards_to_goal)
    value = _opponent_value(100 - their_start)
    return Option(
        kind=PUNT,
        label="Punt",
        expected_points=value,
        detail=f"opponent starts around their own {round(their_start)}",
    )


def _caveats(situation: GameSituation) -> tuple[str, ...]:
    """Where maximising points stops being the right objective."""
    notes: list[str] = []
    period = situation.period or 0
    if period >= 4:
        notes.append(
            "Fourth quarter: this model maximises points and ignores the clock "
            "and the scoreboard, which are what decide a late call."
        )
    elif period == 2:
        notes.append(
            "Second quarter: the model does not know how much time is left in "
            "the half, and a call before the break can turn on it."
        )
    if situation.score_differential is not None and abs(situation.score_differential) > 8 and period >= 3:
        notes.append(
            "The margin is more than one score, which changes the objective "
            "away from expected points."
        )
    return tuple(notes)


def advise(situation: GameSituation) -> FourthDownAdvice:
    """Rank the three options, or say why the situation cannot be judged."""
    if not situation.is_fourth_down:
        down = situation.down
        reason = (
            "This is not a fourth down."
            if down
            else "ESPN has not published a down for this game yet."
        )
        return FourthDownAdvice(situation=situation, reason=reason)

    distance = situation.distance
    yards_to_goal = situation.yards_to_goal
    if distance is None or yards_to_goal is None:
        return FourthDownAdvice(
            situation=situation,
            reason="ESPN has not published a distance and ball spot for this down yet.",
        )
    if not 1 <= yards_to_goal <= 99 or distance < 1:
        return FourthDownAdvice(
            situation=situation,
            reason="ESPN reported a ball spot this model cannot read.",
        )

    distance = min(distance, yards_to_goal)
    options = [
        _go_option(yards_to_goal, distance),
        _field_goal_option(yards_to_goal),
        _punt_option(yards_to_goal),
    ]
    options.sort(key=lambda option: option.expected_points, reverse=True)
    return FourthDownAdvice(
        situation=situation,
        options=tuple(options),
        caveats=_caveats(situation),
    )
