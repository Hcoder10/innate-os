"""Can, 150mm tall: rung 2 of the pinhole height ladder."""

from mars_sim_driver.props import Prop

# Centre 0.075m up, so the pick box closes at 0.256m instead of 0.370m and the
# grasp is commanded 0.064m beyond the can -- past the ~20mm of slack an 81mm
# jaw leaves around a 40mm cylinder. See 50_can_h10.py for the ladder.
PROP = Prop(
    name="can_h15",
    label="🥫",
    title="Can (150mm)",
    collision="cylinder",
    size=(0.020, 0.075),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.25, 0.55, 0.85, 1.0),
    rest_z=0.075,
    reach=(0.296, 0.011),
)
