"""Third sock: see 41_sock_b.py. Drops the guessing baseline from 1/2 to 1/3."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="sock_c",
    label="🧦",
    title="Sock",
    collision="box",
    size=(0.020, 0.020, 0.030),
    density=450,
    condim=6,
    rgba=(0.45, 0.46, 0.50, 1.0),
    rest_z=0.03,
    reach=(0.227, -0.154),
)
