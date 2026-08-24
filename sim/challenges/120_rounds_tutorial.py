"""Rounds rung 0: one lap of the patrol circuit.

Rounds is an endurance game: the same three stops, over and over, with a
football parked off to the side that has to still be there at the end. One lap
is 5.8m of driving and three two-second pauses -- roughly 35s of work against a
210s limit -- so this rung asks only whether the agent can follow a
three-stop route at all.

Hypothesis: none. It is the zero point that makes 121-124 readable: a lap the
agent cannot do once tells you nothing about whether it would do it eight
times.
Mechanism: nothing isolated. It pins the circuit, the 2s dwell, the football's
spot and the 90 + 120*laps time budget for the whole ladder.
Control: this file is the one-lap control for 121, 122, 123 and 124.
Degenerate: do-nothing 0 -- spawn is 0.67m outside the first waypoint's circle,
so not even the opening goal is free. A random walk must hit three 0.5m discs
in a fixed order and hold each for 2s.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle

# Ladder axis (120 -> 124): laps of one fixed circuit, 1 -> 2 -> 3 -> 5 -> 8.
# Nothing else moves: same stops, same dwell, same football, and a time limit
# of 90 + 120*laps, which is ~3x a competent lap at half of MAX_LINEAR (0.4).
_ROUNDS = 1
_CIRCUIT = (
    ("the living room", -4.41, -0.84),
    ("the kitchen counter", -5.96, -1.74),
    ("the corridor", -4.36, 1.21),
)


def _stop(x, y):
    """A held stop rather than a drive-through, at a radius (0.5m) every one of
    these points has clearance for. Each goal builds its own Hold: the dwell is
    state, and a shared instance would carry one lap's timer into the next."""
    return Hold(InCircle("robot", x, y, 0.5), seconds=2.0)


_goals = [
    Goal(f"Round {n}: {label}", _stop(x, y)) for n in range(1, _ROUNDS + 1) for label, x, y in _CIRCUIT
]
# The football is judged once, at the END: a tidiness bar over the whole
# patrol rather than a gate on every lap. It sits 1.32m off the route, so a
# clean line never touches it and a sloppy one sends it rolling.
_goals[-1] = Goal(
    f"Round {_ROUNDS}: the corridor, football untouched",
    AllOf([_stop(*_CIRCUIT[-1][1:]), InCircle("soccer_ball", -5.01, 2.36, 0.5)]),
)

CHALLENGE = Challenge(
    id="rounds_tutorial",
    title="Rounds: One Lap",
    brief=(
        "Do one round of the flat for me: stop in the middle of the living "
        "room, then at the kitchen counter, then up in the corridor, and pause "
        "a moment at each. There's a football parked off to one side in the "
        "corridor -- don't kick it."
    ),
    setup=[Drop("soccer_ball", -5.01, 2.36)],
    goals=_goals,
    time_limit_s=210,
)
