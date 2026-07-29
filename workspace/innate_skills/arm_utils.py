#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Arm Utils Skill - Torque on, torque off, or reboot the arm servos.
"""

from typing import Literal, cast

from innate import Manipulation, Skill, SkillReturn

VALID_COMMANDS = ("torque_on", "torque_off", "reboot_arm")
ArmCommand = Literal["torque_on", "torque_off", "reboot_arm"]


class ArmUtils(Skill):
    """Utility skill for low-level arm commands. Requires 'command' parameter:
    'torque_on', 'torque_off', or 'reboot_arm'. torque_on enables motor torque
    so the arm holds position. torque_off disables torque so the arm goes limp
    (for manual positioning). reboot_arm reboots all Dynamixel servos to clear
    hardware errors."""

    manipulation: Manipulation

    def execute(self, command: ArmCommand) -> SkillReturn:
        command = cast(ArmCommand, command.strip().lower())
        if command not in VALID_COMMANDS:
            self.fail(f"Invalid command '{command}'. Must be one of: {', '.join(VALID_COMMANDS)}.")

        if command == "torque_on":
            if not self.manipulation.torque_on():
                self.fail("Failed to enable arm torque")
            return "Arm torque enabled"

        if command == "torque_off":
            if not self.manipulation.torque_off():
                self.fail("Failed to disable arm torque")
            return "Arm torque disabled (arm is limp)"

        # reboot_arm
        if not self.manipulation.reboot_servos():
            self.fail("Failed to reboot arm servos")
        return "Arm servos rebooted and reinitialized; torque is disabled. Run torque_on before moving."
