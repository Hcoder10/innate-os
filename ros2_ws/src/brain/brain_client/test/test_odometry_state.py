# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Tests for the skill-facing Odometry state type.

Pins two contracts:

1. **Attribute API** — the flat 2D pose (x, y, theta) plus velocities that
   skills and the docs rely on.
2. **Legacy dict access** — skills written against releases up to 0.6.x read
   the injected state as a raw-message dict (``odom["theta_degrees"]``,
   ``odom["pose"]["pose"]["position"]``); that shape must keep working.

Part of the fast (no-ROS) pytest bucket in ci/run_integration_tests.sh.
"""

import math

import pytest

from brain_client.skills.odometry import Odometry

ODOM = Odometry(
    x=1.5,
    y=-2.0,
    theta=math.pi / 2,
    linear_velocity=0.15,
    angular_velocity=-0.3,
    stamp=100.25,
    frame_id="odom",
    child_frame_id="base_link",
)


def test_attribute_api():
    assert ODOM.position == (1.5, -2.0)
    assert ODOM.theta_degrees == pytest.approx(90.0)
    assert ODOM.linear_velocity == 0.15
    assert ODOM.angular_velocity == -0.3
    assert ODOM.stamp == 100.25


def test_immutable():
    with pytest.raises(AttributeError):
        ODOM.x = 0.0


def test_legacy_dict_access_matches_old_injected_shape():
    with pytest.warns(DeprecationWarning):
        assert ODOM["theta_degrees"] == pytest.approx(90.0)
    with pytest.warns(DeprecationWarning):
        pose = ODOM["pose"]["pose"]
    assert pose["position"] == {"x": 1.5, "y": -2.0, "z": 0.0}
    # the quaternion must encode the same yaw the attributes report
    ori = pose["orientation"]
    siny_cosp = 2.0 * (ori["w"] * ori["z"] + ori["x"] * ori["y"])
    cosy_cosp = 1.0 - 2.0 * (ori["y"] ** 2 + ori["z"] ** 2)
    assert math.atan2(siny_cosp, cosy_cosp) == pytest.approx(math.pi / 2)
    with pytest.warns(DeprecationWarning):
        header = ODOM["header"]
    assert header == {"stamp": {"sec": 100, "nanosec": 250_000_000}, "frame_id": "odom"}
    with pytest.warns(DeprecationWarning):
        assert ODOM["child_frame_id"] == "base_link"


def test_legacy_dict_access_unknown_key_raises_keyerror():
    with pytest.warns(DeprecationWarning), pytest.raises(KeyError):
        ODOM["twist"]
