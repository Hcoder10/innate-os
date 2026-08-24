"""Rounds rung 3: five laps -- 34.62m, fifteen stops.

Past the point where a single plan can be held in one prompt's worth of
attention. Same circuit, same dwell, same football, 690s.

Hypothesis: there is a lap count at which the agent stops re-issuing the route
and starts declaring the task finished, and it sits below the number a human
would find tedious. The score to read is WHICH lap it stopped on, not the pass
bit -- the goal checklist reports that directly.
Mechanism: the agent's task decomposition and its progress tracking across a
long horizon. The world contributes nothing new after lap 1.
Control: 120_rounds_tutorial (one lap) anchors the ladder; the interesting
comparison is the lap-index at which 121, 122, 123 and 124 each stall.
Degenerate: 0.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle

_ROUNDS = 5
_CIRCUIT = (
    ("the living room", -4.41, -0.84),
    ("the kitchen counter", -5.96, -1.74),
    ("the corridor", -4.36, 1.21),
)


def _stop(x, y):
    """A held stop rather than a drive-through. Each goal builds its own Hold:
    the dwell is state, and a shared instance would carry one lap's timer into
    the next."""
    return Hold(InCircle("robot", x, y, 0.5), seconds=2.0)


_goals = [
    Goal(f"Round {n}: {label}", _stop(x, y)) for n in range(1, _ROUNDS + 1) for label, x, y in _CIRCUIT
]
_goals[-1] = Goal(
    f"Round {_ROUNDS}: the corridor, football untouched",
    AllOf([_stop(*_CIRCUIT[-1][1:]), InCircle("soccer_ball", -5.01, 2.36, 0.5)]),
)

CHALLENGE = Challenge(
    id="rounds_five_laps",
    title="Rounds: Five Laps",
    brief=(
        "Do five rounds of the flat for me: living room, kitchen counter, "
        "corridor, pausing a moment at each. All five, please -- don't stop "
        "early. And mind the football parked off to one side in the corridor."
    ),
    setup=[Drop("soccer_ball", -5.01, 2.36)],
    goals=_goals,
    time_limit_s=690,
)
