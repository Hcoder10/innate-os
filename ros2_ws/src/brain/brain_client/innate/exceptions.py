# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Every exception a skill raises or catches, in one place.

``SkillFailed`` / ``SkillCancelled`` are the run lifecycle; ``ArmFailed`` /
``ArmUnhealthy`` are what Manipulation's motion methods raise. All are also
re-exported at the top level (``from innate import ArmFailed``).
"""

from brain_client.robot.exceptions import ArmFailed, ArmUnhealthy
from brain_client.skills.types import SkillCancelled, SkillFailed

__all__ = ["ArmFailed", "ArmUnhealthy", "SkillCancelled", "SkillFailed"]
