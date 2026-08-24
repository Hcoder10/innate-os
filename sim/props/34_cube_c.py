"""Cube C: the third indistinguishable copy, so a row has a middle."""

from mars_sim_driver.props import Prop

# Two copies give left/right; an ordinal reference needs three. See
# 33_cube_b.py for why every field here is cube's, and 30_blue_cube.py for why
# group is None.
PROP = Prop(
    name="cube_c",
    label="🧊",
    title="Cube",
    collision="box",
    size=(0.02, 0.02, 0.02),
    density=700,
    condim=4,
    rgba=(0.85, 0.28, 0.24, 1.0),
    rest_z=0.02,
    reach=(0.227, 0.116),
)
