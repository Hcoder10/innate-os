"""Unit tests for the recorder -> replay action transform.

Pure Python + numpy (no rclpy / DDS), so it runs in the fast pytest bucket.
Pins the column mapping the replay player depends on:
recorder ``/action`` = [arm(0:6), <leader extras>, cmd_vel.x, cmd_vel.z, progress, term]
            ->  replay ``/action`` = [arm(0:6), cmd_vel.x, cmd_vel.z]  (width 8).
"""

import numpy as np
import pytest

from brain_client.skills.replay_conversion import recording_action_to_replay


def _make_recording(n=5, leader_width=10, cmd_vel=(0.0, 0.0)):
    """Build a recorder-format action array: leader(L) + cmd_vel(2) + termination(2)."""
    rng = np.arange(n * (leader_width + 4), dtype=float).reshape(n, leader_width + 4)
    rng[:, leader_width] = cmd_vel[0]  # cmd_vel.linear.x
    rng[:, leader_width + 1] = cmd_vel[1]  # cmd_vel.angular.z
    rng[:, leader_width + 2] = 0.5  # progress
    rng[:, leader_width + 3] = 1.0  # termination
    return rng


def test_arm_only_recording_is_not_wheeled_and_cmd_vel_zeroed():
    rec = _make_recording(cmd_vel=(0.0, 0.0))
    replay, wheeled = recording_action_to_replay(rec)
    assert wheeled is False
    assert replay.shape == (5, 8)
    # Arm joints preserved from cols 0:6.
    np.testing.assert_array_equal(replay[:, 0:6], rec[:, 0:6])
    # cmd_vel columns zeroed for an arm-only skill.
    np.testing.assert_array_equal(replay[:, 6:8], np.zeros((5, 2)))


def test_linear_base_motion_is_wheeled_and_cmd_vel_preserved():
    rec = _make_recording(cmd_vel=(0.3, 0.0))
    replay, wheeled = recording_action_to_replay(rec)
    assert wheeled is True
    np.testing.assert_array_equal(replay[:, 0:6], rec[:, 0:6])
    np.testing.assert_array_equal(replay[:, 6], np.full(5, 0.3))
    np.testing.assert_array_equal(replay[:, 7], np.zeros(5))


def test_angular_only_base_motion_is_wheeled():
    rec = _make_recording(cmd_vel=(0.0, -0.2))
    replay, wheeled = recording_action_to_replay(rec)
    assert wheeled is True
    np.testing.assert_array_equal(replay[:, 7], np.full(5, -0.2))


def test_threshold_is_inclusive_noise_floor():
    # Below threshold -> treated as arm-only (noise), zeroed.
    rec = _make_recording(cmd_vel=(0.005, 0.0))
    replay, wheeled = recording_action_to_replay(rec)
    assert wheeled is False
    np.testing.assert_array_equal(replay[:, 6:8], np.zeros((5, 2)))


def test_cmd_vel_read_before_termination_regardless_of_leader_width():
    # cmd_vel must be the two columns *before* the two termination columns,
    # not fixed indices — robust to the leader command width.
    rec = _make_recording(leader_width=8, cmd_vel=(0.4, 0.4))
    replay, wheeled = recording_action_to_replay(rec)
    assert wheeled is True
    assert replay.shape == (5, 8)
    np.testing.assert_array_equal(replay[:, 6:8], np.full((5, 2), 0.4))


def test_rejects_too_narrow_action():
    with pytest.raises(AssertionError):
        recording_action_to_replay(np.zeros((5, 6)))
