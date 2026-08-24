"""Second sock: indistinguishable from 12_sock.py, for "which one?" probes.

A clarifying-question probe needs two candidates a camera cannot tell apart,
and the engine keys world state by prop NAME -- so a second identical object
has to be a second sidecar rather than a second drop of the first. Every
physical field is copied from the sock verbatim; only `name` and `reach`
differ. `group` is deliberately None: the manipulation set is what the arm
practises on, and laying out three socks would change that workflow.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="sock_b",
    label="🧦",
    title="Sock",
    collision="box",
    size=(0.020, 0.020, 0.030),
    density=450,
    condim=6,
    rgba=(0.45, 0.46, 0.50, 1.0),
    rest_z=0.03,
    reach=(0.161, -0.154),
)
