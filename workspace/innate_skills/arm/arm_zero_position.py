# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import JointStates, Manipulation, Skill, SkillReturn


class ArmZeroPosition(Skill):
    """Use this to move the arm to its zero/home position where all joints are
    at 0 radians. Safe while holding an object: the gripper keeps its current
    closure unless keep_gripper=False."""

    manipulation: Manipulation
    joint_states: JointStates

    def execute(self, duration: int = 3, keep_gripper: bool = True) -> SkillReturn:
        joints = list[float](self.manipulation.ZERO)
        if keep_gripper:
            # j6 zero is *less* closed than a gripping pose, so blindly
            # zeroing it drops whatever the claw holds.
            joints = self.manipulation.with_gripper(joints, self.manipulation.gripper_j6(self.joint_states))
        self.manipulation.go(joints, duration=duration, logger=self.logger)
        return "Arm moved to zero position"
