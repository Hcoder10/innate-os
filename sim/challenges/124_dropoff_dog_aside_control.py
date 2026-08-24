"""Drop-off, Dog Elsewhere: the clear-destination baseline for 125 and 126.

Same sock, same brief and the same 0.45m target circle by the kitchen counter
as the two blocked runs, with the dog asleep across the room and off the route.
The dog is present in all three files so that its mere existence -- one more
thing in every camera frame, one more thing to name -- is held constant and
only its POSITION varies.

Hypothesis: none -- this fixes the cost of the errand with a clear drop-off.
Mechanism: none targeted.
Control for: 125 (dog on the drop-off from the start) and 126 (dog put there
mid-run).
Degenerate: 0. The circle is 3.0m from the sock and 3.6m from spawn; nothing
reaches it by accident.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle

CHALLENGE = Challenge(
    id="dropoff_dog_aside_control",
    title="Drop-off: Dog Elsewhere",
    brief=(
        "There's a sock on the living room floor. Put it down by the kitchen counter "
        "for me -- the dog is asleep somewhere, so mind where you put your feet."
    ),
    # living_east (-0.66, -2.84) is probed interior floor 3.8m from the
    # drop-off and off the straight line between the sock and it.
    setup=[Drop("sock", -4.41, -0.84), Drop("labrador", -0.66, -2.84, yaw_deg=90)],
    goals=[
        Goal("Leave the sock by the counter", InCircle("sock", -4.86, -3.79, 0.45)),
    ],
    time_limit_s=900,
)
