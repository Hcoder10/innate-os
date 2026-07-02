# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Public authoring namespace for Innate skills.

Everything a skill file needs, under one import:

    from innate import Skill, SkillResult
    from innate.skills import head_emotion, navigate_to_position
"""

from brain_client.skills.types import (
    Interface,
    InterfaceType,
    RobotState,
    RobotStateType,
    Skill,
    SkillResult,
)
from innate.skills import SkillCancelled, SkillFailed

__all__ = [
    "Interface",
    "InterfaceType",
    "RobotState",
    "RobotStateType",
    "Skill",
    "SkillCancelled",
    "SkillFailed",
    "SkillResult",
]
