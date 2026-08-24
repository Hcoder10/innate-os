"""Parameter extraction, rung 1 of 2: back up 1.5m and stop there.

Hypothesis: the number in the sentence reaches the skill call. Mechanism:
argument extraction into move_straight(distance) or navigate_to_position's
local goal -- both take a signed metre value, and the corridor behind spawn is
the one long clear run in the apartment, so the manoeuvre is a straight reverse
with no planning in it. The rung pair is the measurement: this file's disc and
ground_back_up_long's are 1.5m apart and 0.45m wide, so they cannot both latch
in one run and an agent that hears "back up" but drops the number passes at
most one of the two. Control: ground_back_up_long (same brief shape, different
number) and ground_back_up_fast (same number, junk speed). Degenerate: a
do-nothing policy scores 0 -- spawn is 1.5m from this disc, 1.05m outside it.
"""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="ground_back_up_short",
    title="Back Up 1.5m",
    brief="Back up a metre and a half and wait there.",
    setup=[],
    goals=[
        # 1.5m back along +y from spawn (-4.34, -0.17, facing -y). Probed
        # clear_r 0.50, the widest part of the corridor, so the robot really
        # can come to rest inside this disc.
        Goal("Stop 1.5m back", Hold(InCircle("robot", -4.34, 1.33, 0.45), seconds=3.0)),
    ],
    time_limit_s=300,
)
