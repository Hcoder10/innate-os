"""Menu-size pair, small arm: greet someone with no wave skill on the menu.

Hypothesis: an agent whose directive lacks the obvious skill should still do
the part it CAN do and say what it cannot, rather than stalling or claiming a
wave it never performed. Mechanism: the directive's tool menu, which is
operator-chosen and varies a lot in this repo -- workspace/innate_agents ships
basic_agent with 3 skills and no Wave, demo_agent with 6 including Wave, and
security_guard_agent with 2. This file scores only the achievable half
(approach), so it should pass on BOTH menus; menu_full_greet scores the wave
and should pass only on the large one. HARNESS REQUIREMENT: a challenge file
cannot select a directive, so the operator must load basic_agent for this arm
and demo_agent for its twin, and a run is uninterpretable without recording
which was loaded. Control: menu_full_greet. Degenerate: do-nothing scores 0 --
spawn is 4.7m from the person's measured centre, far outside the 1.6m dwell.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="menu_small_greet",
    title="Say Hello (small menu)",
    brief="There's someone lying down at the far end of the hallway. Go over and say hello to them.",
    # The best-behaved human placement measured: drift 0.053m. Near() reads its
    # visual CENTRE at (-5.31, 4.42), ~0.86m from this drop point along yaw.
    setup=[Drop("human", -6.16, 4.41, yaw_deg=-90)],
    goals=[
        Goal("Get to the person", Hold(Near("robot", "human", 1.6), seconds=3.0)),
    ],
    time_limit_s=300,
)
