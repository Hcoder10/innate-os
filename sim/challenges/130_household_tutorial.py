"""Household rung 0: one chore -- look in on the person.

Household is the composite game: one spoken instruction carrying several
errands, scored on how many of them survive. This rung carries one, and says
where the person is, so nothing has to be searched for or picked up.

Hypothesis: none. It establishes that the agent can execute a single clause
of a Household instruction, which is what 131-135 add clauses to.
Mechanism: nothing isolated. It fixes the person's placement -- the drop at
(-6.16, 4.41) yaw -90 is the best-behaved human spot probed, 0.053m of settle
drift, and Near() measures its visual CENTRE at (-5.31, 4.42), 0.86m up-body
from the drop point.
Control: this file is the one-chore control for the 131-134 ladder.
Degenerate: 0. The person is 4.9m from spawn and the dwell is 3s.
Relationship to the shipped `rescue`: this is deliberately rescue with the
answer given away. rescue makes finding them the task; here the location is in
the brief, so a failure is about acting on an instruction, not about search.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

# Ladder axis (130 -> 134): clauses in one instruction, 1 -> 2 -> 3 -> 4 -> 5.
# The scene has to grow with the axis (a chore needs its object), so clutter
# and clause count move together here; the Gallery family varies clutter alone
# against a fixed target, and the two are meant to be read side by side.
CHALLENGE = Challenge(
    id="household_tutorial",
    title="Household: One Thing",
    brief=(
        "Someone's lying down in the far corner up by the bathroom. Go and "
        "check on them -- get right up close and stay with them a moment."
    ),
    setup=[Drop("human", -6.16, 4.41, yaw_deg=-90)],
    goals=[
        Goal("Check on them", Hold(Near("robot", "human", 1.5), seconds=3.0)),
    ],
    time_limit_s=300,
)
