"""Overcommit: five things, five minutes, and no chance of finishing.

Designed to be failed. The score is not pass/fail but how far down the
checklist the run got before the clock ran out, so the goals are the ladder and
the axis is items delivered. What is being measured is what an agent does with
a workload it cannot complete: does it deliver two things cleanly, or does it
start five and finish none because it spent the budget going back and forth?

Hypothesis: an agent with no completion accounting spreads effort across the
set and lands at zero completed items, while one that serialises lands at two
or three. Both look identical at t=0 and identical at the time limit.
Mechanism: not a skill defect -- this probes whether the plan is a queue or a
sweep. The single gripper makes it a queue by force; the failure mode is a
route that revisits.
Control: overcommit_generous -- same five objects, same brief, 25 minutes,
which fixes the achievable ceiling and the unpressured route.
Degenerate: 0 items. The nearest object is 0.67m from spawn but 3.0m from the
destination band, so nothing is delivered by wandering.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="overcommit_tight",
    title="Overcommit: Five in Five Minutes",
    brief=(
        "The living room is a mess -- there are five things on the floor and they all "
        "belong on the kitchen floor by the counter. Do as much as you can in the next "
        "five minutes."
    ),
    # The whole manipulation set, on probed interior floor, none of it inside
    # the destination rectangle. Listed nearest-to-spawn first so the checklist
    # reads as progress for the ordinary route.
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
    time_limit_s=300,
)
