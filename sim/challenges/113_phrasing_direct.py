"""Paraphrase ladder, rung 1 of 3: the bare imperative.

Hypothesis: an agent's grounding of one fixed physical task should not depend
on how the request is worded. Mechanism: the language->skill+argument step in
the directive, upstream of any motion; the scene, the goal predicate and the
time limit are byte-identical across ground_phrasing_direct,
ground_phrasing_polite and ground_phrasing_worried, so the ONLY variable is the
brief. The axis is directness: rung 1 names the object, the room and the action
outright; rung 2 softens the action into a request; rung 3 removes the
imperative entirely and asks a question. A pass-rate that falls along that axis
is paraphrase brittleness and nothing else. Control: the other two rungs are
each other's controls. Degenerate: a do-nothing policy scores 0 (spawn is 4.9m
from the sock); a drive-to-the-nearest-object policy scores 0, because the can
sits 1.4m from spawn, on the way, and is not the target.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="ground_phrasing_direct",
    title="Sock in the Bathroom (plainly)",
    brief="Go to the sock on the bathroom floor and stop next to it.",
    # The can is the anti-greed control: nearer than the sock and on the route,
    # so any "approach whatever you find first" policy commits to it and fails.
    setup=[
        Drop("sock", -6.16, 4.41),
        Drop("can", -4.36, 1.21),
    ],
    goals=[
        Goal("Stand next to the sock", Hold(Near("robot", "sock", 0.9), seconds=3.0)),
    ],
    time_limit_s=300,
)
