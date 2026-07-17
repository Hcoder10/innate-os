#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Gripper Open — a simple command to open the claw.

Callable from any skill's code as a plain function:

    from innate.skills import gripper_open
    gripper_open()      # fully open the claw

(also from the agent, the webapp skills menu, and
`scripts/innate skill run gripper_open`).

The implementation lives in workspace/skill_lib/arm.py (open_checked):
torque on first, then VERIFY the claw actually opened — an overcurrent-
tripped servo no-ops silently — rebooting to clear a trip and retrying.
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


class GripperOpen(Skill):
    """Open the gripper (claw)."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    joint_states = RobotState(RobotStateType.LAST_JOINT_STATES)

    @property
    def name(self):
        return "gripper_open"

    def guidelines(self):
        return "Open the gripper/claw. percent=100 (default) is fully open."

    def execute(self, percent: float = 100.0, duration: float = 1.0):
        """Open the claw. percent 0-100 (default fully open)."""
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        ok = armlib.open_checked(
            self.manipulation,
            lambda: armlib.gripper_j6(self.joint_states),
            percent=percent,
            duration=duration,
            logger=self.logger,
        )
        if not ok:
            return "Failed to open gripper", SkillResult.FAILURE
        return "Gripper opened", SkillResult.SUCCESS

    def cancel(self):
        return "Gripper motion cancelled"
