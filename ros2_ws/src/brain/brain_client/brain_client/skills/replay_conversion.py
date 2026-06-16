"""Pure (ROS-free) helpers for converting a recorded teleop episode into a
replay-skill trajectory.

Kept separate from :mod:`brain_client.skills.catalog` so it imports nothing from
rclpy / brain_messages and can run in the fast (no-ROS) pytest bucket.
"""

from __future__ import annotations

import numpy as np

# A replay recording is classified "arm only" when neither base velocity command
# (linear.x / angular.z, m/s and rad/s) exceeds this magnitude at any timestep.
WHEEL_MOTION_THRESHOLD = 0.01


def recording_action_to_replay(action, wheel_threshold: float = WHEEL_MOTION_THRESHOLD):
    """Transform a recorder ``/action`` array into the replay player's layout.

    Recorder layout per row: ``[arm joints (0:6), <leader extras>, cmd_vel.x,
    cmd_vel.z, progress, termination]`` — base velocity is always the two columns
    before the two trailing termination columns (both are always appended). The
    replay player reads arm joints at cols ``0:6`` and base cmd_vel at cols ``6:8``.

    Returns ``(replay_action, wheeled)`` where ``replay_action`` is ``(N, 8)`` and
    ``wheeled`` is ``False`` (with cmd_vel zeroed) when the base never moved.
    """
    action = np.asarray(action, dtype=float)
    assert action.ndim == 2 and action.shape[1] >= 8, f"Unusable recorder action shape {action.shape}."
    arm, cmd_vel = action[:, :6], action[:, -4:-2]
    wheeled = bool(np.any(np.abs(cmd_vel) > wheel_threshold))
    if not wheeled:
        cmd_vel = np.zeros_like(cmd_vel)
    return np.hstack([arm, cmd_vel]), wheeled
