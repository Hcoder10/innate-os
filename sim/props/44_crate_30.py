"""Storage crate, 0.30m tall. See 40_crate_00.py for the shared ladder contract.

The rung above the horizon: a mug on top sits ~0.33m up, higher than the
0.244m head camera, so its ray never descends to the floor plane at all and
pixel_to_floor returns None rather than a wrong number. The failure changes in
kind here, not just in size, which is why the ladder runs this far.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="crate_30",
    label="▭",
    title="Tall crate",
    collision="box",
    size=(0.10, 0.15, 0.15),
    density=250,
    condim=4,
    friction=(1.2, 0.02, 0.001),
    rgba=(0.42, 0.44, 0.47, 1.0),
    rest_z=0.15,
)
