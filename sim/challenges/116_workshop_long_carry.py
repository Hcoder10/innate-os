"""Workshop, distance rung: the same sock, carried across the room instead.

111 with one number changed. The sock starts on the same mark and goes into a
zone of the same 0.5m radius, but the zone is 3.30m away across the living room
instead of 1.79m, and the clock is the same 420s. At half of MAX_LINEAR the
extra 1.51m is about 8s, so under 2% of the budget went into driving: anything
this rung loses against 111 is lost in the CARRY, not in the commute.

Hypothesis: holding an object across a room is a different problem from lifting
it. A grasp that _grasp_verified signs off on can still be a graze that shakes
loose over three metres of driving, and the agent has no way to notice.
Mechanism: _grasp_verified -- a regex over a free-text Gemini YES/NO plus a j6
gripper-width check, with a documented history of misreporting held objects.
Nothing re-checks after the verification passes.
Control: 111_workshop_sock. Same prop, same start, same zone radius, same
clock; carry distance 1.79m -> 3.30m is the only change.
Degenerate: 0 -- the sock must end at least 2.80m from where it started.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="workshop_long_carry",
    title="Workshop: The Long Carry",
    brief=(
        "There's a grey sock on the floor just in front of you. Pick it up and "
        "take it right across the living room -- put it down by the white "
        "cabinet on the far wall."
    ),
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        Goal("Get to the sock", Near("robot", "sock", 0.5)),
        Goal("Sock down by the white cabinet", InCircle("sock", -1.11, -0.89, 0.5)),
    ],
    time_limit_s=420,
)
