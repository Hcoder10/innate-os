# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing odometry state. ROS-free on purpose. MARS is a differential-
drive base on flat ground, so its pose is fully (x, y, yaw)."""

import math
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from brain_client.skills.dictcompat import LegacyMapping


@dataclass(frozen=True)
class Odometry(LegacyMapping):
    """A 2D odometry snapshot: pose in the odom frame plus body velocities.

    Read ambiently via ``self.odom`` while a skill runs — each read converts
    the newest /odom message. Also injected for legacy ``RobotState``
    descriptors.
    """

    x: float
    """Position along X in meters, odom frame."""
    y: float
    """Position along Y in meters, odom frame."""
    theta: float
    """Yaw in radians, counter-clockwise positive, wrapped to [-pi, pi]."""
    linear_velocity: float = 0.0
    """Forward speed in m/s (negative when driving backward)."""
    angular_velocity: float = 0.0
    """Turn rate in rad/s, counter-clockwise positive."""
    stamp: float = 0.0
    """Sensor timestamp in seconds (ROS time). float64: sub-microsecond
    precision at present-day epochs; the exact integer sec/nanosec are in
    ``raw``."""
    frame_id: str = "odom"
    child_frame_id: str = "base_link"
    raw_source: Any = field(default=None, repr=False, compare=False)
    """Source for ``raw``: a rosbridge-style dict (served as-is) or a
    nav_msgs/Odometry-like message (converted lazily on first access).
    Excluded from ==/hash — it is provenance, not part of the 2D snapshot;
    including it would also make production instances unhashable (dicts)."""

    def __post_init__(self):
        # theta's docstring is a contract (turn_in_place does heading math on
        # it), so enforce it for hand-built instances too. The injected path
        # already gets atan2 output; the guard keeps it trig-free at 50 Hz.
        if not -math.pi <= self.theta <= math.pi:
            # frozen dataclass: object.__setattr__ bypasses the immutability guard
            object.__setattr__(self, "theta", math.atan2(math.sin(self.theta), math.cos(self.theta)))

    @cached_property
    def raw(self) -> dict | None:
        """Escape hatch: the full nav_msgs/Odometry as plain data
        (rosbridge-style keys) — real quaternion, z, covariances, full twist —
        for skills that need more than the flat 2D pose. Built lazily so the
        50 Hz injection path pays nothing for it until a skill asks."""
        source = self.raw_source
        if source is None or isinstance(source, dict):
            return source
        return _msg_to_raw(source)

    @property
    def theta_degrees(self) -> float:
        """Yaw in degrees, counter-clockwise positive."""
        return math.degrees(self.theta)

    @property
    def position(self) -> tuple[float, float]:
        """(x, y) in meters, odom frame."""
        return (self.x, self.y)

    # --- legacy dict compatibility ---------------------------------------
    # LAST_ODOM injected a raw-message dict from 0.3.0 through 0.6.x; the
    # LegacyMapping mixin keeps that access working (see dictcompat.py).
    # Do not delete.

    _legacy_hint = "the Odometry attributes (odom.x, odom.theta_degrees, ...) or odom.raw for the full message"

    @cached_property
    def _legacy_dict(self) -> dict:
        """Exactly the 0.3.0-0.6.x injected shape — {header, child_frame_id,
        pose.pose, theta_degrees}, no twist, no covariance — whichever way the
        instance was built, so legacy access is path-independent. The extra
        data ``raw`` carries stays on ``raw``. Memoized: enumeration protocols
        (dict(odom), {**odom}) hit the mapping once per key."""
        base = self.raw if self.raw is not None else self._reconstructed_raw()
        return {
            "header": base["header"],
            "child_frame_id": base["child_frame_id"],
            "pose": {"pose": base["pose"]["pose"]},
            "theta_degrees": self.theta_degrees,
        }

    def _reconstructed_raw(self) -> dict:
        """Legacy-shape fallback for instances built without ``raw_source``
        (hand-constructed); the quaternion carries yaw only."""
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


def _msg_to_raw(msg) -> dict:
    """nav_msgs/Odometry message -> plain rosbridge-style dict. Duck-typed
    (attribute access only) so this module stays ROS-free."""
    pos = msg.pose.pose.position
    ori = msg.pose.pose.orientation
    twist = msg.twist.twist
    return {
        "header": {
            "stamp": {"sec": msg.header.stamp.sec, "nanosec": msg.header.stamp.nanosec},
            "frame_id": msg.header.frame_id,
        },
        "child_frame_id": msg.child_frame_id,
        "pose": {
            "pose": {
                "position": {"x": pos.x, "y": pos.y, "z": pos.z},
                "orientation": {"x": ori.x, "y": ori.y, "z": ori.z, "w": ori.w},
            },
            "covariance": list(msg.pose.covariance),
        },
        "twist": {
            "twist": {
                "linear": {"x": twist.linear.x, "y": twist.linear.y, "z": twist.linear.z},
                "angular": {"x": twist.angular.x, "y": twist.angular.y, "z": twist.angular.z},
            },
            "covariance": list(msg.twist.covariance),
        },
    }
