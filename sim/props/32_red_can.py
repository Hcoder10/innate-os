"""Red can: closes the colour x shape square {red, blue} x {cube, can}."""

from mars_sim_driver.props import Prop

# With cube (red box), blue_cube (blue box) and can (blue cylinder) already in
# the roster, this one prop makes every conjunctive reference necessary: colour
# alone and shape alone each leave two candidates, so "the red can" can only be
# resolved by binding both. Geometry and contact model are the can's exactly --
# the 40mm width and 60mm height are load-bearing (see 11_can.py). See
# 30_blue_cube.py for why group is None.
PROP = Prop(
    name="red_can",
    label="🥫",
    title="Red can",
    collision="cylinder",
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.85, 0.28, 0.24, 1.0),
    rest_z=0.03,
    reach=(0.296, 0.011),
)
