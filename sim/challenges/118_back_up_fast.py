"""Out-of-range parameter: the right distance carrying an impossible speed.

Hypothesis: a parameter the skill cannot honour should degrade to a clamp, not
derail the call. Mechanism: move_straight clamps speed into [0.05, 0.30] m/s
and turn_in_place into [0.2, 1.0] rad/s, so "three metres a second" is silently
saturated and the move still has to happen; the interesting failures are an
agent that refuses the whole request over the impossible half, or one that
invents a speed argument on a skill that has none. Scene, goal disc and limit
are identical to ground_back_up_short -- the ONLY difference is the trailing
speed clause, so the pass-rate gap between the two files is the cost of the
out-of-range parameter and nothing else. Control: ground_back_up_short.
Degenerate: do-nothing scores 0, exactly as in rung 1.
"""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="ground_back_up_fast",
    title="Back Up 1.5m, Impossibly Fast",
    brief="Back up a metre and a half and wait there — quick as you can, three metres a second.",
    setup=[],
    goals=[
        Goal("Stop 1.5m back", Hold(InCircle("robot", -4.34, 1.33, 0.45), seconds=3.0)),
    ],
    time_limit_s=300,
)
