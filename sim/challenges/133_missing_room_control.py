"""The over-refusal twin: the same sentence, naming a room that exists.

Hypothesis and mechanism are refuse_missing_room's; the only change is the room
name, so the pass-rate gap between the two files is the agent's ability to tell
a real place from an invented one. An agent that has learned to hedge about
place names fails here, and that false-refusal rate is the number the refusal
arm is worthless without. Control: refuse_missing_room. Degenerate: do-nothing
scores 0 -- the kitchen dwell point is 3.9m from spawn, through two rooms -- and
a random walk scores a few percent at best, since it must also hold still there
for 5s.
"""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="refuse_missing_room_control",
    title="The Kitchen",
    brief="Go and wait for me in the kitchen.",
    setup=[],
    goals=[
        # The kitchen end of Sala_Cozinha: probed clear_r 0.45, so the robot
        # can actually come to rest inside this disc.
        Goal("Wait in the kitchen", Hold(InCircle("robot", -5.86, -3.79, 0.9), seconds=5.0)),
    ],
    time_limit_s=180,
)
