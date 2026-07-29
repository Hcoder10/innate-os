#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Gripper Close — a simple command to close the claw.

Callable from any skill's code as a plain function:

    from innate_skills.gripper_close import GripperClose
    gripper_close: GripperClose        # declare, then call self.gripper_close()
    self.gripper_close(strength=0.2)   # close harder (firmer grip)

(also from the agent, the webapp skills menu, and
`scripts/innate skill run gripper_close`).
"""

from innate import Manipulation, Skill, SkillReturn

# Above this squeeze the servo overcurrent-trips on a real object (0.7 and
# 0.8 were both tried on hardware and both tripped; recovery needs a reboot).
MAX_STRENGTH = 0.6


class GripperClose(Skill):
    """Close the gripper/claw. strength adds squeeze (radians past the
    closed stop) for a firmer grip; default 0.0, hardware ceiling 0.6."""

    manipulation: Manipulation

    def execute(self, strength: float = 0.0, duration: float = 1.0) -> SkillReturn:
        # agent/webapp-callable: clamp before anything reaches the servo
        strength = max(0.0, min(strength, MAX_STRENGTH))
        self.manipulation.torque_on()  # a torque-disabled servo won't move at all
        if not self.manipulation.close_gripper(strength=strength, duration=duration, blocking=True):
            self.fail("Failed to close gripper")
        return "Gripper closed"
