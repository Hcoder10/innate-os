# SPDX-License-Identifier: Apache-2.0
"""LiDAR scan health: detects when laser scans stop arriving (e.g. lidar unplugged).

Keeps an always-on subscription to the scan topic and records when the last
message arrived. The orchestrator polls :meth:`stale_problem` from the agent loop to
surface a clear error instead of failing silently when the lidar is
disconnected (no scans -> AMCL never localizes -> no pose -> agent skips every
loop with nothing reaching the app).
"""

from __future__ import annotations

import time

from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan

# Scans arrive at ~6 Hz; this long without one means the lidar is gone, not slow.
SCAN_STALE_AFTER_SEC = 10.0


class ScanHealthMonitor:
    def __init__(self, node, *, scan_topic: str, stale_after_sec: float = SCAN_STALE_AFTER_SEC):
        self._stale_after_sec = stale_after_sec
        self._started_at = time.monotonic()
        self._last_scan_at: float | None = None
        # Sensor-data QoS (best effort) matches the lidar driver and also
        # accepts reliable publishers (e.g. the throttle relay).
        self._scan_sub = node.create_subscription(LaserScan, scan_topic, self._on_scan, qos_profile_sensor_data)

    def _on_scan(self, _msg: LaserScan) -> None:
        self._last_scan_at = time.monotonic()

    def stale_problem(self) -> str | None:
        """Describe why scans are stale, or None when healthy.

        Reads ``_last_scan_at`` once so the staleness check and the message
        agree even if a scan arrives mid-call (the subscription callback runs
        on another executor thread).
        """
        last_scan_at = self._last_scan_at
        reference = last_scan_at if last_scan_at is not None else self._started_at
        elapsed = time.monotonic() - reference
        if elapsed <= self._stale_after_sec:
            return None
        if last_scan_at is None:
            return "no laser scan data has been received since startup"
        return f"laser scan data stopped {elapsed:.0f}s ago"
