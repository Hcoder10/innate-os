"""Workshop, shape ladder rung 4: the cube.

Identical to 111 except that the object is a 40mm cube -- squat enough that the
shipped grasp band closes ABOVE it. The sock's sidecar says this outright: the
jaws close with the pad centre ~50mm off a hard floor, and the 40mm cube "is
below that band and is the HARD case on real hardware too".

Hypothesis: the height gap is decisive on its own. Square footprint, rigid,
high contrast, dead ahead -- every other axis of this ladder is in the cube's
favour, so a failure here is the closing height and nothing else.
Mechanism: the fixed close height in _push_to_floor (floor_z is an end-effector
target, the pads ride ~10mm above it, close_lift adds 10mm more), descended
blind with wrist_steps=2.
Control: 111_workshop_sock -- same box footprint, twice the height.
Degenerate: 0.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="workshop_cube",
    title="Workshop: The Cube",
    brief=(
        "There's a small red cube on the floor just in front of you. Pick it up "
        "and put it down by the kitchen counter."
    ),
    setup=[Drop("cube", -4.41, -0.84)],
    goals=[
        Goal("Get to the cube", Near("robot", "cube", 0.5)),
        Goal("Cube down by the kitchen counter", InCircle("cube", -5.96, -1.74, 0.5)),
    ],
    time_limit_s=420,
)
