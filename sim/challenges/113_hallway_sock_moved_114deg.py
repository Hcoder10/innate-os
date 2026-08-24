"""Hallway Sock, Moved 114 degrees: the target lands outside the scan entirely.

Rung 3 of the bearing ladder (0 / 7 / 54 / 114 degrees). 114 degrees is past
the h0 +/- 65 union of _search's three headings and the 70-degree HFOV, so no
number of pick_any_object retries can find the sock: the skill will scan, raise
SkillFailed, and scan again from the same pose forever. Passing requires the
AGENT to turn the base or drive somewhere new between attempts -- a re-plan,
not a retry.

Hypothesis: the failure here is architectural, not perceptual. An agent that
only re-invokes the failing skill scores 0 no matter how many attempts it has;
only one that changes the base pose between attempts recovers.
Mechanism: pick_any_object._search's fixed three-heading sweep, and the fact
that a SkillFailed hands control back to the brain with no pose change.
Control: hallway_sock_control (same scene, same brief, no perturbation).
Degenerate: 0, as in the control.
Harness: at elapsed 45s send `{"op": "drop_prop_at", "name": "sock", "x": -6.01,
"y": 2.36, "yaw": 0.0}` on the world-server observer socket (127.0.0.1:8799).
`yaw` there is RADIANS. Never send `{"op": "reset"}`: a clock rewind fails the
run. Preferred trigger and the pose-log discard rule are as in 111.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, Near

CHALLENGE = Challenge(
    id="hallway_sock_moved_114deg",
    title="Hallway Sock: Moved (114 deg)",
    brief=(
        "There's a sock on the hallway floor north of you. Bring it to the kitchen and "
        "leave it on the floor by the counter. If it isn't there any more, email "
        "ops@innate.bot and tell me."
    ),
    # Perturbation target (-6.01, 2.36) is hall_center: probed interior floor,
    # 0.75m clear, 1.81m from here at 114.4 degrees left -- BEHIND the shoulder
    # of a robot that arrived heading north. It is also the shortest hop of the
    # three rungs, so a failure cannot be blamed on distance.
    setup=[Drop("sock", -4.36, 3.11)],
    goals=[
        Goal("Get to the sock", Near("robot", "sock", 0.8)),
        Goal("Leave it in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=900,
)
