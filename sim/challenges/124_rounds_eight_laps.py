"""Rounds rung 4: eight laps -- 56.22m, twenty-four stops, the ceiling.

The top of the endurance ladder. 56m of driving is about 281s at half of
MAX_LINEAR plus 48s of dwell, against a 1050s limit, so the clock is a backstop
and not the obstacle -- what is being measured is whether anything keeps
issuing the route for twenty-four consecutive stops.

Hypothesis: at eight laps the limiting factor stops being the robot and
becomes the agent's bookkeeping -- specifically, whether it can tell lap 6 from
lap 7 when the two are physically identical.
Mechanism: long-horizon progress tracking. Every lap presents the same three
camera views, so there is no perceptual cue to which lap this is; the count has
to be carried by the agent.
Control: 120_rounds_tutorial. Also read against 125_rounds_open_circuit, whose
nine stops are all different: if long-but-varied passes and short-but-repeated
does not, the problem is repetition rather than duration.
Degenerate: 0.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle

_ROUNDS = 8
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
    id="rounds_eight_laps",
    title="Rounds: Eight Laps",
    brief=(
        "I need eight full rounds of the flat: living room, kitchen counter, "
        "corridor, pausing a moment at each, eight times through. Keep count "
        "and don't finish early. The football in the corridor stays where it is."
    ),
    setup=[Drop("soccer_ball", -5.01, 2.36)],
    goals=_goals,
    time_limit_s=1050,
)
