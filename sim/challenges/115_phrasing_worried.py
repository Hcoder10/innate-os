"""Paraphrase ladder, rung 3 of 3: no imperative at all, just a worry.

See ground_phrasing_direct for the hypothesis, the mechanism and the axis. The
brief is a yes/no question about a state of the world the robot cannot answer
from where it is standing, so the only honest reply is to go and look -- an
agent that answers conversationally without moving has grounded the words as
chat rather than as a task, which is the failure this rung exists to catch.
Scene, goal and limit are identical to rungs 1 and 2. Control: rungs 1 and 2.
Degenerate: 0 for do-nothing and 0 for drive-to-the-nearest-object; note that
do-nothing is exactly the failure mode under test here, so this rung must be
read against rung 1 rather than on its own.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="ground_phrasing_worried",
    title="Sock in the Bathroom (as a worry)",
    brief="I think I left a sock lying on the bathroom floor. Is it still there?",
    setup=[
        Drop("sock", -6.16, 4.41),
        Drop("can", -4.36, 1.21),
    ],
    goals=[
        Goal("Stand next to the sock", Hold(Near("robot", "sock", 0.9), seconds=3.0)),
    ],
    time_limit_s=300,
)
