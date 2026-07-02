# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import time

from innate import Skill, SkillResult
from innate.skills import arm_zero_position, head_emotion, move_straight, turn_in_place


class RunRoutineDemo(Skill):
    """Example: chain skills with plain Python -- sequence, loops, recovery.

    Skills imported from innate.skills run like functions: each call runs that
    skill to completion (a learned policy, a classic move, a replay -- same
    shape) and shows up as its own step in the app. A successful call returns
    the skill's output message; a failed or cancelled one raises, so a straight
    sequence -- or a loop -- stops at the first problem with no status
    checking. Catch SkillFailed where you'd rather recover than stop.
    Cancellation is handled by the Skill base class.
    """

    @property
    def name(self):
        return "run_routine_demo"

    def guidelines(self):
        return (
            "Run the demo routine: shuffle forward and back, turn left and right, "
            "then celebrate. Use when the user asks to run the demo routine."
        )

    def execute(self):
        arm_zero_position()
        self.say("Starting the demo routine!")
        head_emotion(emotion="excited")
        self.say("I will now move forward!")
        time.sleep(1)
        move_straight(distance=0.2)
        self.say("I will now move backward!")
        time.sleep(1)
        move_straight(distance=-0.2)
        self.say("Now turning left!")
        time.sleep(1)
        turn_in_place(angle_degrees=90)
        self.say("And back to the right!")
        time.sleep(1)
        turn_in_place(angle_degrees=-90)

        head_emotion(emotion="proud")
        self.say("All done!")
        return "Routine complete", SkillResult.SUCCESS
