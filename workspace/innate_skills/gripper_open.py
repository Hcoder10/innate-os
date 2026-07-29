#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Gripper Open — a simple command to open the claw.

Callable from any skill's code as a plain function:

    from innate_skills.gripper_open import GripperOpen
    gripper_open: GripperOpen   # declare, then call self.gripper_open()

(also from the agent, the webapp skills menu, and
`scripts/innate skill run gripper_open`).
"""

from innate import JointStates, Manipulation, Skill, SkillReturn

# Below this j6 the claw is still (tripped) shut after an open command.
GRIPPER_SHUT_J6 = 0.10


class GripperOpen(Skill):
    """Open the gripper/claw. percent=100 (default) is fully open."""

    manipulation: Manipulation
    joint_states: JointStates  # required: guaranteed before execute() starts

    def execute(self, percent: float = 100.0, duration: float = 1.0) -> SkillReturn:
        # agent/webapp-callable: clamp before anything reaches the servo
        percent = max(0.0, min(percent, 100.0))
        self.manipulation.torque_on()  # a torque-disabled servo won't move at all
        ok = self.manipulation.open_gripper(percent=percent, duration=duration, blocking=True)
        j6 = self.manipulation.gripper_j6(self.joint_states)
        if j6 is not None and j6 < GRIPPER_SHUT_J6:
            self.logger.warning(
                f"[gripper] did not open (j6={j6:.3f}); rebooting servos to clear a trip, then retrying"
            )
            self.manipulation.recover(self.logger)
            ok = self.manipulation.open_gripper(percent=percent, duration=duration, blocking=True)
            j6 = self.manipulation.gripper_j6(self.joint_states)
            if j6 is not None and j6 < GRIPPER_SHUT_J6:
                self.fail("Gripper did not open (servo tripped shut)")
        if not ok and j6 is None:
            self.fail("Failed to open gripper")
        return "Gripper opened"
