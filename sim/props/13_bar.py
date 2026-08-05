"""Bar: the long, thin manipulation target (yaw matters to the grasp)."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="bar",
    label="🍫",
    title="Bar",
    collision="box",
    size=(0.015, 0.05, 0.015),
    density=700,
    condim=4,
    # Deep orange: yellow on pale parquet starves the flow tracker.
    rgba=(0.80, 0.33, 0.10, 1.0),
    rest_z=0.015,
    reach=(0.296, -0.117),
)
