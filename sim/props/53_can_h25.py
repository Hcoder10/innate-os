"""Can, 250mm tall: the last rung below the camera horizon."""

from mars_sim_driver.props import Prop

# Centre 0.125m up: the pick box closes at 0.180m, so the arm is commanded to
# grasp 0.140m PAST the can and sweeps through it on the way down. Still below
# the 0.2438m horizon, so the skill localizes it and commits -- 54_can_shelf.py
# is the rung above the horizon, where it cannot. See 50_can_h10.py.
PROP = Prop(
    name="can_h25",
    label="🥫",
    title="Can (250mm)",
    collision="cylinder",
    size=(0.020, 0.125),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.25, 0.55, 0.85, 1.0),
    rest_z=0.125,
    reach=(0.296, 0.011),
)
