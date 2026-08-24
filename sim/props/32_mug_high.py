"""High-release mug: 30_mug.py with a drop_z that clears real furniture.

Same geometry, same colour, same mass -- only the release height differs, so
the dining-table probe and its floor twin are looking at the same object. The
0.38 release the crates need would spawn this one INSIDE the 0.754m tabletop;
21_soccer_ball.py dropped below a solid surface was ejected 6.6m and finished
outside the apartment.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="mug_high",
    label="☕",
    title="Mug",
    collision="cylinder",
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.58, 0.24, 0.72, 1.0),
    rest_z=0.03,
    drop_z=1.10,  # ~0.35m of clear fall onto the 0.754m dining table
    reach=(0.296, 0.011),
)
