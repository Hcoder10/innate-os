# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import threading
import time

from innate import Skill, SkillCancelled, SkillFailed, SkillResult
from innate.skills import arm_zero_position, head_emotion, move_straight, pick_socks, turn_in_place


class RunRoutineDemo(Skill):
    """The guided tour: everything a chained routine can do, in one skill.

    1. Sequence      -- skills are imported functions; code skills and learned
                        policies share one call shape, each call is a step in
                        the app, and a failure raises so the routine stops
                        itself (no status checking)
    2. Speech        -- say() is fire-and-forget; say(wait=True) paces
    3. Loops         -- plain Python; every pass is its own step
    4. Data          -- skills can return structured payloads: result.data
    5. timeout=      -- cap a single step without aborting the routine
    6. Recovery      -- try/except SkillFailed where you'd rather adapt
    7. Dynamic ids   -- self.skills.run() for skills chosen at runtime
    8. Cancellation  -- a watchdog caps the whole routine via
                        self.skills.cancel(); the app's Stop button takes the
                        same SkillCancelled path and must win

    Also invisible but on duty: inputs are validated at every call (a typo'd
    kwarg fails with the expected parameter list), and there is no cancel()
    override here -- the base class forwards a Stop to whichever child is
    running.
    """

    @property
    def name(self):
        return "run_routine_demo"

    def guidelines(self):
        return (
            "Run the full demo routine: talk, emote, shuffle, turn, and try to pick "
            "a sock, narrating each capability. Use when the user asks for the demo "
            "or the grand tour."
        )

    def execute(self):
        # (8) Whole-routine watchdog. Child calls block this thread, so a
        # cancel must come from another thread; self.skills.cancel() stops
        # whichever child is running and the blocked call raises SkillCancelled.
        stopped_by_watchdog = threading.Event()

        def stop_routine():
            stopped_by_watchdog.set()
            self.skills.cancel()

        watchdog = threading.Timer(120.0, stop_routine)
        watchdog.start()
        try:
            turned = self._tour()
        except SkillCancelled:
            # The app's Stop raises this same exception; only handle our own
            # watchdog and let a real Stop propagate to the server.
            if not stopped_by_watchdog.is_set():
                raise
            self.say("Out of time, stopping the demo!")
            return "Demo stopped by its 2-minute watchdog", SkillResult.CANCELLED
        finally:
            watchdog.cancel()  # finished in time -- disarm

        head_emotion(emotion="proud")
        self.say("Tour complete!")
        # (4) the routine itself returns data too, for whoever chains *it*
        return "Demo complete", SkillResult.SUCCESS, {"turned_degrees": turned}

    def _tour(self):
        # (1) A sequence is just consecutive calls.
        self.say("Starting the grand tour!")  # (2) speech overlaps the motion
        arm_zero_position()
        head_emotion(emotion="excited")
        self.say("I finish this sentence before moving.", wait=True)  # (2) paced

        # (3) Loops are plain Python: forward then back, two steps in the app.
        for distance in (0.2, -0.2):
            move_straight(distance=distance)

        # (4) Structured results: .data carries numbers, not prose to parse.
        result = turn_in_place(angle_degrees=90)
        turned = result.data["turned_degrees"]
        self.say(f"My odometry says I turned {turned:.0f} degrees.")

        # (5) timeout= caps this one step: on expiry the child is cancelled
        # and raises SkillFailed HERE, while the routine keeps going --
        # which is (6): catch it where you'd rather adapt than abort.
        try:
            turn_in_place(angle_degrees=-90, timeout=20)
        except SkillFailed:
            head_emotion(emotion="confused")
            self.say("That turn had trouble. Moving on.")

        # (1)+(5)+(6) together: a learned policy, same call shape, same safety.
        try:
            pick_socks(timeout=60)
        except SkillFailed:
            head_emotion(emotion="disappointed")
            self.say("No socks for me today.")

        # (7) Dynamic dispatch: pick the skill (or its inputs) at runtime with
        # the explicit invoker; run() returns (message, status) instead of raising.
        evening = time.localtime().tm_hour >= 18
        self.skills.run("innate-os/head_emotion", emotion="sleepy" if evening else "happy")

        return turned
