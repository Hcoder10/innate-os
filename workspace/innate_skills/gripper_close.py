#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Gripper Close — a simple command to close the claw.

Callable from any skill's code as a plain function:

    from innate.skills import gripper_close
    gripper_close()             # close the claw
    gripper_close(strength=0.2) # close harder (firmer grip)

(also from the agent, the webapp skills menu, and
`scripts/innate skill run local/gripper_close`).

Enables arm torque first so the claw still moves after an overload left the
servo torque-disabled.
"""

from brain_client.skills.types import Interface, InterfaceType, Skill, SkillResult


class GripperClose(Skill):
    """Close the gripper (claw)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "gripper_close"

    def guidelines(self):
        return (
            "Close the gripper/claw. strength adds squeeze (radians past the "
            "closed stop) for a firmer grip; default 0.0."
        )

    def execute(self, strength: float = 0.0, duration: float = 1.0):
        """Close the claw. strength = extra radians of squeeze past closed."""
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        self.manipulation.torque_on()
        ok = self.manipulation.close_gripper(
            strength=strength, duration=duration, blocking=True
        )
        if not ok:
            return "Failed to close gripper", SkillResult.FAILURE
        return "Gripper closed", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Gripper motion cancelled"
