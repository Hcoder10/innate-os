"""Rounds rung 1: two laps -- the first time the circuit repeats.

The cheapest possible test of "did it stop after the first one". Everything is
identical to 120 except that the three stops come round twice, and the budget
grows by the same 120s per lap the rest of the ladder uses.

Hypothesis: the expensive step in a repeated task is the SECOND iteration, not
the eighth -- an agent that treats "do N rounds" as "do a round" fails here and
fails identically at 8.
Mechanism: the agent's own task decomposition. Nothing in the sim changes
between lap 1 and lap 2, so a drop here is the plan, not the world.
Control: 120_rounds_tutorial -- same circuit, one lap.
Degenerate: 0. Six ordered 0.5m discs with a 2s dwell each is not something a
random walk reaches inside 330s.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle

_ROUNDS = 2
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
    id="rounds_two_laps",
    title="Rounds: Two Laps",
    brief=(
        "Do two rounds of the flat for me: living room, kitchen counter, "
        "corridor, pausing a moment at each -- then the same again. There's a "
        "football parked off to one side in the corridor; leave it alone."
    ),
    setup=[Drop("soccer_ball", -5.01, 2.36)],
    goals=_goals,
    time_limit_s=330,
)
