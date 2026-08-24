"""Laundry, Two Socks Apart: the same twin, 3.47m away instead of 0.20m.

The far rung of the twin-separation ladder. The head camera at tilt -20 sees
floor from about 0.32m out to the horizon, so this sock is not necessarily out
of the verification frame -- at 3.47m it is roughly 8 pixels across, against 130
at 0.20m. The axis is therefore how much of the frame the decoy occupies, and
the pair 118/119 says whether the check fails on "a sock is present somewhere"
or on "a sock is prominent".

Hypothesis: the false-miss rate falls with the twin's apparent size, so 119
scores between 118 and the one-sock control rather than at either end.
Mechanism: pick_any_object._grasp_verified, as in 118.
Control: laundry_single_control (one sock); paired rung: laundry_twin_near.
Degenerate: 0, as in the control.
"""

from mars_sim_driver.challenges import AnyOf, Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="laundry_twin_far",
    title="Laundry: Two Socks Apart",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. If you can't manage it, email "
        "ops@innate.bot and tell me why."
    ),
    # living_south (-1.91, -3.24): probed interior floor, 3.47m from the first
    # sock and, unlike the other far candidates, OUTSIDE the destination
    # rectangle -- a twin dropped inside it would pass the goal at t=0.
    setup=[Drop("sock", -4.41, -0.84), Drop("second_sock", -1.91, -3.24)],
    goals=[
        Goal(
            "Leave a sock in the kitchen",
            AnyOf(
                [
                    InRect("sock", -5.74, -4.21, -3.49, -3.76),
                    InRect("second_sock", -5.74, -4.21, -3.49, -3.76),
                ]
            ),
        ),
    ],
    time_limit_s=900,
)
