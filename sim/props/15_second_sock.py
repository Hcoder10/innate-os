"""Second sock: a visually identical twin of `sock`, for the verification probes."""

from mars_sim_driver.props import Prop

# Every number is copied from 12_sock.py deliberately. pick_any_object's
# _grasp_verified asks Gemini whether "the sock" is lying loose on the floor
# after the grasp; a second, indistinguishable sock in that frame is the exact
# input that turns a CORRECT answer about the wrong object into a missed-grasp
# verdict. Deliberately outside the `manipulation` group: laying out the
# practice set must not start dropping two socks.
PROP = Prop(
    name="second_sock",
    label="🧦",
    title="Sock",
    collision="box",
    size=(0.020, 0.020, 0.030),
    density=450,
    condim=6,
    rgba=(0.45, 0.46, 0.50, 1.0),
    rest_z=0.03,
    # Reach y mirrored from the sock's so the pair lands side by side, not
    # stacked, if anything ever places both at the robot.
    reach=(0.161, -0.154),
)
