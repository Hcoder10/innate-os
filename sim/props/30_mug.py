"""Mug: the standard pick target for the detection / localization probes.

Geometry is the can's, verbatim: 40mm across and 60mm tall is the one
cylinder size 11_can.py established as actually graspable, and a probe that
changes it would confound its own axis with a grasp regression. What differs
is drop_z -- Drop() carries no z and drop_z defaults to rest_z, so a prop that
must start on top of something needs its own release height. 0.38 clears the
tallest crate (top 0.30) and still only falls 0.35m onto the bare floor.
"""

from mars_sim_driver.props import Prop

# Ungrouped on purpose: "lay out the manipulation set" places every group
# member at its own reach offset, and a sixth prop there would land on the
# can's spot.
PROP = Prop(
    name="mug",
    label="☕",
    title="Mug",
    collision="cylinder",
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    # Violet: no other prop is near this hue, and it is far from the parquet
    # in both hue and value -- 31_mug_pale.py is the matched low-contrast twin.
    rgba=(0.58, 0.24, 0.72, 1.0),
    rest_z=0.03,
    drop_z=0.38,
    reach=(0.296, 0.011),
)
