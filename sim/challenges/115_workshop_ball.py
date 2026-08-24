"""Workshop, shape ladder rung 5: the stress ball -- the top of the ladder.

Identical to 111 except that the object is a 45mm sphere, which its own sidecar
calls "the least forgiving shape in the roster": on a hard sphere the descending
blade grazes the flank on ordinary aim error, stalls the descent high, and the
jaws close over the top cap without ever moving the ball. The shipped prop is
softened to ~3mm of dent precisely so that this is possible at all.

Hypothesis: the ladder's ceiling. Where the cube fails on height, the ball
fails on curvature -- aim error that a flat face absorbs, a sphere converts
into a slide-off.
Mechanism: the blind descent plus the sphere contact rolling around the curve;
priority=4 in the sidecar exists to stop the arm riding up over it.
Control: 111_workshop_sock. Compare against 114_workshop_cube too: cube and
ball are the same 40-45mm width, so cube-passes-ball-fails isolates curvature
from size.
Degenerate: 0. Note the one physical caveat on this rung: a sphere that the
base clips will ROLL, so a lucky nudge could in principle land it in the zone.
It has to roll 1.29m in the right direction on condim-6 rolling friction; treat
a pass with no grasp in the log as exactly that.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="workshop_ball",
    title="Workshop: The Stress Ball",
    brief=(
        "There's a green stress ball on the floor just in front of you. Pick it "
        "up and put it down by the kitchen counter."
    ),
    setup=[Drop("ball", -4.41, -0.84)],
    goals=[
        Goal("Get to the ball", Near("robot", "ball", 0.5)),
        Goal("Ball down by the kitchen counter", InCircle("ball", -5.96, -1.74, 0.5)),
    ],
    time_limit_s=420,
)
