# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing arm state. ROS-free on purpose."""

from dataclasses import dataclass


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
