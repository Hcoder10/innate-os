# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the pure pose math (no ROS).

Focus: absolute_to_local_nav_command, the mapfree re-basing that turns the
model's absolute (pose-readout-frame) goals into local_frame goals instead of
letting them die at the inactive map-frame planner.
"""

import math

import pytest

from brain_client.perception.pose import (
    absolute_to_local_nav_command,
    adjust_local_nav_command,
    compute_pose_delta,
)


def test_absolute_goal_ahead_becomes_forward_local_goal():
    # Robot at (1, 2) facing +x; goal 3m further along +x.
    out = absolute_to_local_nav_command({"x": 4.0, "y": 2.0, "theta_degrees": 0.0}, (1.0, 2.0, 0.0))
    assert out["local_frame"] is True
    assert out["x"] == pytest.approx(3.0)
    assert out["y"] == pytest.approx(0.0)
    assert out["theta_degrees"] == pytest.approx(0.0)


def test_absolute_heading_is_rebased_onto_robot_heading():
    # The demo-log case: robot at heading 22 deg, model wants absolute -158 deg
    # (turn around). Locally that is a 180 deg rotation in place.
    out = absolute_to_local_nav_command({"x": 0.0, "y": 0.0, "theta_degrees": -158.0}, (0.0, 0.0, math.radians(22.0)))
    assert out["local_frame"] is True
    assert out["x"] == pytest.approx(0.0)
    assert out["y"] == pytest.approx(0.0)
    assert abs(out["theta_degrees"]) == pytest.approx(180.0)


def test_absolute_goal_respects_rotated_robot_frame():
    # Robot at origin facing +y (90 deg); a goal at (0, 1) is 1m straight ahead.
    out = absolute_to_local_nav_command({"x": 0.0, "y": 1.0, "theta_degrees": 90.0}, (0.0, 0.0, math.radians(90.0)))
    assert out["x"] == pytest.approx(1.0)
    assert out["y"] == pytest.approx(0.0)
    assert out["theta_degrees"] == pytest.approx(0.0)


def test_absolute_conversion_preserves_radian_theta_key():
    out = absolute_to_local_nav_command({"x": 0.0, "y": 0.0, "theta": math.pi / 2}, (0.0, 0.0, 0.0))
    assert "theta_degrees" not in out
    assert out["theta"] == pytest.approx(math.pi / 2)


def test_local_frame_input_passes_through_unchanged():
    inputs = {"x": 1.0, "y": 0.0, "theta_degrees": 90.0, "local_frame": True}
    assert absolute_to_local_nav_command(inputs, (5.0, 5.0, 1.0)) is inputs


def test_absolute_conversion_does_not_mutate_input():
    inputs = {"x": 4.0, "y": 2.0, "theta_degrees": 0.0}
    absolute_to_local_nav_command(inputs, (1.0, 2.0, 0.0))
    assert inputs == {"x": 4.0, "y": 2.0, "theta_degrees": 0.0}


def test_rebase_then_capture_delta_compensation_compose():
    # A rebased goal is exact at the current pose: applying a zero capture
    # delta (robot has not moved since) must leave it unchanged.
    rebased = absolute_to_local_nav_command({"x": 2.0, "y": 0.0, "theta_degrees": 0.0}, (1.0, 0.0, 0.0))
    unchanged = adjust_local_nav_command(rebased, compute_pose_delta((1.0, 0.0, 0.0), (1.0, 0.0, 0.0)))
    assert unchanged["x"] == pytest.approx(rebased["x"])
    assert unchanged["y"] == pytest.approx(rebased["y"])
    assert unchanged["theta_degrees"] == pytest.approx(rebased["theta_degrees"])
