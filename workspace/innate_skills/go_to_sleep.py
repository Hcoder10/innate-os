#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Go To Sleep Skill - Put the robot into low-power sleep mode.
"""

import time

from brain_client.skills.types import Skill, SkillResult
from std_srvs.srv import Trigger


class GoToSleep(Skill):
    """Ask the power manager to enter light sleep."""

    def __init__(self, logger):
        super().__init__(logger)
        self._sleep_client = None

    @property
    def name(self):
        return "go_to_sleep"

    def guidelines(self):
        return (
            "Put the robot into low-power sleep mode (lidar, cameras, navigation and "
            "arm power down; the agent goes offline until wake-up). Use when asked to "
            "sleep, rest, nap, or power down. The robot wakes from the app, joystick "
            "or chat input, or a gentle push on its head."
        )

    def execute(self):
        if self.node is None:
            return "Robot node not available", SkillResult.FAILURE

        if self._sleep_client is None:
            self._sleep_client = self.node.create_client(Trigger, "/power/sleep")

        if not self._sleep_client.wait_for_service(timeout_sec=5.0):
            return "Power manager is not available", SkillResult.FAILURE

        # Speak first: the power manager deactivates the brain (and with it TTS)
        # a couple of seconds after accepting the sleep request.
        self.say("Going to sleep. Boop my head or use the app to wake me up.", wait=True)

        future = self._sleep_client.call_async(Trigger.Request())
        deadline = time.time() + 10.0
        while not future.done():
            if time.time() > deadline:
                return "Power manager did not answer in time", SkillResult.FAILURE
            time.sleep(0.05)

        result = future.result()
        if result is None or not result.success:
            reason = getattr(result, "message", "") or "unknown error"
            return f"Could not go to sleep: {reason}", SkillResult.FAILURE

        return "Going to sleep — wake me with a head boop or from the app", SkillResult.SUCCESS

    def cancel(self):
        # The handoff to the power manager is near-instant; nothing to unwind.
        return "Nothing to cancel"
