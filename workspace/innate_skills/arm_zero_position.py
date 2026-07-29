#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Arm Zero Position Skill — move arm to all-zeros joint position.

Callable from any skill's code as a plain function:

    from innate_skills.arm_zero_position import ArmZeroPosition
    arm_zero: ArmZeroPosition   # declare, then call self.arm_zero()
"""

from brain_client.robot.manipulation import ArmCancelled, ArmFailed
from innate import Manipulation, Skill, SkillResult, SkillReturn


class ArmZeroPosition(Skill):
    """Use this to move the arm to its zero/home position where all joints are at 0 radians."""

    manipulation: Manipulation

    def execute(self, duration: int = 3) -> SkillReturn:
        try:
            self.manipulation.go(
                self.manipulation.ZERO,
                duration=duration,
                is_cancelled=lambda: self.cancelled,
                logger=self.logger,
            )
        except ArmCancelled:
            return "Arm motion cancelled", SkillResult.CANCELLED
        except ArmFailed:
            self.fail("Failed to send arm command")
        return "Arm moved to zero position"
