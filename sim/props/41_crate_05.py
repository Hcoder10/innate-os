"""Storage crate, 0.05m tall. See 40_crate_00.py for the shared ladder contract.

At this height the head camera still sees a 60mm mug's base from 1.7m, and a
mug standing on it sits ~0.08m up: the back-projected floor point overshoots
the true range by ~1.7x, which is the smallest error in the ladder that is
still larger than the pick box.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="crate_05",
    label="▭",
    title="Low crate",
    collision="box",
    size=(0.10, 0.15, 0.025),
    density=250,
    condim=4,
    friction=(1.2, 0.02, 0.001),
    rgba=(0.42, 0.44, 0.47, 1.0),
    rest_z=0.025,
)
