"""Household rung 4: five chores, spoken in the order that walks best.

The top of the clause ladder, and the efficient half of the pair with 135. The
five errands are said in an order whose literal execution is a 22.3m route:
can to the recycling on the way out, sock to the laundry, football to the dog,
look in on the person, back to the charger. 135 is the identical challenge with
the identical goal list, spoken in an order whose literal execution is 30.3m --
so the pair measures whether the agent plans a route or reads a sentence.

Hypothesis: five clauses is where an agent stops carrying the whole instruction
and starts working from whichever fragment it last attended to.
Mechanism: instruction parsing and plan retention across a long composite run.
The can's leg is deliberately the SAME 1.79m carry as 112_workshop_can, so a
stall there can be attributed.
Control: 133_household_four_chores (the can clause removed);
135_household_bad_order (same task, hostile clause order) is the route twin.
Degenerate: 0.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="household_five_chores",
    title="Household: The Whole List",
    brief=(
        "A few bits and pieces before you shut down. There's a blue can in the "
        "middle of the living room -- stick it in the recycling by the kitchen "
        "counter. There's a grey sock up in the corridor, that goes in the "
        "laundry pile in the bedroom. The dog's football is in the corridor as "
        "well, push it through to him. Someone's lying down in the far corner "
        "by the bathroom, so look in on them. Then come back and park on the "
        "charger."
    ),
    setup=[
        Drop("human", -6.16, 4.41, yaw_deg=-90),
        Drop("soccer_ball", -4.36, 3.11),
        Drop("labrador", -0.49, 2.89, yaw_deg=90),
        Drop("sock", -4.36, 1.21),
        Drop("can", -4.41, -0.84),
    ],
    goals=[
        # This list is FIXED across 134 and 135 -- only the brief's wording
        # differs -- so the pair changes exactly one variable. The middle three
        # are memoryless, so they latch in whatever order the robot really
        # worked, and a batch of them cascades on the tick the first goal
        # closes.
        Goal("Check on them", Hold(Near("robot", "human", 1.5), seconds=3.0)),
        Goal("Football back to the dog", Near("soccer_ball", "labrador", 1.2)),
        Goal("Sock in the laundry pile", InCircle("sock", -3.46, 2.86, 0.6)),
        Goal("Can in the recycling", InCircle("can", -5.96, -1.74, 0.6)),
        Goal("Back on the charger", Hold(InCircle("robot", -4.34, -0.17, 0.5), seconds=3.0)),
    ],
    time_limit_s=1260,
)
