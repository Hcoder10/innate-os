"""Mug that starts on the floor: the reachable half of the height pair.

Geometry is 11_can.py's verbatim -- 40mm across and 60mm tall is the cylinder
size that file established as actually graspable, and changing it would
confound the height axis with a grasp regression. The name is NOT plain "mug":
30_mug.py already claims that, props load by name with later files winning, and
quietly overriding another probe's release height and colour is a worse bug
than a long name. Paired field-for-field with 44_mug_on_table.py, which differs
only in drop_z -- an elevated target has to be the same object as its floor
control or the pair measures the object instead of the height.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="mug_on_floor",
    label="☕",
    title="Mug",
    collision="cylinder",
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.90, 0.85, 0.30, 1.0),
    rest_z=0.03,
    reach=(0.296, 0.011),
)
