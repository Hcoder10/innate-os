"""Workshop, shape ladder rung 3: the bar.

Identical to 111 except that the object is 30 x 100 x 30mm -- the first rung
where the object's YAW matters. Approached along its length the jaws close on
100mm of bar and cannot span it; approached across it they close on 30mm.
Nothing in the brief mentions orientation, because a real user would not.

Hypothesis: the pipeline has no notion of grasp orientation, so bar attempts
succeed or fail on the accident of which way the robot happened to approach.
Mechanism: the scripted grasp -- a fixed wrist pose descended blindly. There
is no yaw search anywhere between detection and close.
Control: 111_workshop_sock (square footprint, same height class, everything
else equal). Run several attempts: the prediction here is high VARIANCE, not a
uniformly low score, and one attempt cannot show that.
Degenerate: 0.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

# yaw_deg=0 lays the 100mm axis along world +y, which is the line the robot
# approaches down from spawn. Which way round the jaws prefer is not something
# this file claims -- it is pinned so the rung measures the shape rather than a
# fresh random draw each attempt.
CHALLENGE = Challenge(
    id="workshop_bar",
    title="Workshop: The Bar",
    brief=(
        "There's an orange bar on the floor just in front of you. Pick it up "
        "and put it down by the kitchen counter."
    ),
    setup=[Drop("bar", -4.41, -0.84, yaw_deg=0)],
    goals=[
        Goal("Get to the bar", Near("robot", "bar", 0.5)),
        Goal("Bar down by the kitchen counter", InCircle("bar", -5.96, -1.74, 0.5)),
    ],
    time_limit_s=420,
)
