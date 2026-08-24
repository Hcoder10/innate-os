"""The same hall-end can, scored only if move_straight did the driving.

Hypothesis and mechanism are ground_hall_end_nav's; this is the other arm of
that partition, identical in brief and scene so the only thing separating the
two results is which skill the agent picked. move_straight is the harder answer
here -- the can is behind the robot, so this arm can only latch after a
turn_in_place, which makes a pass evidence of real composition rather than a
one-shot reflex. Control: ground_hall_end_nav (the other arm) and
ground_hall_end_arrival (capability). Degenerate: ~0, same guard.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near, SkillDone

CHALLENGE = Challenge(
    id="ground_hall_end_straight",
    title="Down the Hall (straight arm)",
    brief="There's a can down at the far end of the hallway. Go and stand next to it.",
    setup=[Drop("can", -4.36, 3.11)],
    goals=[
        Goal("Drive to the can using move_straight", SkillDone("move_straight", guard=Near("robot", "can", 1.0))),
    ],
    time_limit_s=300,
)
