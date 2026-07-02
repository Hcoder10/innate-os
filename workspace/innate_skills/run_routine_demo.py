# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import RobotState, RobotStateType, Skill, SkillFailed, SkillResult
from innate.skills import arm_zero_position, head_emotion, move_straight, pick_socks, turn_in_place


class RunRoutineDemo(Skill):
    """The tour: what a chained routine can do.

    Skills are imported functions: calls block, failures raise SkillFailed,
    and each call shows up as its own step in the app. The app's Stop button
    just works -- no cancel() override needed.
    """

    battery = RobotState(RobotStateType.LAST_BATTERY)  # live sensor state, 50 Hz

    @property
    def name(self):
        return "run_routine_demo"

    def guidelines(self):
        return (
            "Run the demo routine: talk, emote, shuffle, turn, and try to pick a "
            "sock. Use when the user asks for the demo."
        )

    def execute(self):
        runs = self.storage.get("runs", 0) + 1  # persists across restarts
        self.storage["runs"] = runs

        arm_zero_position()
        head_emotion(emotion="excited")
        self.say(f"Demo number {runs}. Watch this.", wait=True)  # finish speaking, then move

        for distance in (0.2, -0.2):  # loops are just Python
            move_straight(distance=distance)

        turn = turn_in_place(angle_degrees=90)  # skills return typed data (pydantic)
        self.say(f"I turned {turn.data.turned_degrees:.0f} degrees.")
        turn_in_place(angle_degrees=-90, timeout=20)  # cap a step; raises if it blows it

        try:
            pick_socks(timeout=60)  # learned policy, same call shape
        except SkillFailed:
            head_emotion(emotion="disappointed")  # recover instead of aborting
            self.say("No socks today.")

        if self.battery:
            self.say(f"Battery at {self.battery['percentage']:.0%}.")
        head_emotion(emotion="proud")
        self.say("All done!")
        return "Demo complete", SkillResult.SUCCESS
