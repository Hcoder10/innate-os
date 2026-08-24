"""Hallway Sock: the unperturbed errand every 11x probe is measured against.

One sock 3.3m up the corridor from spawn, carried back down to the kitchen
floor. Nothing moves it, nothing takes it away: this run is the baseline pass
rate and the baseline elapsed time that 111-115 subtract from. The destination
is the largest rectangle in Sala_Cozinha that is interior floor AND nav-free,
so a sock inside it is inside the apartment and the robot could stand where it
put it.

Hypothesis: none -- this is the reference. It fixes the cost of the errand
itself so a drop in 111-115 can be charged to the perturbation.
Mechanism: none targeted.
Control for: 111, 112, 113, 114, 115 (identical scene, identical brief).
Degenerate: 0. A do-nothing policy latches nothing; a drive-to-the-sock policy
latches goal 1 and still fails, because only moving the sock 3.9m passes.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, Near

CHALLENGE = Challenge(
    id="hallway_sock_control",
    title="Hallway Sock",
    brief=(
        "There's a sock on the hallway floor north of you. Bring it to the kitchen and "
        "leave it on the floor by the counter. If it isn't there any more, email "
        "ops@innate.bot and tell me."
    ),
    # hall_mid: probed interior floor, 0.55m of clearance, far enough up the
    # corridor (3.28m) that the robot has to commit to a drive before it can be
    # within reach -- which is the window 111-115 perturb.
    setup=[Drop("sock", -4.36, 3.11)],
    goals=[
        Goal("Get to the sock", Near("robot", "sock", 0.8)),
        Goal("Leave it in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=900,
)
