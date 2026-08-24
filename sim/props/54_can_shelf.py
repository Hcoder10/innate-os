"""Can released above furniture height: the rung past the camera horizon."""

from mars_sim_driver.props import Prop

# 11_can.py with one field changed. Drop() carries no z, and the can's own
# drop_z defaults to rest_z (floor level), so landing the SAME object on a
# worktop needs a second sidecar rather than a second coordinate. 1.10m clears
# the 0.754m dining table by the 0.35m the settle tests wanted.
PROP = Prop(
    name="can_shelf",
    label="🥫",
    title="Can (worktop)",
    collision="cylinder",
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.25, 0.55, 0.85, 1.0),
    rest_z=0.03,
    drop_z=1.10,
    reach=(0.296, 0.011),
)
