"""Parameter extraction, rung 2 of 2: the same sentence with a bigger number.

See ground_back_up_short for the hypothesis and the mechanism; this rung moves
the requested distance from 1.5m to 3m and nothing else. Control:
ground_back_up_short. Degenerate: do-nothing scores 0, and so does an agent
that always reverses 1.5m -- the two discs are disjoint, which is the whole
design. KNOWN CONFOUND, stated rather than hidden: the route to this disc
passes a doorway pinch at y~2.5 where the corridor narrows to 0.20m of
clearance either side of the robot's 0.165m half-width, so a blind odometry
reverse can wedge there. A fail here with a pass on rung 1 is therefore
parameter extraction OR that pinch, and the two are told apart by where the
robot actually stopped (short of the disc versus jammed at y~2.5), which the
viewer shows live but the engine does not record.
"""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="ground_back_up_long",
    title="Back Up 3m",
    brief="Back up about three metres and wait there.",
    setup=[],
    goals=[
        # 3.0m back along +y from spawn; probed clear_r 0.35, above the robot's
        # 0.30m circumscribed radius.
        Goal("Stop 3m back", Hold(InCircle("robot", -4.34, 2.83, 0.45), seconds=3.0)),
    ],
    time_limit_s=300,
)
