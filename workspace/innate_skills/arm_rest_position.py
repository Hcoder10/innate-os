#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Arm Rest Position Skill — move the arm to its resting pose.

Callable from any skill's code as a plain function:

    from innate_skills.arm_rest_position import ArmRestPosition
    arm_rest: ArmRestPosition   # declare, then call self.arm_rest()

(also runnable from the agent, the webapp skills menu, and
`scripts/innate skill run arm_rest_position`).
"""

from brain_client.robot.manipulation import ArmCancelled, ArmFailed
from innate import JointStates, Manipulation, Skill, SkillResult, SkillReturn


class ArmRestPosition(Skill):
    """Use this to move the arm to its resting position (folded against the
    body, servos unloaded). Safe while holding an object: the gripper keeps
    its current closure unless keep_gripper=False."""

    manipulation: Manipulation
    joint_states: JointStates  # required: guaranteed before execute() starts

    def execute(self, duration: int = 3, keep_gripper: bool = True) -> SkillReturn:
        try:
            self.manipulation.go(
                self.manipulation.rest_joints(self.joint_states, keep_gripper),
                duration=duration,
                is_cancelled=lambda: self.cancelled,
                logger=self.logger,
            )
        except ArmCancelled:
            return "Arm motion cancelled", SkillResult.CANCELLED
        except ArmFailed:
            self.fail("Failed to send arm command")
        return "Arm moved to rest position"
