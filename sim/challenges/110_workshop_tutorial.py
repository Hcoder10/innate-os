"""Workshop rung 0: the design-target object, a forgiving drop zone.

One instruction, one object, one place to put it. The sock is the prop
pick_any_object was tuned around -- its sidecar says so -- and the zone is
0.9m across, so anywhere vaguely by the kitchen counter counts. This is the
prerequisite the rest of the Workshop ladder is conditional on: if this fails,
111-116 measure nothing about shape or distance.

Hypothesis: a grasp-and-release of the skill's own design target, over 1.79m,
is inside this agent's envelope.
Mechanism: the whole pick_any_object pipeline end to end -- detect, back-
project, servo, blind descent, verify. Deliberately not isolated.
Control: 111_workshop_sock is the same task with the zone tightened to 0.5m;
the two together separate "cannot grasp" from "cannot place accurately".
Degenerate: 0. Nothing moves an object without touching it, and a random walk
that shoves the sock has to shove it 0.89m in one particular direction.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

# Pick spot 0.67m dead ahead of spawn and the zone 1.79m past it, so the sock
# has to travel at least 0.89m: a graze from the base cannot satisfy this.
CHALLENGE = Challenge(
    id="workshop_tutorial",
    title="Workshop: Warm-up",
    brief=(
        "There's a grey sock on the floor just in front of you. Pick it up and "
        "put it down over by the kitchen counter -- anywhere close is fine."
    ),
    setup=[Drop("sock", -4.41, -0.84)],
    goals=[
        # 0.5m, not 0.8: spawn is already 0.67m from the mark, so a wider
        # radius would latch this goal before the robot moved. 0.5m is also
        # about where a grasp happens -- the props' `reach` offsets put a held
        # object 0.22-0.32m from the base origin.
        Goal("Get to the sock", Near("robot", "sock", 0.5)),
        Goal("Sock down by the kitchen counter", InCircle("sock", -5.96, -1.74, 0.9)),
    ],
    time_limit_s=600,
)
