# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Manipulation, Skill, SkillReturn


class ArmZeroPosition(Skill):
    """Use this to move the arm to its zero/home position where all joints are at 0 radians."""

    manipulation: Manipulation

    def execute(self, duration: int = 3) -> SkillReturn:
        self.manipulation.go(self.manipulation.ZERO, duration=duration, logger=self.logger)
        return "Arm moved to zero position"
