"""Triage, All Three: the feasibility control for the one-trip version.

The same three objects, the same brief and twenty minutes. Every delivery the
tight run could have made is gradeable here, so this file answers the question
that makes 133 readable: are these three errands achievable at all, and in what
order does an unhurried agent do them? A pass here with a zero there is a
prioritisation failure. A zero here means the scene is too hard and 133 says
nothing.

Hypothesis: none -- this is the reference. Its per-item completion order, read
off the checklist, is also the unpressured ordering an agent falls into.
Mechanism: none targeted.
Control for: triage_tight.
Degenerate: 0.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="triage_generous",
    title="Triage: All Three",
    brief=(
        "Three things are on the floor and they all belong on the kitchen floor by the "
        "counter: a blue pill bottle in the bedroom hallway, and a red cube and a grey "
        "sock in the living room. The pill bottle is the one that matters -- if you only "
        "manage one, make it that one."
    ),
    setup=[
        Drop("can", -2.36, 3.01),
        Drop("cube", -4.41, -0.84),
        Drop("sock", -3.36, -1.19),
    ],
    goals=[
        # Ordered, but these are world-state predicates and they do not expire:
        # delivering out of order still latches every goal once the earlier
        # items arrive, because the later ones are still sitting in the rect.
        # Listed priority-first so the checklist reads as the brief does.
        Goal("Get the pill bottle to the kitchen", InRect("can", -5.74, -4.21, -3.49, -3.76)),
        Goal("Get the cube to the kitchen", InRect("cube", -5.74, -4.21, -3.49, -3.76)),
        Goal("Get the sock to the kitchen", InRect("sock", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=1200,
)
