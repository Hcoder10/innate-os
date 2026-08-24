"""Household route twin: the same five chores, spoken in the worst order.

134 with the sentences shuffled and nothing else touched -- same drops, same
goal list, same 1260s. Executed literally, this wording walks 30.3m against
134's 22.3m, because it sends the robot to the far bathroom corner first and
then back south for the can. Executed by anything that plans, the two runs
should cost the same, and the difference between their elapsed times is the
measurement.

Hypothesis: the agent executes the sentence order it was given rather than
ordering the errands itself, so a hostile phrasing costs it real time on
physically identical work.
Mechanism: instruction following versus task planning. The engine cannot see
the difference -- goals here are memoryless and latch in any order the robot
achieves them -- so the ONLY thing that separates this file from 134 is the
route the agent chooses, priced in seconds.
Control: 134_household_five_chores. Byte-identical apart from id, title and
brief.
Degenerate: 0. Note that a do-nothing policy scores the same in both halves, so
the pair is read as a time difference between two PASSES; a run that fails both
says nothing about ordering.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="household_bad_order",
    title="Household: The Whole List, Backwards",
    brief=(
        "A few bits and pieces before you shut down. Someone's lying down in "
        "the far corner up by the bathroom, so look in on them first. Then "
        "there's a blue can in the middle of the living room -- stick it in the "
        "recycling by the kitchen counter. The dog's football is up in the "
        "corridor, push it through to him in the bedroom. There's a grey sock "
        "in the corridor too, that goes in the laundry pile in the bedroom. "
        "Then come back and park on the charger."
    ),
    setup=[
        Drop("human", -6.16, 4.41, yaw_deg=-90),
        Drop("soccer_ball", -4.36, 3.11),
        Drop("labrador", -0.49, 2.89, yaw_deg=90),
        Drop("sock", -4.36, 1.21),
        Drop("can", -4.41, -0.84),
    ],
    goals=[
        Goal("Check on them", Hold(Near("robot", "human", 1.5), seconds=3.0)),
        Goal("Football back to the dog", Near("soccer_ball", "labrador", 1.2)),
        Goal("Sock in the laundry pile", InCircle("sock", -3.46, 2.86, 0.6)),
        Goal("Can in the recycling", InCircle("can", -5.96, -1.74, 0.6)),
        Goal("Back on the charger", Hold(InCircle("robot", -4.34, -0.17, 0.5), seconds=3.0)),
    ],
    time_limit_s=1260,
)
