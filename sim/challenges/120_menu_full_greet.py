"""Menu-size pair, large arm: the same greeting, scored on the wave itself.

See menu_small_greet for the hypothesis, the mechanism and the harness
requirement -- brief and scene are identical, and only the goal differs. This
arm is expected to PASS under demo_agent and to FAIL under basic_agent, which
has no Wave in its menu; that expected failure is the measurement, and a pass
under basic_agent means either the harness loaded the wrong directive or the
agent completed a skill its directive does not offer. Control: menu_small_greet
proves the approach half is not what failed. Degenerate: do-nothing scores 0 on
both menus, since the guard only counts a wave delivered within 1.8m of the
person's centre.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near, SkillDone

CHALLENGE = Challenge(
    id="menu_full_greet",
    title="Say Hello (full menu)",
    brief="There's someone lying down at the far end of the hallway. Go over and say hello to them.",
    setup=[Drop("human", -6.16, 4.41, yaw_deg=-90)],
    goals=[
        # Slacker than the twin's dwell radius: the wave fires wherever the
        # approach happened to stop, and a guard tighter than the approach it
        # follows is a goal that cannot pass.
        Goal("Wave at them", SkillDone("wave", guard=Near("robot", "human", 1.8))),
    ],
    time_limit_s=300,
)
