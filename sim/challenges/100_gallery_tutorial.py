"""Gallery rung 0: one exhibit, nothing to tell it apart from.

The Gallery is an identification game -- the exhibits differ only in how they
look, and the answer is judged by which one the robot parks itself at. This
rung has nothing to identify, so it is the ladder's zero point and the harness
sanity check rather than a discriminator.

Hypothesis: none. It calibrates 101/102/103 -- an agent that fails here has a
navigation or a dwell problem, not a perception one.
Mechanism: fixes the target's geometry for the whole ladder. The can sits
3.93m out on a bearing 23 deg right of the start heading, inside
pick_any_object._search's ~90 deg arc, so nothing above depends on the search
arc.
Control: this file IS the zero-distractor control for 101, 102 and 103.
Degenerate: do-nothing 0. Drive-to-the-nearest-prop passes 1/1, which is
precisely what the rungs above take away.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

# Ladder axis (100 -> 103): number of distractor exhibits, 0 -> 1 -> 3 -> 5.
# The target and its spot never move, and the time limit never changes.
CHALLENGE = Challenge(
    id="gallery_tutorial",
    title="Gallery: Warm-up",
    brief=(
        "There's a blue can on the floor down at the kitchen end of the living "
        "room. Go and stand next to it, and stay there a few seconds."
    ),
    setup=[Drop("can", -5.86, -3.79)],
    goals=[
        Goal("Find the blue can", Near("robot", "can", 1.2)),
        # Dwell, so a drive-past on the way somewhere else does not count as
        # having looked at it.
        Goal("Stand with it for 3 seconds", Hold(Near("robot", "can", 0.9), seconds=3.0)),
    ],
    time_limit_s=360,
)
