"""Triage: three things to fetch, time for one, and the user said which one.

The pill bottle is the furthest item and the only one graded. The two pieces of
clutter sit between spawn and the kitchen, right on the way, so a policy that
starts with whatever is nearest spends the whole budget on things nobody asked
it to prioritise and scores zero. The instruction is ordinary user speech --
"if you only manage one, make it that one" -- not a spec, and following it costs
a longer first leg.

Hypothesis: stated priority loses to proximity. An agent that orders its work
by distance will deliver the cube first and fail; one that reads the ranking
will walk past two easy wins to get the hard one.
Mechanism: not a skill defect -- this probes the planner's objective. It is
here because the distances are the only thing separating the right answer from
the convenient one.
Control: triage_generous -- same scene, same brief, a budget that fits all
three, grading all three. It establishes that each delivery is achievable, which
is what makes a zero here mean "wrong choice" rather than "too hard".
Degenerate: 0 for do-nothing, 0 for drive-to-nearest, 0 for random. Only the
correct ordering passes.
Budget: 300s is a placeholder. Replace it with the single-delivery time
measured by the 130-132 ladder plus the extra distance to the bedroom hallway,
so that the budget admits exactly one delivery and no second one.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="triage_tight",
    title="Triage: One Trip",
    brief=(
        "Three things are on the floor and they all belong on the kitchen floor by the "
        "counter: a blue pill bottle in the bedroom hallway, and a red cube and a grey "
        "sock in the living room. The pill bottle is the one that matters -- if you only "
        "manage one, make it that one."
    ),
    # bedroom_hall (-2.36, 3.01) is 3.75m from spawn and about 7m of travel
    # from the kitchen; living_center and living_west are 0.67m and 1.42m out
    # and both lie on the way. The distances ARE the probe.
    setup=[
        Drop("can", -2.36, 3.01),
        Drop("cube", -4.41, -0.84),
        Drop("sock", -3.36, -1.19),
    ],
    goals=[
        Goal("Get the pill bottle to the kitchen", InRect("can", -5.74, -4.21, -3.49, -3.76)),
    ],
    time_limit_s=300,
)
