# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Manipulation, Skill, SkillReturn


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
        if not self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=z, roll=roll, pitch=pitch, yaw=yaw, duration=duration, blocking=True
        ):
            self.fail("Failed to solve IK or send arm command")
        return f"Arm moved to ({x}, {y}, {z})"
