#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Arm Zero Position Skill — move arm to all-zeros joint position.

Implementation: workspace/skill_lib/arm.py (zero).
"""

from brain_client.skills.types import Interface, InterfaceType, Skill, SkillResult
from workspace.skill_lib import arm as armlib


class ArmZeroPosition(Skill):
    """Move the arm to the zero position (all joints at 0 radians)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "arm_zero_position"

    def guidelines(self):
        return "Use this to move the arm to its zero/home position where all joints are at 0 radians."

    def execute(self, duration: int = 3):
        """Execute the arm movement to zero position."""
        self._cancelled = False
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        try:
            armlib.zero(
                self.manipulation, duration=duration,
                is_cancelled=lambda: self._cancelled, logger=self.logger,
            )
        except armlib.ArmCancelled:
            return "Arm motion cancelled", SkillResult.CANCELLED
        except armlib.ArmFailed:
            return "Failed to send arm command", SkillResult.FAILURE
        return "Arm moved to zero position", SkillResult.SUCCESS

    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
