"""Household rung 2: three chores -- the middle one moves an object.

The football joins in. It is the one errand on this ladder that changes the
world without needing a grasp: the ball is pushed, exactly as the shipped
`shepherd` challenge does it, so this rung stays inside the reach of an agent
that cannot pick anything up. That matters -- it keeps the clause-count ladder
readable for two more rungs before manipulation becomes a prerequisite.

Hypothesis: a clause that changes the world sits differently in the plan from
one that only moves the robot, and inserting it between two navigation clauses
is where a flat "do these in order" agent starts losing the tail.
Mechanism: the agent's plan, plus real physics on the push -- the ball is a
sphere with condim-6 rolling friction, so an overshoot rolls it past the dog
and the errand has to be redone.
Control: 131_household_two_chores (the same first and last clause, nothing in
between).
Degenerate: 0.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

# The ball starts 1.90m up the corridor from where later rungs put the sock,
# so the two never share a camera frame and each detection is about one object.
CHALLENGE = Challenge(
    id="household_three_chores",
    title="Household: Three Things",
    brief=(
        "The dog's football has ended up in the corridor -- push it back "
        "through to him in the bedroom. Someone's also lying down in the far "
        "corner up by the bathroom, so look in on them. Then park yourself back "
        "on the charger."
    ),
    setup=[
        Drop("human", -6.16, 4.41, yaw_deg=-90),
        Drop("soccer_ball", -4.36, 3.11),
        Drop("labrador", -0.49, 2.89, yaw_deg=90),
    ],
    goals=[
        # The two dwell goals bracket the run deliberately. Hold carries state
        # and is only updated once it is the open goal, so a Hold buried in the
        # middle would silently restart; the object goals between them are
        # memoryless and latch in whatever order the robot actually works.
        Goal("Check on them", Hold(Near("robot", "human", 1.5), seconds=3.0)),
        Goal("Football back to the dog", Near("soccer_ball", "labrador", 1.2)),
        Goal("Back on the charger", Hold(InCircle("robot", -4.34, -0.17, 0.5), seconds=3.0)),
    ],
    time_limit_s=660,
)
