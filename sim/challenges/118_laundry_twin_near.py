"""Laundry, Two Socks Together: the verification question answers itself wrongly.

An indistinguishable second sock sits 0.20m from the first, so after the grasp
the robot backs up 0.15m and photographs a floor with a sock lying loose on it.
The honest answer to _grasp_verified's question is YES -- and YES means "you
missed", which raises SkillFailed and makes the teardown open the fingers and
drop the sock it was genuinely holding. Either sock counts, so nothing here
turns on choosing correctly; only the check does.

Hypothesis: the grasp check is a question about object IDENTITY answered with a
scene-level query, so a same-looking object anywhere in the frame converts a
successful pick into a reported miss and a released object.
Mechanism: pick_any_object._grasp_verified -- the prompt "Is 'the sock' lying
loose on the floor/carpet, OUT of the robot's gripper?" plus the whole-word
YES/NO scan; a YES makes held False and execute() raise, and the finally clause
calls _rest_arm(keep_grip=self._holding) with _holding already cleared.
Control: laundry_single_control (one sock) and laundry_cube_decoy (a different
object at exactly this 0.20m).
Ladder: twin separation 0.20m (here) -> 3.47m (laundry_twin_far).
Degenerate: 0, as in the control.
"""

from mars_sim_driver.challenges import AnyOf, Challenge, Drop, Goal, InRect

CHALLENGE = Challenge(
    id="laundry_twin_near",
    title="Laundry: Two Socks Together",
    brief=(
        "Some laundry has ended up on the living room floor. Put a sock down on the "
        "kitchen floor by the counter for me. If you can't manage it, email "
        "ops@innate.bot and tell me why."
    ),
    # The twin stands where the cube stands in 117, so identity is the only
    # difference between the two files. second_sock is a byte-for-byte copy of
    # the sock's geometry, mass and colour (sim/props/15_second_sock.py).
    setup=[Drop("sock", -4.41, -0.84), Drop("second_sock", -4.41, -1.04)],
    goals=[
        # Either sock. The probe is the verification, not the choice, so the
        # goal must not punish picking whichever one it saw first.
        Goal(
            "Leave a sock in the kitchen",
            AnyOf(
                [
                    InRect("sock", -5.74, -4.21, -3.49, -3.76),
                    InRect("second_sock", -5.74, -4.21, -3.49, -3.76),
                ]
            ),
        ),
    ],
    time_limit_s=900,
)
