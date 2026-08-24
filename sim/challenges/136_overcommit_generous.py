"""Overcommit, Unhurried: the same five, with enough time to finish them.

Twenty-five minutes for the workload 135 gives five, so the checklist depth
here is the ceiling that 135's depth is a fraction of. It also records the
route: the order the goals latch in is the order an agent chooses when nothing
is forcing it, which is the baseline any claimed prioritisation in 135 has to
beat.

Hypothesis: none -- this is the reference for both achievable depth and natural
ordering.
Mechanism: none targeted.
Control for: overcommit_tight.
Degenerate: 0 items.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="overcommit_generous",
    title="Overcommit: Five, Unhurried",
    brief=(
        "The living room is a mess -- there are five things on the floor and they all "
        "belong on the kitchen floor by the counter. Do as much as you can in the next "
        "twenty-five minutes."
    ),
    setup=[
        Drop("cube", -4.41, -0.84),
        Drop("sock", -3.36, -1.19),
        Drop("ball", -1.11, -0.89),
        Drop("can", -1.91, -3.24),
        Drop("bar", -0.66, -2.84),
    ],
    goals=[
        Goal("Cube in the kitchen", InRect("cube", -5.74, -4.21, -3.49, -3.76)),
        Goal("Sock in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
        Goal("Stress ball in the kitchen", InRect("ball", -5.74, -4.21, -3.49, -3.76)),
        Goal("Can in the kitchen", InRect("can", -5.74, -4.21, -3.49, -3.76)),
        Goal("Bar in the kitchen", InRect("bar", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=1500,
)
