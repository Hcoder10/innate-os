"""Blue cube: the colour twin of `cube`, for referring-expression probes."""

from mars_sim_driver.props import Prop

# Geometry, mass and contact model are cube's exactly -- colour is the ONLY
# difference, so "the red one" vs "the blue one" cannot be answered by size,
# shape or how it behaves under the gripper. The blue is the can's, which the
# roster already trusts on pale parquet. group=None because these twins are
# placed deliberately into a scene, never laid out with the practice set: they
# share cube's reach offset and would land on top of it.
PROP = Prop(
    name="blue_cube",
    label="🟦",
    title="Blue cube",
    collision="box",
    size=(0.02, 0.02, 0.02),
    density=700,
    condim=4,
    rgba=(0.25, 0.55, 0.85, 1.0),
    rest_z=0.02,
    reach=(0.227, 0.116),
)
