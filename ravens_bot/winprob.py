"""The clock as a number, and what a scoreboard is worth with time left on it.

The fourth down model next door maximises expected points, which is the right
objective until the clock is short enough to decide the result. This module is
the smallest honest thing that knows better: a win probability for the team with
the ball, from the score, the time remaining, and what having the ball is worth.

The shape is the standard one. Points still to be scored behave like a random
walk, so their spread grows with the square root of the time left: a seven point
lead is comfortable with a minute to play and means very little in the first
quarter. Dividing the margin by that spread gives a z-score, and a logistic
curve turns it into a probability — the usual approximation to the normal
distribution, close enough that the difference never reaches a percentage point.

The ball itself is worth points, and those points belong in the margin. The
caller supplies them, because the expected points curve lives in
``fourthdown`` and this module is deliberately downstream of nothing. What this
module does add is that possession is only worth what there is time to use: a
first down at the opponent's twenty is worth four points with a quarter to play
and almost nothing with four seconds left, so the value is discounted towards
zero as the clock runs out.

Two things this is not. It is not ``nfl4th``, which is a gradient boosted model
trained on play by play data and reads the closing spread to know how good the
teams are; this has no training data, no teams, and no runtime inputs. And it
does not know timeouts, because ESPN's scoreboard ``situation`` block does not
publish them — a two minute drill with three timeouts and one with none are the
same game here.
"""

from __future__ import annotations

import math
import re


PERIOD_SECONDS = 15 * 60
REGULATION_PERIODS = 4
GAME_SECONDS = REGULATION_PERIODS * PERIOD_SECONDS
HALF_PERIODS = 2
# The standard deviation of the points still to be scored over a whole game,
# which is what a margin is measured against.
FULL_GAME_SCORE_SPREAD = 13.5
# Logistic curves approximate the normal distribution at this scale.
LOGISTIC_SCALE = 1.702
# Under this much time the spread stops shrinking, so a one point lead with two
# seconds left is a near certainty rather than an arithmetic certainty.
MIN_EFFECTIVE_SECONDS = 6.0
# About how long a scoring drive takes, and so how much time possession needs
# before it is worth its full expected points.
TYPICAL_DRIVE_SECONDS = 150.0
# Probabilities are never quoted as certainties: a game is not over until it is.
MIN_WIN_PROBABILITY = 0.001
MAX_WIN_PROBABILITY = 0.999


def parse_clock_seconds(clock: str | None) -> int | None:
    """Seconds left in the period, read from ESPN's display clock.

    ESPN writes the clock the way a stadium does — "5:21", "0:04", and under a
    minute sometimes with tenths — and writes nothing at all between periods. A
    clock this cannot read is treated as no clock rather than as zero, since
    zero is a real and very different situation.
    """
    if not clock:
        return None
    text = clock.strip()
    match = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2})(?:\.\d+)?", text)
    if match is None:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2))
    if seconds >= 60 and match.group(1) is not None:
        return None
    total = minutes * 60 + seconds
    return total if 0 <= total <= PERIOD_SECONDS else None


def seconds_remaining_in_game(period: int | None, clock_seconds: int | None) -> int | None:
    """Time left to play, counting the periods after this one.

    Overtime has no fixed length that the scoreboard states — ten minutes in the
    regular season, fifteen in January, and sudden death can end it sooner — so
    an overtime clock stands for itself.
    """
    if period is None or clock_seconds is None or period < 1:
        return None
    if period > REGULATION_PERIODS:
        return clock_seconds
    return (REGULATION_PERIODS - period) * PERIOD_SECONDS + clock_seconds


def seconds_remaining_in_half(period: int | None, clock_seconds: int | None) -> int | None:
    """Time left before the break, or before the end in the second half."""
    if period is None or clock_seconds is None or period < 1:
        return None
    if period > REGULATION_PERIODS:
        return clock_seconds
    last = HALF_PERIODS if period <= HALF_PERIODS else REGULATION_PERIODS
    return (last - period) * PERIOD_SECONDS + clock_seconds


def possession_value(expected_points: float, seconds_remaining: float) -> float:
    """What the ball is worth with this much time left, in points.

    Expected points assume there is time for the drive they describe. Late in a
    game there is not, so the value is scaled down to what the clock allows and
    reaches zero as the clock does.
    """
    if seconds_remaining <= 0:
        return 0.0
    share = min(1.0, seconds_remaining / TYPICAL_DRIVE_SECONDS)
    return expected_points * share


def win_probability(
    score_differential: float,
    seconds_remaining: float,
    possession_points: float = 0.0,
) -> float:
    """The chance the team with the ball wins, between 0 and 1.

    ``score_differential`` is stated from that team's point of view, so a team
    trailing by four passes ``-4``. ``possession_points`` is what holding the
    ball is worth, already discounted for the time left; pass zero for a
    scoreboard with no ball attached to it, such as a kickoff about to happen.
    """
    if seconds_remaining <= 0:
        if score_differential > 0:
            return MAX_WIN_PROBABILITY
        if score_differential < 0:
            return MIN_WIN_PROBABILITY
        # Tied at zero is overtime, which starts as a coin toss.
        return 0.5
    spread = FULL_GAME_SCORE_SPREAD * math.sqrt(
        max(seconds_remaining, MIN_EFFECTIVE_SECONDS) / GAME_SECONDS
    )
    margin = score_differential + possession_points
    exponent = LOGISTIC_SCALE * margin / spread
    # A large exponent overflows before it changes the answer.
    if exponent > 30:
        return MAX_WIN_PROBABILITY
    if exponent < -30:
        return MIN_WIN_PROBABILITY
    value = 1.0 / (1.0 + math.exp(-exponent))
    return min(MAX_WIN_PROBABILITY, max(MIN_WIN_PROBABILITY, value))
