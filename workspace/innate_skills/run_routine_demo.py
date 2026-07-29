# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.arm_zero_position import ArmZeroPosition
from innate_skills.head_emotion import HeadEmotion
from innate_skills.move_straight import MoveStraight
from innate_skills.turn_in_place import TurnInPlace

from innate import Battery, Skill, SkillResult, SkillReturn


class RunRoutineDemo(Skill):
    """Run the demo routine: talk, emote, shuffle, turn, and try to pick a
    sock. Use when the user asks for the demo."""

    battery: Battery | None  # nice to have — the demo runs without a reading

    # Sub-skills compose like PyTorch modules: declare the class you want,
    # call the attribute. The declared class is what runs — override by
    # subclassing and re-declaring, not by file naming.
    arm_zero: ArmZeroPosition
    emote: HeadEmotion
    move: MoveStraight
    turn: TurnInPlace

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

        # pick_socks is a trained policy the repo ships only metadata for.
        # Declaring it (PhysicalSkill) makes it a hard dependency checked at
        # wire time, which would fail the whole demo on a robot whose policy
        # is absent or still training — dispatch by id at run time instead
        # and degrade to "no socks", as the routine always has.
        if self.skills is not None:
            _msg, status = self.skills.run("pick_socks", timeout=60)
            if status is SkillResult.CANCELLED:
                return "Demo cancelled", SkillResult.CANCELLED
            if status is not SkillResult.SUCCESS:
                self.emote(emotion="disappointed")
                self.say("No socks today.")

        if self.battery:
            self.say(f"Battery at {self.battery.percentage:.0%}.")
        self.emote(emotion="proud")
        self.say("All done!")
        return "Demo complete"
