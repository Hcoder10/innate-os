"""Hall-end can, scored only if navigate_to_position did the driving.

Hypothesis: "go and stand next to it", 3.3m away, is satisfiable by
navigate_to_position OR by turn_in_place + move_straight, and which one the
agent reaches for is a property of its grounding, not of the task -- nothing
here forces either answer and both are in the directive's menu. Mechanism:
skill selection, read off /brain/skill_status_update. This file latches only on
navigate_to_position and its twin ground_hall_end_straight only on
move_straight, so the two partition one run and their pass rates over N runs
are the choice distribution; ground_hall_end_arrival is the capability control
that separates "chose the other skill" from "never got there". Degenerate: a
do-nothing or random-walk policy scores ~0 -- the guard only counts a
completion delivered with the base inside 1.0m of a point 3.3m up the hall.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near, SkillDone

CHALLENGE = Challenge(
    id="ground_hall_end_nav",
    title="Down the Hall (nav arm)",
    brief="There's a can down at the far end of the hallway. Go and stand next to it.",
    # 3.28m straight up the corridor behind spawn; settle-tested drift 0.000 on
    # Banheiros_Corredor floor, and the robot can stand within 0.28m of it.
    setup=[Drop("can", -4.36, 3.11)],
    goals=[
        # Guard radius is loose on purpose: a two-hop approach delivers an
        # early completion that simply fails the guard and leaves the goal
        # open, so only the completion that actually arrives at the can counts.
        Goal("Drive to the can using navigate_to_position", SkillDone("navigate_to_position", guard=Near("robot", "can", 1.0))),
    ],
    time_limit_s=300,
)
