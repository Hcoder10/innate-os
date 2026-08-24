"""Paraphrase ladder, rung 2 of 3: the same task as a polite request.

See ground_phrasing_direct for the hypothesis, the mechanism and the axis. This
rung keeps the object and the room explicit but replaces the imperative with a
request frame and a vague action verb ("check on"), which is what most real
users actually say. Scene, goal and limit are identical to the other two rungs.
Control: rungs 1 and 3. Degenerate: 0 for do-nothing and 0 for
drive-to-the-nearest-object, exactly as in rung 1.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="ground_phrasing_polite",
    title="Sock in the Bathroom (politely)",
    brief="Could you go and check on the sock in the bathroom for me?",
    setup=[
        Drop("sock", -6.16, 4.41),
        Drop("can", -4.36, 1.21),
    ],
    goals=[
        Goal("Stand next to the sock", Hold(Near("robot", "sock", 0.9), seconds=3.0)),
    ],
    time_limit_s=300,
)
