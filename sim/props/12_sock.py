"""Sock: pick_any_object's design target -- the prop the skill is measured against."""

from mars_sim_driver.props import Prop

# pick_any_object's design target: its default prompt is "the sock", and its
# grasp band sits HIGH -- the shipped constants close the jaws with the pad
# centre ~50mm off a hard floor (floor_z is an ee target, the pads ride ~10mm
# above it, close_lift adds 10mm more). A sock is a 60-80mm lump you pinch by
# the top; the 40mm cube is below that band and is the HARD case on real
# hardware too. This prop is what "the skill works" should be measured
# against: a rolled-sock-sized, sock-weight box (65g of fabric).
PROP = Prop(
    name="sock",
    label="🧦",
    title="Sock",
    collision="box",
    # 60mm tall (the skill's closing band lands on its upper third);
    # 40x40 so the worst-case diagonal clears the 81mm jaw at any yaw.
    # Grey, not white: white on pale parquet starves the flow tracker.
    size=(0.020, 0.020, 0.030),
    density=450,
    condim=6,
    rgba=(0.45, 0.46, 0.50, 1.0),
    rest_z=0.03,
    reach=(0.161, 0.154),
)
