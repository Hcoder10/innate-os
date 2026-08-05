# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""API-surface snapshot for the arm SDK.

Skills are written against the surface below and must keep working across
refactors: same names, same parameter order, same defaults (a changed default
like ``block=True`` is a silent field regression), same constants. This test
is the tripwire — an intentional API change updates the table here in the
same commit.

Runs without ROS via ros_stubs (no-op inside the CI image).
"""

import inspect

import pytest
import ros_stubs

ros_stubs.install()

from brain_client.robot import manipulation as manipulation_mod  # noqa: E402
from brain_client.robot.manipulation import Manipulation  # noqa: E402

REQUIRED = inspect.Parameter.empty  # sentinel: parameter has no default

# Method → ordered (parameter, default) pairs, `self` omitted.
EXPECTED_SURFACE = {
    "move_to": [
        ("x", REQUIRED),
        ("y", REQUIRED),
        ("z", REQUIRED),
        ("roll", 0.0),
        ("pitch", 0.0),
        ("yaw", 0.0),
        ("duration", 1.5),
        ("grip", None),
        ("tolerance_xy", 0.05),
        ("tolerance_z", 0.10),
        ("block", True),
    ],
    "move_by": [
        ("dx", 0.0),
        ("dy", 0.0),
        ("dz", 0.0),
        ("droll", 0.0),
        ("dpitch", 0.0),
        ("dyaw", 0.0),
        ("duration", 0.5),
        ("grip", None),
        ("tolerance_xy", 0.05),
        ("tolerance_z", 0.10),
        ("block", True),
    ],
    "follow": [("waypoints", REQUIRED), ("grip", None), ("block", True)],
    "move_joints": [("joints", REQUIRED), ("duration", 3.0), ("block", True)],
    "rest": [("duration", 3.0)],
    "wait": [("timeout", None)],
    "gripper_open": [("percent", 100.0), ("duration", 0.5), ("block", True)],
    "gripper_close": [("strength", 0.0), ("duration", 0.5), ("block", True)],
    "torque_on": [],
    "torque_off": [],
    "reboot_servos": [],
    "recover": [],
    "halt": [],
    "start": [],
    "stop": [],
    "shutdown": [],
}

EXPECTED_CONSTANTS = {"GRIPPER_CLOSED": 0.0, "GRIPPER_OPEN": 0.85, "GRIPPER_MAX_STRENGTH": 0.6}

EXPECTED_PROPERTIES = ("pose", "moving", "torque_enabled", "joint_names", "last_fk_pose")

# The pre-0.7 (0.6.0) compat shims were removed; they must not creep back.
REMOVED_LEGACY = (
    "solve_ik",
    "move_to_joint_positions",
    "move_to_cartesian_pose",
    "move_cartesian_trajectory",
    "open_gripper",
    "close_gripper",
    "get_current_end_effector_pose",
    "get_current_orientation_rpy",
    "spin_node_to_refresh_topics",
)


@pytest.mark.parametrize("name", sorted(EXPECTED_SURFACE))
def test_method_signature_unchanged(name):
    method = getattr(Manipulation, name, None)
    assert method is not None, f"Manipulation.{name} disappeared"
    params = [(p.name, p.default) for p in inspect.signature(method).parameters.values() if p.name != "self"]
    assert params == EXPECTED_SURFACE[name]


def test_constants_unchanged():
    for name, value in EXPECTED_CONSTANTS.items():
        assert getattr(Manipulation, name) == value


@pytest.mark.parametrize("name", REMOVED_LEGACY)
def test_legacy_surface_stays_removed(name):
    assert not hasattr(Manipulation, name), f"removed 0.6.0 shim Manipulation.{name} reappeared"


def test_constructor_contract():
    # skills_server constructs Manipulation(node, logger, lazy=True); the
    # annotation renders as Node or 'Node' depending on lazy-eval, so compare
    # parameter names and the lazy default only.
    params = inspect.signature(Manipulation.__init__).parameters
    assert list(params) == ["self", "node", "logger", "lazy"]
    assert params["lazy"].default is False


def test_framework_attributes_survive():
    # RobotStateProvider hangs its subscriptions off .node and reads
    # .last_fk_pose at 50 Hz — both are load-bearing framework surface.
    for name in EXPECTED_PROPERTIES:
        assert isinstance(getattr(Manipulation, name), property), f"Manipulation.{name} is no longer a property"
    assert "node" in inspect.signature(Manipulation.__init__).parameters


def test_exceptions_defined_here():
    # skills raise/catch these; the import path is contract.
    assert issubclass(manipulation_mod.ArmFailed, RuntimeError)
    assert issubclass(manipulation_mod.ArmUnhealthy, RuntimeError)


def test_innate_export_identity():
    pytest.importorskip("numpy")  # innate namespace pulls state/image.py
    import innate

    assert innate.Manipulation is Manipulation
