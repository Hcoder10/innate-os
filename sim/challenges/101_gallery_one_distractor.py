"""Gallery rung 1: two exhibits, equidistant, one named.

The red cube sits 3.92m from spawn and the blue can 3.93m, on opposite sides
of the start heading (38 deg left, 23 deg right). Equal range is the point: it
takes the "drive to whatever is closest" shortcut off the table, so the only
way through is to tell red from blue before committing to a 4m drive.

Hypothesis: the single ask_image call that opens every pick_any_object run is
also how this agent decides WHICH object it is looking at, so identification
accuracy is measurable without any manipulation.
Mechanism: colour/shape discrimination in one Gemini detection call. The
distractor's mark is checked too, so a wheel through the gallery voids the run
(_BlobTracker never sees a prop the base has already shoved 0.5m).
Control: 100_gallery_tutorial -- same can, same spot, same limit, no distractor.
Degenerate: do-nothing 0; random-of-two 50%; nearest-first 50% (the two are
0.01m apart in range); a fixed turn-left bias 50%, since they are on opposite
sides at equal range.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="gallery_one_distractor",
    title="Gallery: Two Up",
    brief=(
        "Two things are lying on the floor at the far end of the living room. "
        "Go and stand by the blue can for a few seconds -- and mind the other "
        "one, leave it where it is."
    ),
    setup=[
        Drop("can", -5.86, -3.79),
        Drop("cube", -1.91, -3.24),
    ],
    goals=[
        Goal("Find the blue can", Near("robot", "can", 1.2)),
        # The tidiness bar rides on the SAME goal as the dwell: a knocked-away
        # cube then makes the goal unlatchable and the run dies on the clock,
        # rather than a rule declaring a loss.
        Goal(
            "Stand with it for 3 seconds, cube still on its mark",
            AllOf([Hold(Near("robot", "can", 0.9), seconds=3.0), InCircle("cube", -1.91, -3.24, 0.4)]),
        ),
    ],
    time_limit_s=360,
)
