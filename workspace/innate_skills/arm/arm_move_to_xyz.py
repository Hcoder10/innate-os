# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import ArmFailed, ArmUnhealthy, Manipulation, Skill, SkillReturn


class ArmMoveToXYZ(Skill):
    """Move the arm end-effector to a target position in Cartesian space (x, y, z
    in meters). Coordinates are relative to the robot base_link. If x, y, z are
    omitted the arm moves to its home/resting pose. Optionally specify roll,
    pitch, yaw orientation in radians."""

    manipulation: Manipulation

    def execute(
        self,
        x: float = 0.15,
        y: float = 0.1,
        z: float = 0.1,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        duration: int = 3,
    ) -> SkillReturn:
        self.logger.info(f"Moving arm to XYZ ({x}, {y}, {z}) with RPY ({roll}, {pitch}, {yaw}) over {duration}s")
        try:
            # Unverified move (tol=None): arbitrary agent-chosen targets may
            # legitimately settle off-pose near joint limits.
            self.manipulation.move_to(
                x, y, z, roll=roll, pitch=pitch, yaw=yaw, duration=duration, tol_xy=None, tol_z=None
            )
        except (ArmFailed, ArmUnhealthy) as e:
            self.fail(f"Failed to move arm: {e}")
        return f"Arm moved to ({x}, {y}, {z})"
