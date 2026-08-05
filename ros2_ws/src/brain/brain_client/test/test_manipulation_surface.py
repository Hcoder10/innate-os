# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""API-surface snapshot for the arm interface's released (0.6.0) contract.

Customer skills in the field were written against the 0.6.0 surface below and
must keep working across the arm-SDK refactor: same names, same signatures
(defaults included — a changed default like ``blocking=False`` is a silent
field regression), same constants. This test is the tripwire: it must pass
unchanged before AND after the refactor.

Runs without ROS via ros_stubs (no-op inside the CI image).
"""

import inspect

import pytest
import ros_stubs

ros_stubs.install()

from brain_client.robot import manipulation as manipulation_mod  # noqa: E402
from brain_client.robot.manipulation import Manipulation  # noqa: E402

# str(inspect.signature(...)) of every method released in 0.6.0
# (`git show 0.6.0:.../manipulation.py`, class then named ManipulationInterface).
EXPECTED_SURFACE = {
    "move_to_cartesian_pose": (
        "(self, x: float, y: float, z: float, roll: float = 0.0, pitch: float = 0.0, "
        "yaw: float = 0.0, duration: float = 3.0, ik_timeout: float = 2.0, "
        "blocking: bool = False, gripper_position: float | None = None) -> bool"
    ),
    "move_to_joint_positions": (
        "(self, joint_positions: list[float], duration: float = 3.0, blocking: bool = False) -> bool"
    ),
    "move_cartesian_trajectory": (
        "(self, poses: list[dict], segment_duration: float = 1.0, "
        "segment_durations: list[float] | None = None, ik_timeout: float = 2.0, "
        "gripper_position: float | None = None) -> bool"
    ),
    "solve_ik": (
        "(self, x: float, y: float, z: float, roll: float = 0.0, pitch: float = 0.0, "
        "yaw: float = 0.0, timeout: float = 2.0) -> list[float] | None"
    ),
    "open_gripper": "(self, percent: float = 100.0, duration: float = 0.5, blocking: bool = False) -> bool",
    "close_gripper": "(self, strength: float = 0.0, duration: float = 0.5, blocking: bool = False) -> bool",
    "torque_on": "(self) -> bool",
    "torque_off": "(self) -> bool",
    "reboot_servos": "(self) -> bool",
    "get_current_end_effector_pose": "(self) -> dict | None",
    "get_current_orientation_rpy": "(self) -> dict | None",
    "spin_node_to_refresh_topics": "(self, count: int = 10, timeout_sec: float = 0.001)",
    "start": "(self)",
    "stop": "(self)",
    "shutdown": "(self)",
}

# 0.6.0 gripper constants.
EXPECTED_CONSTANTS = {"GRIPPER_CLOSED": 0.0, "GRIPPER_OPEN": 0.85}


@pytest.mark.parametrize("name", sorted(EXPECTED_SURFACE))
def test_legacy_method_signature_unchanged(name):
    method = getattr(Manipulation, name, None)
    assert method is not None, f"released method Manipulation.{name} disappeared"
    normalized = str(inspect.signature(method)).replace('"', "'")
    assert normalized == EXPECTED_SURFACE[name].replace('"', "'")


def test_constants_unchanged():
    for name, value in EXPECTED_CONSTANTS.items():
        assert getattr(Manipulation, name) == value


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
    assert isinstance(Manipulation.last_fk_pose, property)
    assert "node" in inspect.signature(Manipulation.__init__).parameters


def test_exceptions_defined_here():
    # pick-era code and shims raise/catch these; the import path is contract.
    assert issubclass(manipulation_mod.ArmFailed, RuntimeError)
    assert issubclass(manipulation_mod.ArmUnhealthy, RuntimeError)


def test_innate_export_identity():
    pytest.importorskip("numpy")  # innate namespace pulls state/image.py
    import innate

    assert innate.Manipulation is Manipulation
