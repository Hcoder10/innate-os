"""Household rung 3: four chores -- the first one needs a real grasp.

The sock joins in, and with it the first clause on this ladder that an agent
which cannot pick things up simply cannot do. From here the ladder is
conditional: read a stall on the sock clause against 111_workshop_sock, which
is the same object over a comparable 1.88m carry with nothing else going on. If
111 fails too, this rung is measuring manipulation, not composition.

Hypothesis: clause survival degrades with clause count independently of what
the clauses are -- so the fourth clause is lost at a similar rate whether it is
easy or hard, and the sock's difficulty shows up in TIME rather than in which
goals latch.
Mechanism: the agent's plan over four errands, plus pick_any_object end to end
for the sock leg.
Control: 132_household_three_chores (same instruction with the sock clause
removed); 111_workshop_sock for the sock leg in isolation.
Degenerate: 0.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="household_four_chores",
    title="Household: Four Things",
    brief=(
        "There's a grey sock in the corridor -- that goes in the laundry pile "
        "in the bedroom. The dog's football is up in the corridor too, push it "
        "through to him. Someone's lying down in the far corner up by the "
        "bathroom, so look in on them. Then park yourself back on the charger."
    ),
    setup=[
        Drop("human", -6.16, 4.41, yaw_deg=-90),
        Drop("soccer_ball", -4.36, 3.11),
        Drop("labrador", -0.49, 2.89, yaw_deg=90),
        Drop("sock", -4.36, 1.21),
    ],
    goals=[
        Goal("Check on them", Hold(Near("robot", "human", 1.5), seconds=3.0)),
        Goal("Football back to the dog", Near("soccer_ball", "labrador", 1.2)),
        # 0.6m zone at a probed bedroom floor point, 1.88m from where the sock
        # starts: a shove from the base cannot cover that.
        Goal("Sock in the laundry pile", InCircle("sock", -3.46, 2.86, 0.6)),
        Goal("Back on the charger", Hold(InCircle("robot", -4.34, -0.17, 0.5), seconds=3.0)),
    ],
    time_limit_s=960,
)
