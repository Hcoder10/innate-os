# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for resolve_local_goal in the navigate_to_position skill.

Local (base_link-relative) goals are composed with the robot's pose in the
fixed odom frame before being sent to Nav2, so the goal can be replanned
without TF lookups. These tests pin the planar composition math.
"""

import importlib.util
import math
from pathlib import Path

import pytest

_SKILL_PATH = Path(__file__).resolve().parents[5] / "workspace" / "innate_skills" / "navigate_to_position.py"


def _load_skill_module():
    spec = importlib.util.spec_from_file_location("navigate_to_position", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


navigate_to_position = pytest.importorskip("rclpy") and _load_skill_module()
resolve_local_goal = navigate_to_position.resolve_local_goal


def test_identity_base_passes_goal_through():
    assert resolve_local_goal(0.0, 0.0, 0.0, 0.3, 0.0, 0.0) == pytest.approx((0.3, 0.0, 0.0))


def test_forward_goal_follows_base_heading():
    gx, gy, gyaw = resolve_local_goal(0.0, 0.0, math.pi / 2.0, 0.3, 0.0, 0.0)
    assert (gx, gy, gyaw) == pytest.approx((0.0, 0.3, math.pi / 2.0))


def test_lateral_goal_is_rotated_into_fixed_frame():
    # Robot facing +y: "0.5 m to my left" is -x in the fixed frame.
    gx, gy, gyaw = resolve_local_goal(0.0, 0.0, math.pi / 2.0, 0.0, 0.5, 0.0)
    assert (gx, gy, gyaw) == pytest.approx((-0.5, 0.0, math.pi / 2.0))


def test_translated_and_rotated_base():
    gx, gy, gyaw = resolve_local_goal(1.0, 2.0, math.pi, 0.3, -0.1, 0.5)
    assert (gx, gy, gyaw) == pytest.approx((0.7, 2.1, math.pi + 0.5))


def test_backward_goal_matches_demo_second_waypoint():
    # run_routine_demo's second waypoint: 0.3 m behind the robot.
    gx, gy, gyaw = resolve_local_goal(4.0, -1.0, 0.0, -0.3, 0.0, 0.0)
    assert (gx, gy, gyaw) == pytest.approx((3.7, -1.0, 0.0))


def test_execute_takes_degrees_not_radians():
    """Units policy: user-facing angles are degrees with the unit in the name;
    the radians conversion happens once, on entry."""
    import inspect

    module = _load_skill_module()
    params = inspect.signature(module.NavigateToPosition.execute).parameters
    assert "theta_degrees" in params and "theta" not in params
