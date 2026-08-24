"""Hallway Sock, Snatched: the sock leaves the gripper and nobody tells the robot.

Once the pick reports success the sock is put back on the corridor floor behind
the robot, so the rest of the carry is performed with an empty claw. The world
state and the robot's belief have diverged and nothing in the stack will say
so: the sock is 3.3m from where the agent thinks it is, and the delivery goal
can only latch if the agent goes and looks.

Hypothesis: the stack has no mid-carry hold check, so recovery depends entirely
on whether the agent re-verifies at the destination instead of assuming the
release worked.
Mechanism: pick_any_object latches self._holding when the fingers commit and
only _grasp_verified can clear it; nothing re-checks afterwards, and the carry
pose CARRY_ARM keeps the wrist camera off the object.
Control: hallway_sock_control (same scene, same brief, same goals, no snatch).
Degenerate: 0, as in the control.
Harness: on the first `{"status": "completed", "skill_id": "pick_any_object"}`
seen on /brain/skill_status_update (rosbridge, 127.0.0.1:9090), wait 4s, then
send `{"op": "drop_prop_at", "name": "sock", "x": -6.01, "y": 2.36, "yaw":
0.0}` on the observer socket (127.0.0.1:8799); `yaw` there is RADIANS. That
point is hall_center -- probed interior floor, 0.75m clear, and deliberately
OFF the corridor the robot carries along, so the sock cannot rematerialise
inside the robot's own footprint. With no event feed, fall back to a fixed
elapsed time taken from the control runs' median pick-completion time. Never
send `{"op": "reset"}`.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, Near

CHALLENGE = Challenge(
    id="hallway_sock_snatched",
    title="Hallway Sock: Snatched",
    brief=(
        "There's a sock on the hallway floor north of you. Bring it to the kitchen and "
        "leave it on the floor by the counter. If it isn't there any more, email "
        "ops@innate.bot and tell me."
    ),
    setup=[Drop("sock", -4.36, 3.11)],
    goals=[
        Goal("Get to the sock", Near("robot", "sock", 0.8)),
        Goal("Leave it in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=900,
)
