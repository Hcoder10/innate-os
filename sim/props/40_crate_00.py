"""Storage crate, 0.02m tall: the zero rung of the elevation ladder.

The five crates share one footprint (0.20 x 0.30), one colour and one mass
band, and differ only in height, so a challenge can move an object up in 0.05m
steps without changing anything else in the scene -- that is the whole point of
carrying a flat one nobody would call a crate. drop_z is left unset, so the
crate is PLACED at rest rather than dropped: it must already be still when the
prop standing on it lands.

They double as occluders. From the head camera (0.244m above the floor at the
-20 degree pick tilt) a crate this tall stops hiding a 60mm mug beyond ~4.3m,
which is further than any sightline in this apartment.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="crate_00",
    label="▭",
    title="Flat crate",
    collision="box",
    size=(0.10, 0.15, 0.01),
    density=250,
    condim=4,
    friction=(1.2, 0.02, 0.001),
    rgba=(0.42, 0.44, 0.47, 1.0),
    rest_z=0.01,
)
