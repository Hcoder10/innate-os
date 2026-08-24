"""Laundry, One Sock: the clean-frame baseline for the verification probes.

One sock on the living room floor, 0.67m in front of spawn, carried 2.9m to the
kitchen floor. Nothing else is in the room, so when the grasp check backs the
robot up and photographs the floor there is nothing in that frame that could be
mistaken for the sock it is holding. Every 11x/12x/13x file that starts with
this scene uses this brief verbatim, so scene and measurement are the only
things that ever differ between them.

Hypothesis: none -- this is the reference pass rate and reference time for the
laundry errand.
Mechanism: none targeted.
Control for: 117, 118, 119 (verification), 122, 123 (phantom object), and the
top rung of the deadline ladder 130-132, which is this scene at 120/240/480s.
Degenerate: 0. Nothing passes without moving a sock 2.9m into a 2.25x0.45m
band of interior, nav-free floor.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="laundry_single_control",
    title="Laundry: One Sock",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. If you can't manage it, email "
        "ops@innate.bot and tell me why."
    ),
    # living_center: probed interior floor, 0.60m clear. Close to spawn on
    # purpose -- the run should be dominated by the grasp and its verification,
    # not by navigation.
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        Goal("Leave a sock in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=900,
)
