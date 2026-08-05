# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Geometry helpers shared across brain_client nodes."""

import math


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


def quaternion_to_yaw(orientation) -> float:
    """Return the yaw (rotation about Z, in radians) of a quaternion.

    ``orientation`` is any object exposing ``w``/``x``/``y``/``z`` fields,
    such as ``geometry_msgs.msg.Quaternion``.
    """
    return quat_to_rpy(orientation.x, orientation.y, orientation.z, orientation.w)[2]
