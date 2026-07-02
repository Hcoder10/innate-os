# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import RobotState, RobotStateType, Skill, SkillFailed, SkillResult
from innate.skills import arm_zero_position, head_emotion, move_straight, pick_socks, turn_in_place


class RunRoutineDemo(Skill):
    """Demo of a chained routine: skills are imported functions, calls block,
    failures raise SkillFailed, and each call is its own step in the app."""

    battery = RobotState(RobotStateType.LAST_BATTERY)

    @property
    def name(self):
        return "run_routine_demo"

    def guidelines(self):
        return (
            "Run the demo routine: talk, emote, shuffle, turn, and try to pick a "
            "sock. Use when the user asks for the demo."
        )

    def execute(self):
        runs = self.storage.get("runs", 0) + 1
        self.storage["runs"] = runs

        arm_zero_position()
        head_emotion(emotion="excited")
        self.say(f"Demo number {runs}. Watch this.", wait=True)

        for distance in (0.2, -0.2):
            move_straight(distance=distance)

        turn = turn_in_place(angle_degrees=90)
        self.say(f"I turned {turn.data.turned_degrees:.0f} degrees.")
        turn_in_place(angle_degrees=-90, timeout=20)

        try:
            pick_socks(timeout=60)  # learned policy, same call shape
        except SkillFailed:
            head_emotion(emotion="disappointed")
            self.say("No socks today.")

        if self.battery:
            self.say(f"Battery at {self.battery['percentage']:.0%}.")
        head_emotion(emotion="proud")
        self.say("All done!")
        return "Demo complete", SkillResult.SUCCESS
