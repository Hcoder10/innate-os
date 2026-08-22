# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.sign_off import SignOff
from innate_skills.turn_in_place import TurnInPlace

from innate import Skill, SkillReturn


class Goodbye(Skill):
    """Turn away, say goodbye, and perform the sign-off arm gesture."""

    sign_off: SignOff
    turn_in_place: TurnInPlace

    def guidelines(self) -> str:
        return (
            "Use only when ending an interaction and saying goodbye. This skill turns Mars "
            "around, speaks the farewell, and performs the sign-off gesture in the required order."
        )

    def execute(self) -> SkillReturn:
        self.turn_in_place(angle_degrees=180.0, speed=0.5)
        self.say("Goodbye! It was wonderful talking with you. See you next time!")
        self.sign_off(timeout=30.0)
        return "Turned around and signed off while saying goodbye"
