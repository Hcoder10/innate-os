"""Household rung 1: two chores -- and the second one is the last thing said.

Go there, come back. The return leg is the cheapest possible trailing clause:
no object, no perception, 4.9m of driving the agent has already done once in
reverse. If a two-clause instruction loses its second clause here, the losses
further up the ladder are not about the difficulty of the errands.

Hypothesis: trailing clauses are dropped for being trailing, not for being
hard. An agent that reports success on arriving at the person has finished the
task it heard rather than the task it was given.
Mechanism: the agent's instruction parsing and its own completion criterion --
nothing in the sim resists a return to spawn.
Control: 130_household_tutorial (clause one alone).
Degenerate: 0. The robot STARTS inside the charger circle, but the charger goal
is last and ordered goals are only judged once their predecessors latch, so the
opening pose is never worth anything -- it has to be re-earned after the visit.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="household_two_chores",
    title="Household: There And Back",
    brief=(
        "Someone's lying down in the far corner up by the bathroom. Go check on "
        "them, then come straight back and park yourself on the charger, where "
        "you're standing now."
    ),
    setup=[Drop("human", -6.16, 4.41, yaw_deg=-90)],
    goals=[
        Goal("Check on them", Hold(Near("robot", "human", 1.5), seconds=3.0)),
        # The charger is spawn. reset_world puts the robot here, which is why
        # this goal is last: judged from tick 0 it would latch for free.
        Goal("Back on the charger", Hold(InCircle("robot", -4.34, -0.17, 0.5), seconds=3.0)),
    ],
    time_limit_s=420,
)
