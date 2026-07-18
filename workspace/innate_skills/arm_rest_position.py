#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Arm Rest Position Skill — move the arm to its resting pose.

From skill code, call the library function directly instead of this skill:

    from workspace.skill_lib import arm as armlib
    armlib.rest(self.manipulation, self.joint_states)

This class is the door for the agent, the webapp skills menu, and
`scripts/innate skill run local/arm_rest_position`.
"""

from brain_client.skills.types import (
    Interface,
    InterfaceType,
    RobotState,
    RobotStateType,
    Skill,
    SkillResult,
)
from workspace.skill_lib import arm as armlib


class ArmRestPosition(Skill):
    """Move the arm to the resting position."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    joint_states = RobotState(RobotStateType.LAST_JOINT_STATES)

    @property
    def name(self):
        return "arm_rest_position"

    def guidelines(self):
        return (
            "Use this to move the arm to its resting position (folded against "
            "the body, servos unloaded). Safe while holding an object: the "
            "gripper keeps its current closure unless keep_gripper=False."
        )

    def execute(self, duration: int = 3, keep_gripper: bool = True):
        """
        Move the arm to the resting pose.

        Args:
            duration: Trajectory duration in seconds.
            keep_gripper: Keep the gripper's current closure (default) so a
                held object isn't released; False also restores the captured
                rest gripper value.
        """
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        self.logger.info(f"Moving arm to rest position over {duration}s")
        ok = armlib.rest(
            self.manipulation,
            self.joint_states,
            duration=duration,
            keep_gripper=keep_gripper,
            cancelled=lambda: self._cancelled,
        )
        if self._cancelled:
            return "Arm motion cancelled", SkillResult.CANCELLED
        if not ok:
            return "Failed to send arm command", SkillResult.FAILURE
        return "Arm moved to rest position", SkillResult.SUCCESS

    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
