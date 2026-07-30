# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math

from innate import Manipulation, Skill, SkillReturn


class ArmCircleMotion(Skill):
    """Move the arm in a circular motion pattern. The circle is traced in the YZ
    plane (vertical) while maintaining a constant X position. You can specify the
    center position, radius, number of loops, and speed. A good default center
    position is x=0.2, y=-0.05, z=0.2 (roughly in front of the robot with arm
    extended)."""

    manipulation: Manipulation

    def execute(
        self,
        center_x: float = 0.2,
        center_y: float = -0.05,
        center_z: float = 0.2,
        radius: float = 0.1,
        num_loops: int = 1,
        points_per_loop: int = 16,
        duration_per_point: float = 0.5,
    ) -> SkillReturn:
        orientation = self.manipulation.get_current_orientation_rpy()
        if orientation is None:
            self.logger.warning("Could not get current orientation, using defaults")
            roll, pitch, yaw = 0.0, 0.0, 0.0
        else:
            roll, pitch, yaw = orientation["roll"], orientation["pitch"], orientation["yaw"]

        self.logger.info(
            f"Circular motion: center=({center_x}, {center_y}, {center_z}), radius={radius}m, loops={num_loops}"
        )
        if not self.manipulation.move_to_cartesian_pose(
            x=center_x, y=center_y, z=center_z + radius, roll=roll, pitch=pitch, yaw=yaw, duration=1.0
        ):
            self.fail("Failed to move to start position")
        self.sleep(1.2)

        for loop in range(num_loops):
            self.logger.info(f"Starting loop {loop + 1}/{num_loops}")
            for i in range(points_per_loop):
                angle = (2 * math.pi * i) / points_per_loop
                target_y = center_y + radius * math.sin(angle)
                target_z = center_z + radius * math.cos(angle)
                if not self.manipulation.move_to_cartesian_pose(
                    x=center_x, y=target_y, z=target_z, roll=0.0, pitch=0.0, yaw=0.0, duration=duration_per_point
                ):
                    self.logger.warning(f"IK failed at point {i + 1}, skipping")
                    continue
                self.sleep(duration_per_point)

        return f"Completed {num_loops} circular loop(s)"
