#!/usr/bin/env python3
"""
Battery Check Skill - Report the robot's current battery level.

Custom skill: lets agents check charge before starting a long patrol or task,
and phrase warnings to the user when the battery runs low.
"""

import threading

from sensor_msgs.msg import BatteryState

from brain_client.skills.types import Skill, SkillResult


class BatteryCheck(Skill):
    """Read /battery_state once and report percentage and voltage."""

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "battery_check"

    def guidelines(self):
        return (
            "Check the robot's battery level. Takes no parameters. Use before "
            "starting long tasks (patrols, mapping) or when the user asks how "
            "much charge is left."
        )

    def execute(self):
        if self.node is None:
            return "ROS node not available", SkillResult.FAILURE

        received = threading.Event()
        state = {}

        def _on_battery(msg: BatteryState):
            state["percentage"] = msg.percentage
            state["voltage"] = msg.voltage
            received.set()

        # /battery_state publishes at 0.2 Hz, so wait longer than one period
        sub = self.node.create_subscription(BatteryState, "/battery_state", _on_battery, 10)
        try:
            if not received.wait(timeout=12.0):
                return "No battery reading received within 12s", SkillResult.FAILURE
        finally:
            self.node.destroy_subscription(sub)

        percentage = state["percentage"]
        # BatteryState.percentage is 0-1 per ROS convention, but some drivers
        # publish 0-100 directly; normalize for the report.
        if percentage <= 1.0:
            percentage *= 100.0

        msg = f"Battery at {percentage:.0f}% ({state['voltage']:.1f}V)"
        if percentage < 20:
            msg += " - low, consider returning to the charger"

        self.logger.info(f"[BatteryCheck] {msg}")
        return msg, SkillResult.SUCCESS

    def cancel(self):
        return "Battery check cannot be cancelled"
