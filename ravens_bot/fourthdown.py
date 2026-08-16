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

Expected points is the right objective for most of a game and the wrong one once
the clock decides the result: a team down eight with a minute left should go for
it on fourth-and-goal from anywhere, and no arrangement of points curves says
so. So when the scoreboard and the clock are both known, each option's outcomes
are carried through to the game state they leave behind and scored in win
probability instead, using the model in ``winprob``. The curves above are what
weigh those outcomes either way; win probability is a layer on top of them
rather than a replacement for them. A situation with no clock, or no score, is
still ranked on expected points exactly as before.

Two limits are worth stating plainly, because a recommendation that hides them
is worse than none:

- It knows nothing about the two teams. Every number here is a league average,
  so a great offence facing a poor defence is understated, and vice versa. This
  is where ``nfl4th`` differs most: it reads the closing point spread to know
  who is playing, and this reads nothing at runtime.
- Nobody publishes timeouts on the scoreboard route this bot reads, so the win
  probability layer does not know them. Two minutes with three timeouts and two
  minutes with none are the same game here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import GameSituation
from .winprob import possession_value, win_probability


GO = "go"
FIELD_GOAL = "field goal"
PUNT = "punt"

# The snap goes seven yards back and the holder spots it ten yards in front of
# the end line, so the kick is always this much longer than the yards to goal.
FIELD_GOAL_OVERHEAD = 17
# Beyond this the attempt is a coin toss with field position, not a field goal.
MAX_FIELD_GOAL_YARDS = 66
# The ball on the one yard line is the shortest kick the rules allow.
MIN_FIELD_GOAL_YARDS = 1 + FIELD_GOAL_OVERHEAD
# A kick from a team's own end of the field is longer than anyone has made, but
# asking about one should still get an answer rather than an error.
LONGEST_ASKABLE_FIELD_GOAL = 99 + FIELD_GOAL_OVERHEAD
# A missed kick hands the ball back at the spot of the kick, or the twenty,
# whichever is further from the kicking team's goal line.
MISSED_FIELD_GOAL_FLOOR = 20
TOUCHDOWN_POINTS = 6.95
FIELD_GOAL_POINTS = 3.0
# Beneath this gap in expected points the two options are the same call.
CLOSE_CALL_POINTS = 0.15
# And beneath this gap in win probability, which is the same idea in the other
# currency: a percentage point either way is not a recommendation.
CLOSE_CALL_WIN_PROBABILITY = 0.01
# What each option takes off the clock, in seconds, snap to whistle.
GO_SECONDS = 6.0
FIELD_GOAL_SECONDS = 5.0
# A punt is a longer play than either, and the return costs more still.
PUNT_SECONDS = 12.0
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
class Scoreboard:
    """The score and the clock, which is what an option's outcomes land on.

    Held together because neither is any use alone: a two point lead means
    nothing without the time left to defend it.
    """

    score_differential: int
    seconds_remaining: float

    def after(self, seconds: float) -> "Scoreboard":
        """The same score with a play's worth of clock taken off it."""
        return Scoreboard(
            score_differential=self.score_differential,
            seconds_remaining=max(0.0, self.seconds_remaining - seconds),
        )

    def keeping_ball(self, yards_to_goal: float) -> float:
        """Our chance of winning, still holding the ball at this spot."""
        value = possession_value(expected_points(yards_to_goal), self.seconds_remaining)
        return win_probability(
            self.score_differential, self.seconds_remaining, value
        )

    def handing_over(self, their_yards_to_goal: float) -> float:
        """Our chance of winning once the other team has the ball.

        Football is zero sum, so this is read as their chance of winning from
        where they now stand, subtracted from one.
        """
        value = possession_value(
            expected_points(their_yards_to_goal), self.seconds_remaining
        )
        return 1.0 - win_probability(
            -self.score_differential, self.seconds_remaining, value
        )

    def scoring(self, points: float) -> float:
        """Our chance of winning having just scored, with the kickoff to come.

        The kickoff is not priced separately, because the points curves already
        are net of it: a touchdown is worth ``TOUCHDOWN_POINTS`` rather than
        seven precisely because the other team receives afterwards. Late in a
        game that average is generous to a team that scores and must then kick
        off with seconds left, which is the sharpest edge on this model.
        """
        return win_probability(
            self.score_differential + points, self.seconds_remaining
        )


@dataclass(frozen=True)
class Option:
    """One of the three things a team can do, and what it is worth."""

    kind: str
    label: str
    expected_points: float
    detail: str
    # Set when the clock and the score were both known, and so the option could
    # be carried through to the game state it leaves behind.
    win_probability: float | None = None


@dataclass(frozen=True)
class FourthDownAdvice:
    situation: GameSituation
    options: tuple[Option, ...] = ()
    caveats: tuple[str, ...] = ()
    # Set when the situation cannot be answered at all.
    reason: str | None = None
    # Whether the ranking is win probability or expected points, which is a
    # different question with a different answer and so is said out loud.
    ranked_by_win_probability: bool = False

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
    def win_probability_margin(self) -> float | None:
        """The gap to the next best option, in win probability."""
        if not self.ranked_by_win_probability or len(self.options) < 2:
            return None
        best, runner_up = self.options[0], self.options[1]
        if best.win_probability is None or runner_up.win_probability is None:
            return None
        return best.win_probability - runner_up.win_probability

    @property
    def is_close(self) -> bool:
        win_margin = self.win_probability_margin
        if win_margin is not None:
            return win_margin < CLOSE_CALL_WIN_PROBABILITY
        margin = self.margin
        return margin is not None and margin < CLOSE_CALL_POINTS


def _go_option(
    yards_to_goal: int, distance: int, scoreboard: Scoreboard | None = None
) -> Option:
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
    chance = None
    if scoreboard is not None:
        after = scoreboard.after(GO_SECONDS)
        # A touchdown ends the possession; a first down keeps the ball where
        # the runner stopped.
        if goal_to_go:
            won = after.scoring(TOUCHDOWN_POINTS)
        else:
            won = after.keeping_ball(yards_to_goal - distance)
        lost = after.handing_over(100 - yards_to_goal)
        chance = rate * won + (1 - rate) * lost
    return Option(
        kind=GO,
        label="Go for it",
        expected_points=value,
        detail=f"{round(rate * 100)}% convert → {detail_success}",
        win_probability=chance,
    )


def _field_goal_option(
    yards_to_goal: int, scoreboard: Scoreboard | None = None
) -> Option:
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
    chance = None
    if scoreboard is not None:
        after = scoreboard.after(FIELD_GOAL_SECONDS)
        made = after.scoring(FIELD_GOAL_POINTS)
        missed = after.handing_over(100 - their_start)
        chance = rate * made + (1 - rate) * missed
    return Option(
        kind=FIELD_GOAL,
        label="Field goal",
        expected_points=value,
        detail=f"{kick}-yard attempt, {round(rate * 100)}% made",
        win_probability=chance,
    )


def _punt_option(yards_to_goal: int, scoreboard: Scoreboard | None = None) -> Option:
    their_start = punt_result(yards_to_goal)
    value = _opponent_value(100 - their_start)
    chance = None
    if scoreboard is not None:
        chance = scoreboard.after(PUNT_SECONDS).handing_over(100 - their_start)
    return Option(
        kind=PUNT,
        label="Punt",
        expected_points=value,
        detail=f"opponent starts around their own {round(their_start)}",
        win_probability=chance,
    )


def _scoreboard(situation: GameSituation) -> Scoreboard | None:
    """The score and clock behind this down, when ESPN published both."""
    seconds = situation.seconds_remaining
    differential = situation.score_differential
    if seconds is None or differential is None:
        return None
    return Scoreboard(score_differential=differential, seconds_remaining=seconds)


def _caveats(situation: GameSituation) -> tuple[str, ...]:
    """Where maximising points stops being the right objective.

    Only reached when the answer is ranked on expected points, since these are
    the things the win probability layer exists to stop apologising for.
    """
    notes: list[str] = []
    period = situation.period or 0
    if period >= 4:
        notes.append(
            "Fourth quarter, and ESPN published no clock or score for this "
            "down: without them this falls back to maximising points, which is "
            "not what decides a late call."
        )
    elif period == 2:
        notes.append(
            "Second quarter, with no clock published for this down, so the "
            "model cannot see the break a call before it can turn on."
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
    scoreboard = _scoreboard(situation)
    options = [
        _go_option(yards_to_goal, distance, scoreboard),
        _field_goal_option(yards_to_goal, scoreboard),
        _punt_option(yards_to_goal, scoreboard),
    ]
    # A kick out of range has no win probability either: it keeps the minus
    # infinity that stops it winning a ranking it should never win.
    ranked_by_win_probability = scoreboard is not None
    if ranked_by_win_probability:
        options.sort(
            key=lambda option: (
                option.win_probability
                if option.win_probability is not None
                else float("-inf")
            ),
            reverse=True,
        )
    else:
        options.sort(key=lambda option: option.expected_points, reverse=True)
    return FourthDownAdvice(
        situation=situation,
        options=tuple(options),
        caveats=() if ranked_by_win_probability else _caveats(situation),
        ranked_by_win_probability=ranked_by_win_probability,
    )


@dataclass(frozen=True)
class FieldGoalOutlook:
    """What a single field goal attempt is worth, asked on its own.

    The same curves answer "should they kick" and "would this kick go in", so
    this is a thin reading of the model rather than a second one. A kick asked
    about in the abstract has no spot, so the expected points of attempting it —
    which depend on where a miss hands the ball over — are only filled in when
    the ball's position is known.
    """

    kick_distance: int
    make_rate: float
    yards_to_goal: int | None = None
    expected_points: float | None = None
    # Set when the kick was read off a live ball spot rather than given.
    situation: GameSituation | None = None

    @property
    def in_range(self) -> bool:
        return self.make_rate > 0


def yards_to_goal_for_kick(kick_distance: int) -> int | None:
    """The ball spot a kick of this length is taken from, when one exists."""
    yards = kick_distance - FIELD_GOAL_OVERHEAD
    return yards if 1 <= yards <= 99 else None


def field_goal_outlook(
    kick_distance: int | None = None,
    yards_to_goal: int | None = None,
    situation: GameSituation | None = None,
) -> FieldGoalOutlook:
    """The odds on a kick, given either its length or where the ball is.

    Callers pass the distance a person would say out loud — a "fifty two
    yarder" — or the yards to the goal line, which the snap and the spot make
    seventeen yards shorter than the kick.
    """
    if kick_distance is None:
        if yards_to_goal is None:
            raise ValueError("A field goal needs a kick distance or a ball spot.")
        kick_distance = field_goal_distance(yards_to_goal)
    if yards_to_goal is None:
        yards_to_goal = yards_to_goal_for_kick(kick_distance)
    rate = field_goal_rate(kick_distance)
    # A kick out of range has no expected points worth quoting: the model gives
    # it a value of minus infinity so it never wins a ranking, which is a
    # sorting device rather than a number about this kick.
    points = (
        _field_goal_option(yards_to_goal).expected_points
        if yards_to_goal is not None and rate > 0
        else None
    )
    return FieldGoalOutlook(
        kick_distance=kick_distance,
        make_rate=rate,
        yards_to_goal=yards_to_goal,
        expected_points=points,
        situation=situation,
    )
