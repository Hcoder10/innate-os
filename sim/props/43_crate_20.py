"""Storage crate, 0.20m tall. See 40_crate_00.py for the shared ladder contract.

A mug on this one sits ~0.23m up, just under the 0.244m head camera, so its
sightline to the floor is almost horizontal and pixel_to_floor's back-projected
range blows up by more than an order of magnitude. The next crate up crosses
the camera height entirely.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="crate_20",
    label="▭",
    title="Tall crate",
    collision="box",
    size=(0.10, 0.15, 0.10),
    density=250,
    condim=4,
    friction=(1.2, 0.02, 0.001),
    rgba=(0.42, 0.44, 0.47, 1.0),
    rest_z=0.10,
)
