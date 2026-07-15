#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Gripper Open — a simple command to open the claw.

Callable from any skill's code as a plain function:

    from innate.skills import gripper_open
    gripper_open()      # fully open the claw

(also from the agent, the webapp skills menu, and
`scripts/innate skill run local/gripper_open`).

Enables arm torque first. A hard close that overloads the servo leaves its
torque disabled, after which a plain open silently does nothing — that's why
opening from a resting closed claw failed. Enabling torque first makes it move.
"""

from brain_client.skills.types import Interface, InterfaceType, Skill, SkillResult


class GripperOpen(Skill):
    """Open the gripper (claw)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "gripper_open"

    def guidelines(self):
        return "Open the gripper/claw. percent=100 (default) is fully open."

    def execute(self, percent: float = 100.0, duration: float = 1.0):
        """Open the claw. percent 0-100 (default fully open)."""
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        self.manipulation.torque_on()  # a torque-disabled servo won't move
        ok = self.manipulation.open_gripper(
            percent=percent, duration=duration, blocking=True
        )
        if not ok:
            return "Failed to open gripper", SkillResult.FAILURE
        return "Gripper opened", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Gripper motion cancelled"
