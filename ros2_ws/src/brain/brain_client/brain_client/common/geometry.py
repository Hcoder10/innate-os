"""Geometry helpers shared across brain_client nodes."""

import math


def quaternion_to_yaw(orientation) -> float:
    """Return the yaw (rotation about Z, in radians) of a quaternion.

    ``orientation`` is any object exposing ``w``/``x``/``y``/``z`` fields,
    such as ``geometry_msgs.msg.Quaternion``.
    """
    siny_cosp = 2.0 * (orientation.w * orientation.z + orientation.x * orientation.y)
    cosy_cosp = 1.0 - 2.0 * (orientation.y * orientation.y + orientation.z * orientation.z)
    return math.atan2(siny_cosp, cosy_cosp)
