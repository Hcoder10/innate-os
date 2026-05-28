#!/usr/bin/env python3
"""
Open Door Skill - Explicit unavailable stub for directives that still request it.
"""

from brain_client.skill_types import Skill, SkillResult


class OpenDoor(Skill):
    """Report that door opening is not available in this build."""

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "open_door"

    def guidelines(self):
        return (
            "Use when a directive asks to open a door. This build does not yet "
            "include door-opening manipulation, so the skill reports that clearly."
        )

    def execute(self):
        return (
            "Door opening is not available in this simulator/robot build.",
            SkillResult.FAILURE,
        )

    def cancel(self):
        return "Open door cannot be cancelled"
