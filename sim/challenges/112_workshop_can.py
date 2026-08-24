"""Workshop, shape ladder rung 2: the can.

Identical to 111 except that the object is a 40mm x 60mm cylinder. Tall enough
for the grasp band like the sock, but round: an off-centre pinch rolls it out
of the jaws instead of squashing into them.

Hypothesis: at equal height, a round cross-section costs more than a square
one, because the contact slides rather than deforms.
Mechanism: the fixed jaw close in _push_to_floor against a curved flank; the
can's sidecar notes 50mm "pinches at the fingertip and rolls out", so 40mm is
already the friendly version of this shape.
Control: 111_workshop_sock -- same spot, same carry, same zone, same clock.
Degenerate: 0.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="workshop_can",
    title="Workshop: The Can",
    brief=(
        "There's a blue can on the floor just in front of you. Pick it up and "
        "put it down by the kitchen counter."
    ),
    setup=[Drop("can", -4.41, -0.84)],
    goals=[
        Goal("Get to the can", Near("robot", "can", 0.5)),
        Goal("Can down by the kitchen counter", InCircle("can", -5.96, -1.74, 0.5)),
    ],
    time_limit_s=420,
)
