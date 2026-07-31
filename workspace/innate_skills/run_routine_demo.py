# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.arm.arm_zero_position import ArmZeroPosition
from innate_skills.head_emotion import HeadEmotion
from innate_skills.move_straight import MoveStraight
from innate_skills.pick_socks import PickSocks
from innate_skills.turn_in_place import TurnInPlace

from innate import Battery, Skill, SkillFailed, SkillReturn


class RunRoutineDemo(Skill):
    """Run the demo routine: talk, emote, shuffle, turn, and try to pick a
    sock. Use when the user asks for the demo."""

    battery: Battery | None

    arm_zero: ArmZeroPosition
    emote: HeadEmotion
    move: MoveStraight
    turn: TurnInPlace
    pick: PickSocks

    def execute(self) -> SkillReturn:
        runs = self.storage.get("runs", 0) + 1
        self.storage["runs"] = runs

        self.arm_zero()
        self.emote(emotion="excited")
        self.say(f"Demo number {runs}. Watch this.", wait=True)

        for distance in (0.2, -0.2):
            self.move(distance=distance)

        turn = self.turn(angle_degrees=90)
        self.say(f"I turned {turn.data.turned_degrees:.0f} degrees.")
        self.turn(angle_degrees=-90)

        # pick_socks is a learned policy; the typed ref checks it exists at
        # run start, so only a failed grasp lands here — not a missing skill.
        try:
            self.pick(timeout=60)
        except SkillFailed:
            self.emote(emotion="disappointed")
            self.say("No socks today.")

        if self.battery:
            self.say(f"Battery at {self.battery.percentage:.0%}.")
        self.emote(emotion="proud")
        self.say("All done!")
        return "Demo complete"
