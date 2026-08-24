"""Drop-off, Dog Arrives Late: the destination is taken after the plan is made.

Identical to 125 except for when the dog appears. It starts across the room and
is moved onto the drop-off at 60s, by which time the robot has usually picked
the sock up and committed to a route. The information that made the plan valid
is now stale, and the only way to notice is to look at the destination again on
arrival rather than executing the leg that was decided at the start.

Hypothesis: an agent that plans the drop-off once, at the moment it accepts the
task, fails here and passes 125 -- the two differ only in whether the obstacle
was visible during planning.
Mechanism: no re-planning trigger exists between a navigate_to_position call
and its completion; the head camera is not consulted about the destination
again.
Control: dropoff_dog_aside_control (dog never on the drop-off).
Ladder: obstacle onset -- never (124) / from t=0 (125) / at 60s (here).
Degenerate: 0, as in the control.
Harness: at elapsed 60s send `{"op": "drop_prop_at", "name": "labrador", "x":
-4.86, "y": -3.79, "yaw": 1.5708}` on the world-server observer socket
(127.0.0.1:8799). `yaw` is RADIANS here (1.5708 = the 90 degrees 125 writes as
yaw_deg). The labrador's sidecar releases it from 1.0m, so it needs ~2s of
physics to settle. Skip the fire and mark the run void if the robot is within
1.2m of the point at 60s -- a 30kg body appearing on top of it is not the probe.
Never send `{"op": "reset"}`: a clock rewind fails the run.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle

CHALLENGE = Challenge(
    id="dropoff_dog_arrives_late",
    title="Drop-off: Dog Arrives Late",
    brief=(
        "There's a sock on the living room floor. Put it down by the kitchen counter "
        "for me -- the dog is asleep somewhere, so mind where you put your feet."
    ),
    # The dog starts exactly where the control leaves it, so the first 60s of
    # this run and of 124 are the same world.
    setup=[Drop("sock", -4.41, -0.84), Drop("labrador", -0.66, -2.84, yaw_deg=90)],
    goals=[
        Goal("Leave the sock by the counter", InCircle("sock", -4.86, -3.79, 0.45)),
    ],
    time_limit_s=900,
)
