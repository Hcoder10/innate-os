"""Hallway Sock, Moved 54 degrees: the target lands at the edge of the scan.

Rung 2 of the bearing ladder (0 / 7 / 54 / 114 degrees). _search sweeps three
headings -- h0, h0-30, h0+30 (the shipped turns 0, -30, +60 are CUMULATIVE) --
and the head camera's HFOV_DEG is 70, so the union of what it can see is about
h0 +/- 65 degrees. 54 degrees is inside that union but only via the +30 turn
plus most of the frame's half-width, so this rung sits right against the edge
the next one crosses.

Hypothesis: recovery survives to the edge of the scan union and collapses just
past it; this rung and 113 bracket the cliff.
Mechanism: pick_any_object._search -- three localize attempts at h0, h0-30,
h0+30, then SkillFailed("Could not find ... even after scanning"). Only an
agent that turns the BASE itself widens that.
Control: hallway_sock_control (same scene, same brief, no perturbation).
Degenerate: 0, as in the control.
Harness: at elapsed 45s send `{"op": "drop_prop_at", "name": "sock", "x": -6.16,
"y": 4.41, "yaw": 0.0}` on the world-server observer socket (127.0.0.1:8799).
`yaw` there is RADIANS. Never send `{"op": "reset"}`: a clock rewind fails the
run. Preferred trigger and the pose-log discard rule are as in 111.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, Near

CHALLENGE = Challenge(
    id="hallway_sock_moved_54deg",
    title="Hallway Sock: Moved (54 deg)",
    brief=(
        "There's a sock on the hallway floor north of you. Bring it to the kitchen and "
        "leave it on the floor by the counter. If it isn't there any more, email "
        "ops@innate.bot and tell me."
    ),
    # Perturbation target (-6.16, 4.41) is bath_northwest: probed interior
    # floor, 0.60m clear, 2.22m from here at 54.2 degrees left of due north.
    # Every rung is placed on the LEFT because that is where the scan is
    # widest; the mirror-image ladder to the right is the obvious extension.
    setup=[Drop("sock", -4.36, 3.11)],
    goals=[
        Goal("Get to the sock", Near("robot", "sock", 0.8)),
        Goal("Leave it in the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=900,
)
