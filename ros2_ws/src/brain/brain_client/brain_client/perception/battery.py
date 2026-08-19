# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Always-on battery watch: the newest reading for the agent's turn context,
and the idle-time out-of-battery voice warning.

The skills provider's /battery_state feed only runs while a skill executes,
so both consumers need their own subscription on the main node.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.state.battery import Battery
from brain_client.transport.chat import Sender

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclpy.node import Node
    from sensor_msgs.msg import BatteryState

    from brain_client.transport.chat import ChatManager

# Voltage sags under motor load and recovers at rest; the gap between the two
# thresholds keeps one sag-recover cycle from re-firing the warning.
_WARN_BELOW_VOLTS = 10.6
_REARM_ABOVE_VOLTS = 11.0
_WARNING = "I'm out of battery. Please replace my battery."


class BatteryMonitor:
    def __init__(self, node: Node, chat: ChatManager, brain_is_active: Callable[[], bool]):
        # ROS import deferred so tests can construct this without rclpy.
        from sensor_msgs.msg import BatteryState

        self._chat = chat
        self._brain_is_active = brain_is_active
        self._charging_status: int = BatteryState.POWER_SUPPLY_STATUS_CHARGING
        self._warned = False
        self.current: Battery | None = None
        node.create_subscription(BatteryState, "/battery_state", self._on_battery, 10)

    def _on_battery(self, msg: BatteryState) -> None:
        # A live pack cannot read 0V — the driver reports it (as 0%) until its
        # first I2C health response lands, ~60s into every boot.
        if msg.voltage <= 0.0:
            return
        battery = Battery(
            percentage=msg.percentage,
            voltage=msg.voltage,
            current=msg.current,
            charging=msg.power_supply_status == self._charging_status,
        )
        self.current = battery
        self._maybe_warn(battery)

    def _maybe_warn(self, battery: Battery) -> None:
        """One spoken warning per depletion, only while the brain is idle — an
        active brain sees the level in its turn context and speaks for itself."""
        if battery.voltage >= _REARM_ABOVE_VOLTS or battery.charging:
            self._warned = False
            return
        if battery.voltage >= _WARN_BELOW_VOLTS or self._warned or self._brain_is_active():
            return
        self._warned = True
        self._chat.emit(Sender.ROBOT, _WARNING)
