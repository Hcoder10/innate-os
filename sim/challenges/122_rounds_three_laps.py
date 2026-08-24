"""Rounds rung 2: three laps -- 20.22m, nine stops.

The middle of the endurance ladder, and the rung 125 is matched against: nine
goals, 20.22m of route, a 450s limit. Everything about it exists so that the
repetition variable can be isolated by a control that keeps the goal count and
the distance and throws the repetition away.

Hypothesis: completion falls off with lap count on a curve, not a cliff, and
the elapsed time per lap tells you which -- a linear time-per-lap means the
agent is executing a loop, a growing one means it is re-planning from scratch
each round.
Mechanism: the agent's task decomposition and its willingness to re-issue a
plan it has already carried out. The world is stationary throughout.
Control: 120_rounds_tutorial for the ladder; 125_rounds_open_circuit for the
repetition variable specifically (same nine goals, same clock, 20.79m of route,
no stop visited twice).
Degenerate: 0.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle

_ROUNDS = 3
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
    id="rounds_three_laps",
    title="Rounds: Three Laps",
    brief=(
        "Do three rounds of the flat for me: living room, kitchen counter, "
        "corridor, pausing a moment at each, three times through. Mind the "
        "football parked off to one side in the corridor."
    ),
    setup=[Drop("soccer_ball", -5.01, 2.36)],
    goals=_goals,
    time_limit_s=450,
)
