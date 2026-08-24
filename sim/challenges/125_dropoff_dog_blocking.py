"""Drop-off, Dog In The Way: the destination is occupied when the robot gets there.

The dog is lying on the exact point the sock has to end up near. The circle is
0.45m and the dog's collision hull is about 0.25m across, so the errand is still
solvable -- but only by putting the sock BESIDE the named spot instead of on it.
A robot that treats the destination as a coordinate to drive onto stalls against
30kg of labrador; one that treats it as a region places the sock and leaves.

Hypothesis: destinations are handled as points, not as regions with occupancy,
so an occupied drop-off produces a stall rather than an offset placement.
Mechanism: navigate_to_position drives to an xy; pick_any_object's placement
side has no notion of "somewhere near here that is free". Nothing in the stack
re-selects a drop point.
Control: dropoff_dog_aside_control (same sock, same brief, same circle, dog off
the route).
Ladder: obstacle onset -- never (124) / from t=0 (here) / at 60s (126).
Degenerate: 0, as in the control.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle

CHALLENGE = Challenge(
    id="dropoff_dog_blocking",
    title="Drop-off: Dog In The Way",
    brief=(
        "There's a sock on the living room floor. Put it down by the kitchen counter "
        "for me -- the dog is asleep somewhere, so mind where you put your feet."
    ),
    # yaw 90 lays the dog's 1.0m long axis ALONG the counter run, so it covers
    # the approach the robot would otherwise take straight down the band.
    setup=[Drop("sock", -4.41, -0.84), Drop("labrador", -4.86, -3.79, yaw_deg=90)],
    goals=[
        Goal("Leave the sock by the counter", InCircle("sock", -4.86, -3.79, 0.45)),
    ],
    time_limit_s=900,
)
