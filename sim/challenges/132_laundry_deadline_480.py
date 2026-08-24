"""Laundry in 8 Minutes: rung 3 of the deadline ladder (120 / 240 / 480s).

The generous half of the matched pair. Eight minutes should be several attempts
worth of budget, so this rung's pass rate is the errand's ceiling and 130's is
its floor; everything between them is time management rather than capability.
A gap between this rung and the 900s control means the deadline sentence itself
is changing behaviour, which is worth knowing separately.

Hypothesis: pass(132) is indistinguishable from pass(laundry_single_control),
and any shortfall is attributable to the brief's deadline, not to the clock.
Mechanism: as 130.
Control: laundry_single_control (900s, no deadline in the brief).
Ladder: 120s / 240s / 480s (here).
Degenerate: 0 at every rung.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="laundry_deadline_480",
    title="Laundry in 8 Minutes",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. You have eight minutes."
    ),
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        Goal("Leave a sock in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=480,
)
