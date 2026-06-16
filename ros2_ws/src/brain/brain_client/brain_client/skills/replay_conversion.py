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


def recording_action_to_replay(action, head=None, wheel_threshold: float = WHEEL_MOTION_THRESHOLD):
    """Transform a recorder ``/action`` array into the replay player's layout.

    Recorder layout per row: ``[arm joints (0:6), <leader extras>, cmd_vel.x,
    cmd_vel.z, progress, termination]`` — base velocity is always the two columns
    before the two trailing ``[progress, termination]`` columns (both always
    appended by the recorder). The replay player reads arm joints at cols ``0:6``
    and base cmd_vel at cols ``6:8``.

    Minimum width is 10 (6 arm + 2 cmd_vel + 2 trailing); below that the arm slice
    ``0:6`` and the cmd_vel slice ``-4:-2`` would overlap.

    ``head`` is an optional per-timestep head angle (degrees), recorded in its own
    dataset rather than in ``/action`` so it never touches the learned-policy
    action space. When supplied it becomes a 9th replay column that the player
    publishes to the head servo.

    Returns ``(replay_action, wheeled)`` where ``replay_action`` is ``(N, 8)`` — or
    ``(N, 9)`` when ``head`` is supplied — and ``wheeled`` is ``False`` (with
    cmd_vel zeroed) when the base never moved.
    """
    action = np.asarray(action, dtype=float)
    if action.ndim != 2 or action.shape[1] < 10:
        raise ValueError(f"Unusable recorder action shape {action.shape}; expected (N, >=10).")
    arm, cmd_vel = action[:, :6], action[:, -4:-2]
    wheeled = bool(np.any(np.abs(cmd_vel) > wheel_threshold))
    if not wheeled:
        cmd_vel = np.zeros_like(cmd_vel)
    columns = [arm, cmd_vel]
    if head is not None:
        head = np.asarray(head, dtype=float)
        if head.shape[0] != action.shape[0]:
            raise ValueError(f"head has {head.shape[0]} rows but action has {action.shape[0]}.")
        columns.append(head.reshape(-1, 1))
    return np.hstack(columns), wheeled
