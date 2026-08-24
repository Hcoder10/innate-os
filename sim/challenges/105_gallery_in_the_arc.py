"""Gallery, bearing control: the same exhibit, the same range, in front.

The matched twin of 104. One can, 1.41m from spawn on a bearing of 44 deg --
inside the wedge pick_any_object._search actually scans. Same prop, same brief,
same goals, same time limit; the drop coordinate is the only difference between
the two files, and the range differs by 0.03m.

Hypothesis: the agent finds a close object it starts pointed at. If this rung
also fails, 104's failure says nothing about the search arc and the pair should
be discarded rather than reported.
Mechanism: the in-arc half of pick_any_object._search (head yaws 0, -30, +60).
Control: 104_gallery_behind_you is the treatment; this file is its control.
Degenerate: do-nothing 0; random-walk-plus-3s-dwell is small but non-zero over
300s, and it is identical in both halves of the pair, which is why the pair is
what gets reported.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="gallery_in_the_arc",
    title="Gallery: In Plain Sight",
    brief=(
        "There's a blue can on the floor somewhere close by. Find it and stand "
        "with it for a few seconds."
    ),
    setup=[Drop("can", -3.36, -1.19)],
    goals=[
        Goal("Find the blue can", Near("robot", "can", 1.2)),
        Goal("Stand with it for 3 seconds", Hold(Near("robot", "can", 0.9), seconds=3.0)),
    ],
    time_limit_s=300,
)
