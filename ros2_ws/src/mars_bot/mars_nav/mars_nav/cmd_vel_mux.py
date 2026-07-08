#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""cmd_vel priority mux — one writer to the base at a time.

Forwards messages from the highest-priority source that has published within
its freshness window:

    /cmd_vel_teleop > /cmd_vel_skills > /cmd_vel_nav  ->  /cmd_vel

Event-driven (no re-timing). One zero twist is published when every source
goes stale so the base stops deterministically.
"""

import time

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node

# (name, topic, freshness window s) — highest priority first. Windows exceed
# each source's publish interval so an active source never flickers stale.
SOURCES = [
    ("teleop", "/cmd_vel_teleop", 0.5),
    ("skills", "/cmd_vel_skills", 0.5),
    ("nav", "/cmd_vel_nav", 0.5),
]


class CmdVelMux(Node):
    def __init__(self):
        super().__init__("cmd_vel_mux")
        self._pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self._last_rx = {name: 0.0 for name, _topic, _window in SOURCES}
        self._forwarding = False  # motion since the last all-stale zero-stop
        for index, (_name, topic, _window) in enumerate(SOURCES):
            self.create_subscription(Twist, topic, self._make_callback(index), 10)
        self.create_timer(0.1, self._stop_when_all_stale)
        self.get_logger().info("cmd_vel mux up: " + " > ".join(f"{name} ({topic})" for name, topic, _w in SOURCES))

    def _make_callback(self, index):
        name = SOURCES[index][0]

        def _on_twist(msg):
            now = time.monotonic()
            self._last_rx[name] = now
            for higher_name, _topic, window in SOURCES[:index]:
                if now - self._last_rx[higher_name] < window:
                    self.get_logger().info(
                        f"'{higher_name}' overriding '{name}' on /cmd_vel",
                        throttle_duration_sec=2.0,
                    )
                    return
            self._pub.publish(msg)
            self._forwarding = True

        return _on_twist

    def _stop_when_all_stale(self):
        """One deterministic zero after the last active source goes quiet."""
        if not self._forwarding:
            return
        now = time.monotonic()
        if any(now - self._last_rx[name] < window for name, _topic, window in SOURCES):
            return
        self._forwarding = False
        self._pub.publish(Twist())
        self.get_logger().debug("all cmd_vel sources stale — published zero stop")


def main(args=None):
    rclpy.init(args=args)
    node = CmdVelMux()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
