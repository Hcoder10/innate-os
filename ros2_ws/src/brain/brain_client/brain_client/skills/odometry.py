# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing odometry state.

MARS is a differential-drive base on flat ground, so its pose is fully
described by (x, y, yaw) — skills get that directly instead of the raw ROS
Odometry message with its quaternion orientation. ROS-free on purpose: it is
part of the public `innate` authoring namespace and of the no-ROS test bucket.
"""

import math
import warnings
from dataclasses import dataclass


@dataclass(frozen=True)
class Odometry:
    """A 2D odometry snapshot: pose in the odom frame plus body velocities.

    Injected for ``RobotState(RobotStateType.LAST_ODOM)`` and refreshed at
    50 Hz while a skill runs.
    """

    x: float
    """Position along X in meters, odom frame."""
    y: float
    """Position along Y in meters, odom frame."""
    theta: float
    """Yaw in radians, counter-clockwise positive, wrapped to (-pi, pi]."""
    linear_velocity: float = 0.0
    """Forward speed in m/s (negative when driving backward)."""
    angular_velocity: float = 0.0
    """Turn rate in rad/s, counter-clockwise positive."""
    stamp: float = 0.0
    """Sensor timestamp in seconds (ROS time)."""
    frame_id: str = "odom"
    child_frame_id: str = "base_link"
    raw: dict | None = None
    """Escape hatch: the full nav_msgs/Odometry as plain data (rosbridge-style
    keys) — real quaternion, z, covariances, full twist — for skills that need
    more than the flat 2D pose."""

    @property
    def theta_degrees(self) -> float:
        """Yaw in degrees, counter-clockwise positive."""
        return math.degrees(self.theta)

    @property
    def position(self) -> tuple[float, float]:
        """(x, y) in meters, odom frame."""
        return (self.x, self.y)

    def __getitem__(self, key):
        """Deprecated dict-style access matching the raw-message dict that
        LAST_ODOM used to inject (releases up to 0.6.x)."""
        warnings.warn(
            "dict-style odometry access is deprecated; use the Odometry "
            "attributes instead (odom.x, odom.theta_degrees, ...) or odom.raw "
            "for the full message",
            DeprecationWarning,
            stacklevel=2,
        )
        if key == "theta_degrees":
            return self.theta_degrees
        return (self.raw if self.raw is not None else self._reconstructed_raw())[key]

    def _reconstructed_raw(self) -> dict:
        """Legacy-shape fallback for instances built without ``raw``
        (hand-constructed in tests); the quaternion carries yaw only."""
        sec = int(self.stamp)
        half = self.theta / 2.0
        return {
            "header": {
                "stamp": {"sec": sec, "nanosec": int(round((self.stamp - sec) * 1e9))},
                "frame_id": self.frame_id,
            },
            "child_frame_id": self.child_frame_id,
            "pose": {
                "pose": {
                    "position": {"x": self.x, "y": self.y, "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)},
                }
            },
        }
