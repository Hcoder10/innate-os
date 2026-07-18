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
`scripts/innate skill run gripper_close`).
"""

from brain_client.skills.types import Interface, InterfaceType, Skill, SkillResult
from workspace.skill_lib import arm as armlib


class GripperClose(Skill):
    """Close the gripper (claw)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    @property
    def name(self):
        return "gripper_close"

    def guidelines(self):
        return (
            "Close the gripper/claw. strength adds squeeze (radians past the "
            "closed stop) for a firmer grip; default 0.0. Up to ~0.8 holds "
            "well on a real object — watch for overcurrent servo trips above."
        )

    def execute(self, strength: float = 0.0, duration: float = 1.0):
        """Close the claw. strength = extra radians of squeeze past closed."""
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        if not armlib.close(self.manipulation, strength=strength, duration=duration):
            return "Failed to close gripper", SkillResult.FAILURE
        return "Gripper closed", SkillResult.SUCCESS

    def cancel(self):
        return "Gripper motion cancelled"
