# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math

from innate import ArmFailed, ArmUnhealthy, Manipulation, Skill, SkillReturn, Waypoint


class ArmCircleMotion(Skill):
    """Move the arm in a circular motion pattern. The circle is traced in the YZ
    plane (vertical) while maintaining a constant X position. You can specify the
    center position, radius, number of loops, and speed. A good default center
    position is x=0.3, y=-0.05, z=0.2 (roughly in front of the robot with arm
    extended)."""

    manipulation: Manipulation

    def execute(
        self,
        center_x: float = 0.3,
        center_y: float = -0.05,
        center_z: float = 0.2,
        radius: float = 0.1,
        num_loops: int = 1,
        points_per_loop: int = 16,
        duration_per_point: float = 0.5,
    ) -> SkillReturn:
        try:
            roll, pitch, yaw = self.manipulation.pose.rpy
        except ArmFailed:
            self.logger.warning("Could not get current orientation, using defaults")
            roll, pitch, yaw = 0.0, 0.0, 0.0

        self.logger.info(
            f"Circular motion: center=({center_x}, {center_y}, {center_z}), radius={radius}m, loops={num_loops}"
        )
        # One smooth trajectory per loop, starting and ending at the top of
        # the circle — no per-point stop-and-go.
        circle = [
            Waypoint(
                center_x,
                center_y + radius * math.sin(2 * math.pi * i / points_per_loop),
                center_z + radius * math.cos(2 * math.pi * i / points_per_loop),
                duration=duration_per_point,
            )
            for i in range(1, points_per_loop + 1)
        ]
        try:
            self.manipulation.follow(
                [Waypoint(center_x, center_y, center_z + radius, roll=roll, pitch=pitch, yaw=yaw, duration=1.0)]
            )
            for loop in range(num_loops):
                self.logger.info(f"Starting loop {loop + 1}/{num_loops}")
                # Non-blocking + cancellable poll: a Stop lands within 0.1 s
                # instead of after the full ~8 s loop (the in-flight loop
                # still finishes — the goto services cannot preempt).
                self.manipulation.follow(circle, block=False)
                while self.manipulation.moving:
                    self.sleep(0.1)
                self.manipulation.wait()
        except (ArmFailed, ArmUnhealthy) as e:
            self.fail(f"Circular motion failed: {e}")

        return f"Completed {num_loops} circular loop(s)"
