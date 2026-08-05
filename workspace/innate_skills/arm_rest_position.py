# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import JointStates, Manipulation, Skill, SkillReturn


class ArmRestPosition(Skill):
    """Use this to move the arm to its resting position (folded against the
    body, servos unloaded). Safe while holding an object: the gripper keeps
    its current closure unless keep_gripper=False."""

    manipulation: Manipulation
    joint_states: JointStates

    def execute(self, duration: int = 3, keep_gripper: bool = True) -> SkillReturn:
        self.manipulation.go(
            self.manipulation.rest_joints(self.joint_states, keep_gripper),
            duration=duration,
            logger=self.logger,
        )
        return "Arm moved to rest position"
