"""Stress ball: the least forgiving manipulation target."""

from mars_sim_driver.props import Prop

# 40mm, the same width as the cube and can: a 50mm sphere left only 15mm of jaw
# clearance per side, so the descending blade grazed its flank on ordinary aim
# error, stalled the descent high, and the jaws closed over the top cap without
# ever moving the ball (measured: pad centre 31mm above ball centre at the
# close, ball displaced 4mm total, 0/4). A sphere needs the pads at its equator
# -- it is the least forgiving shape in the roster, so it gets the friendliest
# width. Density is a dense rubber ball's: at the sphere's tiny volume the real
# bouncy-ball ~1100 lands at 37g, inside the skitter zone props.py warns about.
PROP = Prop(
    name="ball",
    label="🎾",
    group="manipulation",
    title="Stress ball",
    collision="sphere",
    size=(0.0225,),
    density=1000,
    condim=6,
    # Foam stress ball: a hard sphere is unpickable (the contact rolls around
    # the curve and the arm rides up over it). ~3mm of dent stops the roll;
    # much softer slips. Priority beats the fingers' pad model.
    priority=4,
    friction=(2.0, 0.4, 0.1),
    solref=(0.012, 1.0),
    solimp=(0.9, 0.97, 0.003),
    rgba=(0.4, 0.8, 0.45, 1.0),
    rest_z=0.0225,
    reach=(0.227, -0.221),
)
