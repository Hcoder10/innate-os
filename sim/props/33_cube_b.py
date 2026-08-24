"""Cube B: an indistinguishable copy of `cube`."""

from mars_sim_driver.props import Prop

# Every field is cube's, deliberately: ordinal ("the middle one"), relational
# ("the one next to the dog") and ambiguity ("bring me the cube" with two out)
# probes are only honest when NOTHING but position separates the candidates. A
# single distinguishing feature would let the robot pass by naming a colour.
# Ground truth still tells them apart, because the engine keys on prop name.
# See 30_blue_cube.py for why group is None.
PROP = Prop(
    name="cube_b",
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
