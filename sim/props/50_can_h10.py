"""Can, 100mm tall: rung 1 of the pinhole height ladder."""

from mars_sim_driver.props import Prop

# 11_can.py exactly, except twice as tall. pick_any_object back-projects the
# grasp pixel onto the FLOOR PLANE (innate.geometry.pixel_to_floor), so an
# object whose centre sits h above the floor is reported at
# x_apparent = x_true * cam_z / (cam_z - h), cam_z = 0.2438m at tilt -20.
# Servoing that pixel into the pick box therefore stops the base at
# 0.37*(cam_z-h)/cam_z: 0.294m here against the can's 0.324m. Only rest_z
# moves along this ladder -- width, colour, density and contact model are the
# can's, so nothing but the height can explain a rung's result.
PROP = Prop(
    name="can_h10",
    label="🥫",
    title="Can (100mm)",
    collision="cylinder",
    size=(0.020, 0.050),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.25, 0.55, 0.85, 1.0),
    rest_z=0.050,
    reach=(0.296, 0.011),
)
