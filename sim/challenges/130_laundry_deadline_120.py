"""Laundry in 2 Minutes: rung 1 of the deadline ladder (120 / 240 / 480s).

The one-sock errand from 116, with a budget the robot is told about. The number
appears in the brief and in time_limit_s on purpose: the probe is whether an
agent given a budget spends it differently, not whether it can be secretly
stopwatched. 120s is meant to be under the floor -- one detection, one approach
servo and one grasp cost most of it before any carrying starts.

Hypothesis: below some budget the pass rate goes to zero as a step, not a slope,
because the pick is a fixed-cost block (FOLLOW_TIMEOUT_S 20 plus
WRIST_ALIGN_TIMEOUT_S 60 in the worst case) that cannot be hurried; the ladder
locates that step.
Mechanism: pick_any_object's fixed timeouts and its 6-step servo loop, plus
whatever the agent's own planning overhead adds before the first motion.
Control: laundry_single_control -- same scene, same room, 900s and no deadline
sentence in the brief. Its elapsed time is the ladder's zero point.
Ladder: 120s (here) / 240s / 480s, and 900s unhurried in the control.
Degenerate: 0 at every rung.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="laundry_deadline_120",
    title="Laundry in 2 Minutes",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. You have two minutes."
    ),
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        Goal("Leave a sock in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=120,
)
