"""Gallery, bearing probe: the only exhibit is directly behind the robot.

One can, 1.38m from spawn on a bearing of 180 deg -- the robot starts facing
away from it. Byte-identical to 105 apart from that one coordinate, and the
brief deliberately does not say where the can is, because saying so is the
whole answer.

Hypothesis: this agent cannot find an object outside a ~90 deg wedge ahead of
it, no matter how close the object is.
Mechanism: pick_any_object._search scans exactly three head yaws -- 0, -30,
+60 -- and then raises SkillFailed. Nothing in that sequence turns the base, so
a target at 180 deg is never imaged. A failure here with 105 passing implicates
the search arc and nothing else: same prop, same colour, same dwell, range
matched to 0.03m.
Control: 105_gallery_in_the_arc (bearing 44 deg, range 1.41m).
Degenerate: do-nothing 0. Random-walk is NOT flatly zero here -- 300s in a 12m2
room can wander inside a 0.9m disc -- but it must also hold that disc for 3s,
so read 104 against 105 as a pair, never as an absolute score.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

# Ladder axis (105 -> 104): bearing of the target from the start heading,
# 44 deg -> 180 deg, with range held at 1.38-1.41m.
CHALLENGE = Challenge(
    id="gallery_behind_you",
    title="Gallery: Behind You",
    brief=(
        "There's a blue can on the floor somewhere close by. Find it and stand "
        "with it for a few seconds."
    ),
    setup=[Drop("can", -4.36, 1.21)],
    goals=[
        Goal("Find the blue can", Near("robot", "can", 1.2)),
        Goal("Stand with it for 3 seconds", Hold(Near("robot", "can", 0.9), seconds=3.0)),
    ],
    time_limit_s=300,
)
