"""Gallery rung 3: the whole prop roster out at once, one named.

Six exhibits, every one of them a different colour and shape, spread over all
three rooms. Two of them now sit CLOSER than the answer (the grey sock at
3.03m, the white football at 3.31m), so for the first time on this ladder a
nearest-first policy is not merely uninformed, it is actively wrong.

Hypothesis: with six candidates the failure mode changes character -- an agent
stops mis-ranking and starts mis-naming, settling on whichever object its first
detection call happened to describe confidently.
Mechanism: one ask_image detection per look, over a scene whose colour classes
now collide (red cube against orange bar, grey sock against white football) --
the same HSV separation _BlobTracker depends on downstream.
Control: 100_gallery_tutorial; the ladder 100 -> 101 -> 102 -> 103 walks 0, 1,
3, 5 distractors with the target and its spot pinned.
Degenerate: do-nothing 0; random-of-six 17%; nearest-first 0 -- it walks to the
sock.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle, Near

# Every pair of exhibits is >=2.2m apart, so no two share a camera frame at
# working distance and each detection is answering about one object.
CHALLENGE = Challenge(
    id="gallery_five_distractors",
    title="Gallery: Full House",
    brief=(
        "Six different things are lying around the flat. Find the blue can, "
        "stand with it for a few seconds, and leave everything else exactly "
        "where it is."
    ),
    setup=[
        Drop("can", -5.86, -3.79),
        Drop("cube", -1.91, -3.24),
        Drop("ball", -0.76, 1.31),
        Drop("bar", -2.36, 3.01),
        Drop("sock", -6.01, 2.36),
        Drop("soccer_ball", -1.11, -0.89),
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
                    InCircle("sock", -6.01, 2.36, 0.4),
                    # The football is the one exhibit that runs away when
                    # clipped: condim 6 rolling friction, measured 0.5-1.3m of
                    # roll off a nudge.
                    InCircle("soccer_ball", -1.11, -0.89, 0.4),
                ]
            ),
        ),
    ],
    time_limit_s=360,
)
