"""Unit tests for pure pose math (no ROS required)."""

import math

from brain_client.perception import pose


def test_zero_delta_when_unmoved():
    assert pose.compute_pose_delta((1.0, 2.0, 0.5), (1.0, 2.0, 0.5)) == (0.0, 0.0, 0.0)


def test_pure_forward_motion():
    # Robot heading +x, moves +1 in x -> 1m forward, 0 lateral.
    fwd, lat, dth = pose.compute_pose_delta((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    assert math.isclose(fwd, 1.0, abs_tol=1e-9)
    assert math.isclose(lat, 0.0, abs_tol=1e-9)
    assert math.isclose(dth, 0.0, abs_tol=1e-9)


def test_lateral_motion_depends_on_heading():
    # Heading +y (90deg): moving +1 in global x is 1m to the robot's right (-left).
    fwd, lat, dth = pose.compute_pose_delta((0.0, 0.0, math.pi / 2), (1.0, 0.0, math.pi / 2))
    assert math.isclose(fwd, 0.0, abs_tol=1e-9)
    assert math.isclose(lat, -1.0, abs_tol=1e-9)


def test_delta_theta_normalised():
    _, _, dth = pose.compute_pose_delta((0.0, 0.0, 3.0), (0.0, 0.0, -3.0))
    assert -math.pi <= dth <= math.pi


def test_global_command_unchanged():
    inp = {"x": 1.0, "y": 2.0, "theta": 0.3, "local_frame": False}
    assert pose.adjust_local_nav_command(inp, (5.0, 5.0, 1.0)) is inp


def test_local_command_compensated_and_not_mutated():
    inp = {"x": 1.0, "y": 0.0, "theta": 0.0, "local_frame": True}
    out = pose.adjust_local_nav_command(inp, (0.5, 0.0, 0.0))
    # Robot moved 0.5m forward, so a target 1m ahead is now 0.5m ahead.
    assert math.isclose(out["x"], 0.5, abs_tol=1e-9)
    assert inp["x"] == 1.0  # original untouched
