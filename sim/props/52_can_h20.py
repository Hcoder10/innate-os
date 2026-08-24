"""Can, 200mm tall: rung 3 of the pinhole height ladder."""

from mars_sim_driver.props import Prop

# The rung that crosses Manipulation.REACH_X's near limit: the pick box closes
# at 0.218m, just inside 0.22, so even a perfect grasp command clamps outward
# away from the can. See 50_can_h10.py for the ladder.
PROP = Prop(
    name="can_h20",
    label="🥫",
    title="Can (200mm)",
    collision="cylinder",
    size=(0.020, 0.100),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.25, 0.55, 0.85, 1.0),
    rest_z=0.100,
    reach=(0.296, 0.011),
)
