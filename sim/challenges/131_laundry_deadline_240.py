"""Laundry in 4 Minutes: rung 2 of the deadline ladder (120 / 240 / 480s).

Same errand, same brief shape, four minutes. This is the rung expected to sit
on the knee: enough for a clean pick and carry, not enough for a failed grasp
and a second attempt. If 130 and 132 separate and this one is bimodal across
repeats, the budget is buying retries rather than speed -- which is the number
worth having.

Hypothesis: the marginal value of extra budget is concentrated in one retry, so
the pass rate rises fastest across the rung that first admits a second attempt.
Mechanism: as 130.
Control: laundry_single_control (900s, no deadline in the brief).
Ladder: 120s / 240s (here) / 480s.
Degenerate: 0 at every rung.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="laundry_deadline_240",
    title="Laundry in 4 Minutes",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. You have four minutes."
    ),
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        Goal("Leave a sock in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=240,
)
