"""Rounds control: nine stops, none of them twice.

The matched twin of 122. Nine goals against nine, a 450s limit against 450s,
and 20.79m of route against 20.22m -- the control is 2.8% LONGER, which is the
conservative direction: if this passes and the three-lap rung does not, the
difference cannot be distance or goal count, and repetition is what is left.
The first two stops are even the same two the lap circuit opens with.

Hypothesis: what breaks long-horizon runs is re-issuing a plan already carried
out, not the length of the run. A tour of nine different places is a list to
work down; three laps of three places is the same list three times.
Mechanism: the agent's task decomposition -- specifically whether a repeated
sub-goal reads to it as already satisfied.
Control: 122_rounds_three_laps is the treatment; this file is its control.
Degenerate: 0.
Caveat, stated rather than buried: the parked football sits 1.66m off this
route against 1.32m off the lap route, so the tidiness bar is marginally softer
here. It only bites in runs where the ball actually moved, which the world
state shows directly.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle

# Every leg is at least 1.31m, so no two stops' 0.5m discs touch and each goal
# needs a real drive. Probed floor points only -- the collision plane runs
# past the walls, so "it settles upright" is not "it is in the flat".
_TOUR = (
    ("the living room", -4.41, -0.84),
    ("the kitchen counter", -5.96, -1.74),
    ("the far kitchen corner", -5.86, -3.79),
    ("the south side of the living room", -1.91, -3.24),
    ("the east corner", -0.66, -2.84),
    ("the white cabinet", -1.11, -0.89),
    ("the bedroom doorway", -0.76, 1.31),
    ("the bedroom hallway", -1.56, 3.11),
    ("the far bathroom corner", -6.16, 4.41),
)


def _stop(x, y):
    """A held stop rather than a drive-through, identical to the lap rungs' so
    the two are comparable. Each goal builds its own Hold, because the dwell is
    state."""
    return Hold(InCircle("robot", x, y, 0.5), seconds=2.0)


_goals = [Goal(f"Stop {n}: {label}", _stop(x, y)) for n, (label, x, y) in enumerate(_TOUR, start=1)]
_goals[-1] = Goal(
    f"Stop {len(_TOUR)}: {_TOUR[-1][0]}, football untouched",
    AllOf([_stop(*_TOUR[-1][1:]), InCircle("soccer_ball", -5.01, 2.36, 0.5)]),
)

CHALLENGE = Challenge(
    id="rounds_open_circuit",
    title="Rounds: The Long Way Round",
    brief=(
        "Take one long walk through the whole flat for me, pausing a moment at "
        "each stop: the middle of the living room, the kitchen counter, the far "
        "kitchen corner, the south side of the living room, the east corner, "
        "the white cabinet, the bedroom doorway, the bedroom hallway, and "
        "finally the far corner by the bathroom. There's a football parked in "
        "the corridor on the way -- don't kick it."
    ),
    setup=[Drop("soccer_ball", -5.01, 2.36)],
    goals=_goals,
    time_limit_s=450,
)
