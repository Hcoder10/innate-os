#!/usr/bin/env python3
"""
Look Around Skill - Rotate in place and report what the camera sees at each stop.

Custom skill: useful for patrol/monitoring agents that need a quick 360° sweep
of the room without leaving their current spot.
"""

import time

from brain_client.skills.types import Interface, InterfaceType, RobotState, RobotStateType, Skill, SkillResult


class LookAround(Skill):
    """Rotate in place in steps, sending a camera snapshot as feedback at each stop."""

    mobility = Interface(InterfaceType.MOBILITY)
    image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "look_around"

    def guidelines(self):
        return (
            "Do a sweep of the room by rotating in place and capturing camera views. "
            "Optional 'steps' parameter (int, 2-8, default 4) sets how many stops "
            "the full 360° turn is divided into. Use when you need to check the "
            "surroundings or find something/someone nearby without navigating."
        )

    def execute(self, steps: int = 4):
        """
        Rotate 360° in `steps` increments, pausing at each to look.

        Args:
            steps: Number of stops in the full turn (default 4, i.e. every 90°).
        """
        self._cancelled = False

        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE

        steps = max(2, min(int(steps), 8))
        step_duration = 2.0  # seconds of rotation per step
        # A full turn split into `steps` timed rotations at a gentle speed.
        angular_speed = (2 * 3.14159) / (steps * step_duration)

        self.logger.info(f"[LookAround] Sweeping the room in {steps} steps")
        self._send_feedback(f"Starting a {steps}-step sweep of the room")

        for i in range(steps):
            if self._cancelled:
                self.mobility.send_cmd_vel(0.0, 0.0)
                return "Look around cancelled", SkillResult.CANCELLED

            self.mobility.rotate_in_place(angular_speed, step_duration)
            time.sleep(step_duration + 0.6)  # rotation + settle time

            heading = int(360 * (i + 1) / steps)
            if self.image:
                self._send_feedback(f"View at {heading}° of the sweep", self.image)
            else:
                self._send_feedback(f"At {heading}° of the sweep (no camera frame available)")

        return f"Completed {steps}-step sweep of the surroundings", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        if self.mobility is not None:
            self.mobility.send_cmd_vel(0.0, 0.0)
        return "Look around cancelled"
