"""Storage crate, 0.10m tall. See 40_crate_00.py for the shared ladder contract.

The first height at which the camera loses the mug's base at ordinary room
range: standing 0.35m behind it, this crate hides a 60mm mug from anywhere
beyond ~0.85m.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="crate_10",
    label="▭",
    title="Crate",
    collision="box",
    size=(0.10, 0.15, 0.05),
    density=250,
    condim=4,
    friction=(1.2, 0.02, 0.001),
    rgba=(0.42, 0.44, 0.47, 1.0),
    rest_z=0.05,
)
