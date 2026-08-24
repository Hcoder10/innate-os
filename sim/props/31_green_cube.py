"""Green cube: the third colour, so a negated reference has a unique answer."""

from mars_sim_driver.props import Prop

# "Not the red one" only names a single object once there are exactly two
# alternatives to exclude, so the negation probes need a third colour. Green is
# the stress ball's, already chosen to survive the pale parquet. See
# 30_blue_cube.py for why group is None.
PROP = Prop(
    name="green_cube",
    label="🟩",
    title="Green cube",
    collision="box",
    size=(0.02, 0.02, 0.02),
    density=700,
    condim=4,
    rgba=(0.4, 0.8, 0.45, 1.0),
    rest_z=0.02,
    reach=(0.227, 0.116),
)
