"""Can: the cylindrical manipulation target."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="can",
    label="🥫",
    title="Can",
    collision="cylinder",
    # 40mm across: 50mm pinches at the fingertip and rolls out. 60mm
    # tall: taller fills pick_any_object's optical-flow window with
    # featureless colour and positioning livelocks.
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),  # the third term is rolling resistance on the floor
    rgba=(0.25, 0.55, 0.85, 1.0),
    rest_z=0.03,
    reach=(0.296, 0.011),
)
