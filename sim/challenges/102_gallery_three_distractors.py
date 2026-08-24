"""Gallery rung 2: four exhibits in four rooms, one named.

The four sit 3.75-3.93m from spawn -- a 0.18m spread over a 3.9m radius -- so
range carries no information and the robot has to spend the clock looking.
Bearings fan out across 23 deg right to 148 deg left, which puts two of them
outside the opening search arc: finding the gallery is now part of the game,
and the score is how much of the flat had to be visited to answer.

Hypothesis: identification cost grows with the number of look-alike candidates,
and it is paid in TIME rather than in accuracy -- an exhaustive visitor still
passes, just slowly. Elapsed time is the measurement, not the tick.
Mechanism: one Gemini detection per look, plus the ~90 deg _search arc, which
forces a body rotation before the two rear exhibits can be seen at all.
Control: 100_gallery_tutorial (same target, same spot, zero distractors);
101_gallery_one_distractor is the intermediate rung.
Degenerate: do-nothing 0; random-of-four 25%; nearest-first 25%, because the
four are inside 0.18m of each other in range and the ordering is noise.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="gallery_three_distractors",
    title="Gallery: Four Up",
    brief=(
        "There are four odds and ends scattered around the flat. Find the blue "
        "can, stand with it for a few seconds, and don't disturb the other three."
    ),
    setup=[
        Drop("can", -5.86, -3.79),
        Drop("cube", -1.91, -3.24),
        Drop("ball", -0.76, 1.31),
        Drop("bar", -2.36, 3.01),
    ],
    goals=[
        Goal("Find the blue can", Near("robot", "can", 1.2)),
        Goal(
            "Stand with it for 3 seconds, gallery undisturbed",
            AllOf(
                [
                    Hold(Near("robot", "can", 0.9), seconds=3.0),
                    InCircle("cube", -1.91, -3.24, 0.4),
                    InCircle("ball", -0.76, 1.31, 0.4),
                    InCircle("bar", -2.36, 3.01, 0.4),
                ]
            ),
        ),
    ],
    time_limit_s=360,
)
