"""Hallway Sock, Moved 7 degrees: the target shifts while the robot is on its way.

Rung 1 of the bearing ladder (0 / 7 / 54 / 114 degrees). Mid-approach the sock
jumps 1.26m to a spot 7 degrees left of the heading the robot arrived on, i.e.
inside the very first frame pick_any_object looks at when it re-searches from
the last known position. This rung should cost almost nothing; it exists to
separate "the sock moved" from "the sock moved somewhere the search cannot
reach", which is what 113 asks.

Hypothesis: a re-detect that lands inside the head camera's first view is free,
so failures here indicate the agent never noticed the object had moved at all
rather than a search-coverage limit.
Mechanism: pick_any_object._position_above -- its 6-step servo loop calls
_localize_retry each step and raises SkillFailed once the object leaves the
frame, handing recovery to the agent. Re-search then starts from wherever the
base ended up.
Control: hallway_sock_control (same scene, same brief, no perturbation).
Degenerate: 0, as in the control.
Harness: at elapsed 45s send `{"op": "drop_prop_at", "name": "sock", "x": -4.51,
"y": 4.36, "yaw": 0.0}` on the world-server observer socket (127.0.0.1:8799).
`yaw` there is RADIANS -- Drop's yaw_deg is not. Never send `{"op": "reset"}`:
a clock rewind fails the run with "the sim was reset". A more robust trigger is
the first `{"status": "started", "skill_id": "pick_any_object"}` on
/brain/skill_status_update plus 8s. Log the robot pose when it fires and
discard runs where the base was already within 1.0m of the sock.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, Near

CHALLENGE = Challenge(
    id="hallway_sock_moved_07deg",
    title="Hallway Sock: Moved (7 deg)",
    brief=(
        "There's a sock on the hallway floor north of you. Bring it to the kitchen and "
        "leave it on the floor by the counter. If it isn't there any more, email "
        "ops@innate.bot and tell me."
    ),
    # Start and destination are the control's, unchanged. The perturbation
    # target (-4.51, 4.36) is hall_north: probed interior floor, 0.70m clear,
    # 1.26m from here on a bearing of 6.8 degrees left of due north.
    setup=[Drop("sock", -4.36, 3.11)],
    goals=[
        # Judged against wherever the sock IS. If the robot got within 0.8m
        # before 45s this latches on the old spot -- that is why the harness
        # logs the pose and throws those runs away.
        Goal("Get to the sock", Near("robot", "sock", 0.8)),
        Goal("Leave it in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=900,
)
