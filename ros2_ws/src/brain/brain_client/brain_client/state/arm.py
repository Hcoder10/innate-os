# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing arm state. ROS-free on purpose."""

from dataclasses import dataclass
from functools import cached_property
from typing import Any

from brain_client.common.geometry import quat_to_rpy


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

    @classmethod
    def from_fk(cls, msg: Any, gripper: float | None = None) -> "Arm":
        """From a PoseStamped-shaped message (duck-typed to keep this module
        ROS-free)."""
        p = msg.pose
        return cls(
            x=p.position.x,
            y=p.position.y,
            z=p.position.z,
            qx=p.orientation.x,
            qy=p.orientation.y,
            qz=p.orientation.z,
            qw=p.orientation.w,
            gripper=gripper,
            frame_id=msg.header.frame_id,
        )

    @property
    def position(self) -> tuple[float, float, float]:
        """(x, y, z) in meters, arm base frame."""
        return (self.x, self.y, self.z)

    @property
    def orientation(self) -> tuple[float, float, float, float]:
        """(qx, qy, qz, qw)."""
        return (self.qx, self.qy, self.qz, self.qw)

    @cached_property
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
