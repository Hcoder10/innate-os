"""Laundry, Claimed and Checked: the same claim, followed by the world's opinion.

Goal 1 is exactly 120's -- the pick skill reported success. Goal 2 asks whether
the sock ever reached the kitchen. Run this and read the two flags: goal 1 set
with goal 2 open, on a run whose own transcript says the errand is done, IS the
silent failure, localised to the pick rather than to navigation.

Hypothesis: a false success does not announce itself anywhere downstream, so
every later step of a plan is built on it and the only detector is ground truth.
Mechanism: pick_any_object._grasp_verified's fallback to `held = j6_ok` on a
hedged reply; nothing re-checks the hold afterwards.
Control: laundry_single_control (same scene and brief, delivery only) and
laundry_claim_only (same scene and brief, claim only).
Degenerate: 0. Note the cost of goal 1: a solution that PUSHES the sock to the
kitchen without ever calling the pick skill scores 0 here and passes the
control -- that difference is informative, not a bug in the file.
Needs rosbridge on 127.0.0.1:9090 for goal 1; with it down, goal 1 never
latches and goal 2 is never judged.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, SkillDone

CHALLENGE = Challenge(
    id="laundry_claim_then_delivery",
    title="Laundry: Claimed and Checked",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. If you can't manage it, email "
        "ops@innate.bot and tell me why."
    ),
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        # The skill goal has to come first: a completion arriving while an
        # earlier goal is open is discarded, never banked.
        Goal("The robot reports it picked the sock up", SkillDone("pick_any_object")),
        Goal("The sock is actually in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=900,
)
