"""Pure pose math — no ROS, no I/O.

These functions implement the local-navigation motion compensation: when the robot
moves between capturing an image and executing the navigation command the agent
returned, the target (expressed in the robot frame at capture time) must be
re-expressed in the robot's current frame.

A pose is a plain ``(x, y, theta)`` tuple in metres / radians. Kept import-clean so
it can be unit-tested without a ROS runtime.
"""

from __future__ import annotations

import math

Pose = tuple[float, float, float]
Delta = tuple[float, float, float]


def compute_pose_delta(old_pose: Pose, new_pose: Pose) -> Delta:
    """Motion from ``old_pose`` to ``new_pose`` in the robot frame at ``old_pose``.

    Returns ``(delta_forward, delta_lateral, delta_theta)``:
    - ``delta_forward``  metres moved along the original heading
    - ``delta_lateral``  metres moved sideways (positive = left)
    - ``delta_theta``    rotation in radians, normalised to [-pi, pi]
      (positive = counter-clockwise)
    """
    old_x, old_y, old_theta = old_pose
    new_x, new_y, new_theta = new_pose

    dx = new_x - old_x
    dy = new_y - old_y

    cos_t = math.cos(-old_theta)
    sin_t = math.sin(-old_theta)
    delta_forward = dx * cos_t - dy * sin_t
    delta_lateral = dx * sin_t + dy * cos_t

    delta_theta = math.atan2(math.sin(new_theta - old_theta), math.cos(new_theta - old_theta))
    return (delta_forward, delta_lateral, delta_theta)


def adjust_local_nav_command(inputs: dict, delta: Delta) -> dict:
    """Re-express a local-frame navigation target after the robot has moved.

    For ``navigate_to_position`` with ``local_frame=True`` the original ``(x, y,
    theta)`` is in the frame where the robot stood at image capture; this shifts it
    into the robot's current frame using ``delta`` (from :func:`compute_pose_delta`).

    Global-frame commands are returned unchanged. The input dict is never mutated;
    a copy with updated ``x``/``y``/``theta`` is returned.
    """
    if not inputs.get("local_frame", False):
        return inputs

    delta_forward, delta_lateral, delta_theta = delta

    orig_x = inputs.get("x", 0.0)
    orig_y = inputs.get("y", 0.0)
    orig_theta = inputs.get("theta", 0.0)

    # Translate by the robot's displacement, then rotate into the new frame.
    translated_x = orig_x - delta_forward
    translated_y = orig_y - delta_lateral

    cos_dt = math.cos(-delta_theta)
    sin_dt = math.sin(-delta_theta)
    new_x = translated_x * cos_dt - translated_y * sin_dt
    new_y = translated_x * sin_dt + translated_y * cos_dt

    new_theta = math.atan2(math.sin(orig_theta - delta_theta), math.cos(orig_theta - delta_theta))

    adjusted = inputs.copy()
    adjusted["x"] = new_x
    adjusted["y"] = new_y
    adjusted["theta"] = new_theta
    return adjusted
