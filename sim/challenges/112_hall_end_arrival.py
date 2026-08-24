"""The same hall-end can, scored on arrival alone: the capability control.

Hypothesis: the robot can get to the far end of the hall at all. Mechanism:
none in particular -- that is the point. This is the control both arms of the
skill-choice partition need, because a run that fails ground_hall_end_nav AND
ground_hall_end_straight is ambiguous between "used a third skill" and "never
arrived", and only this file separates them. It is also the one file in the
family that scores with no rosbridge, so it doubles as the family's smoke test.
Degenerate: a do-nothing policy scores 0 (spawn is 3.28m from the can, well
outside 0.9m); a random walk scores a few percent -- roughly the 2.5m2 dwell
disc over ~36m2 of reachable floor, and it has to hold still there for 3s.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="ground_hall_end_arrival",
    title="Down the Hall (arrival control)",
    brief="There's a can down at the far end of the hallway. Go and stand next to it.",
    setup=[Drop("can", -4.36, 3.11)],
    goals=[
        # Dwell, not a touch: a robot that sails past the can on its way
        # somewhere else has not stood next to it.
        Goal("Stand next to the can", Hold(Near("robot", "can", 0.9), seconds=3.0)),
    ],
    time_limit_s=300,
)
