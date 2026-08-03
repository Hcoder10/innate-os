# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing arm state. ROS-free on purpose."""

import math
from dataclasses import dataclass


def quat_to_rpy(qx: float, qy: float, qz: float, qw: float) -> tuple[float, float, float]:
    """(roll, pitch, yaw) in radians from a quaternion (ZYX convention)."""
    sinr_cosp = 2 * (qw * qx + qy * qz)
    cosr_cosp = 1 - 2 * (qx * qx + qy * qy)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    sinp = 2 * (qw * qy - qz * qx)
    pitch = math.copysign(math.pi / 2, sinp) if abs(sinp) >= 1 else math.asin(sinp)

    siny_cosp = 2 * (qw * qz + qx * qy)
    cosy_cosp = 1 - 2 * (qy * qy + qz * qz)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return (roll, pitch, yaw)


@dataclass(frozen=True)
class Arm:
    """An end-effector snapshot, read via ``self.arm`` in skills.

    The pose is the arm's live forward kinematics (/fk_pose); ``gripper`` is
    the claw servo's joint position (j6), the same value skills previously
    dug out of ``joint_states["position"][5]``.
    """

    x: float
    """End-effector X in meters, arm base frame."""
    y: float
    """End-effector Y in meters, arm base frame."""
    z: float
    """End-effector Z in meters, arm base frame."""
    qx: float
    qy: float
    qz: float
    qw: float
    """End-effector orientation quaternion."""
    gripper: float | None = None
    """Gripper joint (j6) position in radians; None if joint states are missing."""
    frame_id: str = ""

    @property
    def position(self) -> tuple[float, float, float]:
        """(x, y, z) in meters, arm base frame."""
        return (self.x, self.y, self.z)

    @property
    def orientation(self) -> tuple[float, float, float, float]:
        """(qx, qy, qz, qw)."""
        return (self.qx, self.qy, self.qz, self.qw)

    @property
    def rpy(self) -> tuple[float, float, float]:
        """(roll, pitch, yaw) in radians, derived from the quaternion."""
        return quat_to_rpy(self.qx, self.qy, self.qz, self.qw)

    @property
    def roll(self) -> float:
        """Roll in radians."""
        return self.rpy[0]

    @property
    def pitch(self) -> float:
        """Pitch in radians."""
        return self.rpy[1]

    @property
    def yaw(self) -> float:
        """Yaw in radians."""
        return self.rpy[2]
