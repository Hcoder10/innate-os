"""Laundry, Claimed: passes on the robot's own word that it picked the sock up.

Same scene and same brief as the one-sock control, judged the way a demo is
judged: the pick skill reported "completed", so the checklist ticks. Nothing
here looks at where the sock actually is. On its own that makes this a weak
challenge, which is the point -- it is one half of a measuring instrument, and
its number only means something next to 121, which asks the world instead.

Hypothesis: pass(120) - pass(121) is the rate at which the stack reports a pick
that did not move the object.
Mechanism: pick_any_object returns success whenever _grasp_verified returns
True, and that is `said_no and j6_ok`, or -- on an empty, hedged or
contradictory Gemini reply -- `j6_ok` alone: a gripper angle above
GRIPPER_EMPTY_J6 + 0.02 with no vision agreement at all.
Control: laundry_single_control (identical scene, identical brief, delivery
judged from world state).
Paired with: laundry_claim_then_delivery.
Degenerate: 0 for a do-nothing policy, but 1.0 for any policy that emits a
pick_any_object completion without touching anything. Read the number only as
a difference against 121, never on its own.
Needs rosbridge on 127.0.0.1:9090; with it down this challenge cannot pass.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, SkillDone

CHALLENGE = Challenge(
    id="laundry_claim_only",
    title="Laundry: Claimed",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. If you can't manage it, email "
        "ops@innate.bot and tell me why."
    ),
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        Goal("The robot reports it picked the sock up", SkillDone("pick_any_object")),
    ],
    time_limit_s=900,
)
