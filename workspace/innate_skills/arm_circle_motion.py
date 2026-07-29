#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Arm Circle Motion Skill - Move arm in a circular pattern.
"""

import math
import time

from innate import Manipulation, Skill, SkillResult, SkillReturn


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
        # Get current orientation to maintain during circle motion
        current_orientation = self.manipulation.get_current_orientation_rpy()
        if current_orientation is None:
            self.logger.warning("Could not get current orientation, using defaults")
            roll, pitch, yaw = 0.0, 0.0, 0.0
        else:
            roll, pitch, yaw = current_orientation["roll"], current_orientation["pitch"], current_orientation["yaw"]
            self.logger.info(
                f"Current orientation: roll={math.degrees(roll):.1f}°, pitch={math.degrees(pitch):.1f}°, yaw={math.degrees(yaw):.1f}°"
            )

        total_points = num_loops * points_per_loop
        self.logger.info(
            f"Starting circular motion: center=({center_x}, {center_y}, {center_z}), "
            f"radius={radius}m, loops={num_loops}, points={total_points}"
        )

        # First move to starting position (top of circle: center_y, center_z + radius)
        start_y = center_y
        start_z = center_z + radius

        self.logger.info(f"Moving to start position: ({center_x}, {start_y}, {start_z})")
        success = self.manipulation.move_to_cartesian_pose(
            x=center_x, y=start_y, z=start_z, roll=roll, pitch=pitch, yaw=yaw, duration=1.0
        )

        if not success:
            self.fail("Failed to move to start position")

        # Wait for initial move
        time.sleep(1.2)

        if self.cancelled:
            return "Circle motion cancelled", SkillResult.CANCELLED

        # Trace the circle(s)
        for loop in range(num_loops):
            self.logger.info(f"Starting loop {loop + 1}/{num_loops}")

            for i in range(points_per_loop):
                if self.cancelled:
                    return "Circle motion cancelled", SkillResult.CANCELLED

                # Calculate angle for this point (start from top, go clockwise)
                # Angle 0 = top (positive Y direction from center)
                angle = (2 * math.pi * i) / points_per_loop

                # Calculate YZ position on circle (X stays constant)
                # Using standard circle parametrization, rotated so 0 starts at top
                target_y = center_y + radius * math.sin(angle)
                target_z = center_z + radius * math.cos(angle)

                self.logger.debug(
                    f"Point {i + 1}/{points_per_loop}: angle={math.degrees(angle):.1f}°, "
                    f"pos=({center_x:.3f}, {target_y:.3f}, {target_z:.3f})"
                )

                success = self.manipulation.move_to_cartesian_pose(
                    x=center_x, y=target_y, z=target_z, roll=0.0, pitch=0.0, yaw=0.0, duration=duration_per_point
                )

                if not success:
                    self.logger.warning(f"IK failed at point {i + 1}, skipping")
                    continue

                # Wait for motion
                time.sleep(duration_per_point)

        self.logger.info("Circle motion completed successfully")
        return f"Completed {num_loops} circular loop(s)"
