"""Laundry with a Cube: clutter in the verification frame, but nothing sock-like.

Identical to the one-sock control except that a red cube sits 0.20m from the
sock -- in the pick approach, in the grasp, and in the frame _grasp_verified
photographs afterwards. The cube is a different colour, a different shape and a
different size, so a correct answer to "is the sock lying loose on the floor?"
is still NO. This rung exists so that any drop measured in 118 and 119 cannot
be explained by "clutter degrades the check"; it has to be explained by the
clutter looking like the sock.

Hypothesis: an unrelated object in the verification frame costs nothing.
Mechanism: pick_any_object._grasp_verified -- one gemlib.ask_image call over
the head and wrist frames whose answer is regex-scanned for a whole-word
YES/NO, ANDed with j6 > GRIPPER_EMPTY_J6 + 0.02.
Control: laundry_single_control (same brief, same sock, no second object).
Matched twin: laundry_twin_near -- same brief, same geometry, second object at
the same 0.20m, differing only in whether it is identical to the sock.
Degenerate: 0, as in the control.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="laundry_cube_decoy",
    title="Laundry: Cube Nearby",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. If you can't manage it, email "
        "ops@innate.bot and tell me why."
    ),
    # (-4.41, -1.04) is 0.20m beyond the sock, inside the largest all-interior
    # nav-free rectangle in Sala_Cozinha, and on the far side from spawn so the
    # robot is not standing on it at t=0. 118 puts the identical twin on this
    # exact spot, so the two files differ only in WHICH prop stands there. A
    # cube rather than a ball: a ball nudged on the approach rolls out of the
    # frame under test.
    setup=[Drop("sock", -4.41, -0.84), Drop("cube", -4.41, -1.04)],
    goals=[
        Goal("Leave a sock in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=900,
)
