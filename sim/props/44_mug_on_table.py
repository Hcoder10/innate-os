"""Mug that starts on the dining table: 43_mug_on_floor.py plus a release height.

Drop() carries no z and Prop.drop_z defaults to rest_z, so every small prop is
released at floor level and a challenge file physically cannot put one on
furniture. drop_z=1.10 clears the 0.754m tabletop with ~0.35m of fall. Nothing
else differs from 43_mug_on_floor.py -- an elevated target has to be the same
object as the floor control or the pair measures the object, not the height.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="mug_on_table",
    label="☕",
    title="Mug",
    collision="cylinder",
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.90, 0.85, 0.30, 1.0),
    rest_z=0.03,
    drop_z=1.10,
    reach=(0.296, 0.011),
)
