"""Workshop, shape ladder rung 1: the sock.

Rung 1 of five identical tasks that differ only in which object is on the
floor. Same pick spot, same 1.79m carry, same 0.5m zone, same clock -- so the
rungs are directly comparable and the ladder reads as one curve rather than
five anecdotes. The sock is the easy end: 60mm tall, which is where the
shipped grasp constants close the jaws.

Hypothesis: success on this ladder is governed by the object's geometry
against a fixed grasp band, not by the navigation around it.
Mechanism: _push_to_floor's blind descent plus the fixed close height. The
sock's sidecar records the band explicitly ("the skill's closing band lands on
its upper third").
Control: this rung is the control for 112-115; 110_workshop_tutorial is the
same object with the zone widened to 0.9m.
Degenerate: 0 -- the object must end up 1.29m or more from where it started.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

# Ladder axis (111 -> 115): object geometry against a fixed grasp band --
# sock (60mm, the design target), can, bar, cube (40mm, below the band), ball
# (sphere). Geometry, zone and time limit are identical across all five.
CHALLENGE = Challenge(
    id="workshop_sock",
    title="Workshop: The Sock",
    brief=(
        "There's a grey sock on the floor just in front of you. Pick it up and "
        "put it down by the kitchen counter."
    ),
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        # 0.5m: spawn already sits 0.67m from the mark, and the props' `reach`
        # offsets put a held object 0.22-0.32m from the base origin.
        Goal("Get to the sock", Near("robot", "sock", 0.5)),
        Goal("Sock down by the kitchen counter", InCircle("sock", -5.96, -1.74, 0.5)),
    ],
    time_limit_s=420,
)
