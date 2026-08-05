"""Cube: the plain rigid manipulation target."""

from mars_sim_driver.props import Prop

# Densities are real materials'; do NOT lighten them -- at 20-40g a prop
# skitters metres off the lightest graze.
PROP = Prop(
    name="cube",
    label="🧊",
    title="Cube",
    collision="box",
    size=(0.02, 0.02, 0.02),
    density=700,
    condim=4,
    rgba=(0.85, 0.28, 0.24, 1.0),
    rest_z=0.02,  # resting height of the body origin = half the footprint
    # Robot-frame metres, on a 0.22m arc around the arm's shoulder so the prop
    # lands where the arm can reach it top-down.
    reach=(0.227, 0.116),
)
