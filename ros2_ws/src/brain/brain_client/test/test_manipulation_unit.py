# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""No-ROS behavioral tests for the arm interface.

Pins the behaviors field skills depend on — IK 5→6 gripper padding, gripper
percent/strength math, the reach clamp, the open-verify self-heal, and the
start()/stop() lifecycle invariants (subscriptions are created once and never
destroyed; destroying one under the spinning private executor crashes the
process with InvalidHandle → rmw_zenoh SIGABRT).

Instances are built with ``Manipulation.__new__`` + hand-set attributes; no
node, no executor, no rclpy.init. These tests stub internal seams and may be
updated alongside internal refactors — the strict compat tripwire is
test_manipulation_surface.py, which stubs nothing.
"""

import threading
from types import SimpleNamespace

import pytest
import ros_stubs

ros_stubs.install()

import rclpy.executors  # noqa: E402  (real or stubbed, both fine)

from brain_client.robot.manipulation import Manipulation  # noqa: E402


class FakeFuture:
    def __init__(self, result):
        self._result = result

    def done(self):
        return True

    def result(self):
        return self._result

    def add_done_callback(self, callback):
        callback(self)


class FakeClient:
    def __init__(self, success=True):
        self.requests = []
        self._success = success

    def service_is_ready(self):
        return True

    def call_async(self, request):
        self.requests.append(request)
        return FakeFuture(SimpleNamespace(success=self._success))


class FakeLogger:
    def __init__(self):
        self.messages = []

    def _log(self, level):
        def log(msg, *args, **kwargs):
            self.messages.append((level, str(msg)))

        return log

    def __getattr__(self, name):
        if name in ("debug", "info", "warn", "warning", "error", "fatal"):
            return self._log(name)
        raise AttributeError(name)


def bare_manipulation(**overrides):
    """A Manipulation with no ROS entities: only the attributes under test."""
    m = Manipulation.__new__(Manipulation)
    m.logger = FakeLogger()
    m._ik_lock = threading.Lock()
    m._lifecycle_lock = threading.Lock()
    m._active = True
    m._executor = None
    m._executor_thread = None
    m._ik_solution = None
    m._ik_solution_fk = None
    m._fk_pose = None
    m._arm_state = None
    m._torque_enabled = None
    for key, value in overrides.items():
        setattr(m, key, value)
    return m


def joint_state(positions):
    return SimpleNamespace(position=list(positions))


# --- IK 5→6 gripper padding -------------------------------------------------


def capture_joint_moves(m):
    calls = []

    def fake_move(joint_positions, duration=3.0, blocking=False):
        calls.append(list(joint_positions))
        return True

    m.move_to_joint_positions = fake_move
    return calls


def test_cartesian_pads_gripper_with_explicit_position():
    m = bare_manipulation()
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    calls = capture_joint_moves(m)
    assert m.move_to_cartesian_pose(0.3, 0.0, 0.1, gripper_position=0.7)
    assert calls[0] == [0.1, 0.2, 0.3, 0.4, 0.5, 0.7]


def test_cartesian_pads_gripper_from_arm_state():
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.42]))
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    calls = capture_joint_moves(m)
    assert m.move_to_cartesian_pose(0.3, 0.0, 0.1)
    assert calls[0][5] == 0.42


def test_cartesian_pads_gripper_zero_when_unknown():
    m = bare_manipulation()
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    calls = capture_joint_moves(m)
    assert m.move_to_cartesian_pose(0.3, 0.0, 0.1)
    assert calls[0][5] == 0.0


def test_cartesian_fails_when_ik_fails():
    m = bare_manipulation()
    m.solve_ik = lambda *a, **k: None
    calls = capture_joint_moves(m)
    assert not m.move_to_cartesian_pose(0.3, 0.0, 0.1)
    assert calls == []


def test_trajectory_pads_every_waypoint_and_flattens():
    client = FakeClient()
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.3]), _goto_js_traj_client=client)
    m.solve_ik = lambda x, y, z, *a, **k: [x, y, z, 0.0, 0.0]
    m.spin_node_to_refresh_topics = lambda *a, **k: None
    poses = [{"x": 0.30, "y": 0.0, "z": 0.20}, {"x": 0.30, "y": 0.0, "z": 0.10}]
    assert m.move_cartesian_trajectory(poses, segment_durations=[0.8])
    request = client.requests[0]
    assert request.num_joints == 6
    assert list(request.segment_durations) == [0.8]
    assert list(request.waypoints.data) == [0.30, 0.0, 0.20, 0.0, 0.0, 0.3, 0.30, 0.0, 0.10, 0.0, 0.0, 0.3]


def test_trajectory_prefers_explicit_gripper():
    client = FakeClient()
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.3]), _goto_js_traj_client=client)
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    m.spin_node_to_refresh_topics = lambda *a, **k: None
    poses = [{"x": 0.3, "y": 0.0, "z": 0.2}, {"x": 0.3, "y": 0.0, "z": 0.1}]
    assert m.move_cartesian_trajectory(poses, gripper_position=0.55)
    data = list(client.requests[0].waypoints.data)
    assert data[5] == 0.55 and data[11] == 0.55


# --- gripper math -----------------------------------------------------------


def capture_gripper(m):
    calls = []

    def fake_command(j6, duration, blocking):
        calls.append(j6)
        return True

    m._command_gripper = fake_command
    return calls


def test_open_gripper_percent_math():
    m = bare_manipulation()
    calls = capture_gripper(m)
    assert m.open_gripper(100.0)
    assert m.open_gripper(50.0)
    assert m.open_gripper(0.0)
    assert m.open_gripper(250.0)  # clamped to 100
    assert calls == pytest.approx([0.85, 0.425, 0.0, 0.85])


def test_close_gripper_strength_clamped():
    m = bare_manipulation()
    calls = capture_gripper(m)
    assert m.close_gripper()  # released default = just closed
    assert m.close_gripper(strength=0.3)
    assert m.close_gripper(strength=5.0)  # clamped to GRIPPER_MAX_STRENGTH
    assert calls == pytest.approx([0.0, -0.3, -Manipulation.GRIPPER_MAX_STRENGTH])


def test_open_gripper_blocking_self_heals_tripped_servo():
    # Servo reads shut (< GRIPPER_SHUT_J6) after a blocking open: recover once,
    # retry once, then give up with False.
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.02]))
    m.spin_node_to_refresh_topics = lambda *a, **k: None
    m._command_gripper = lambda j6, duration, blocking: True
    recoveries = []
    m.recover = lambda *a, **k: recoveries.append(1)
    assert m.open_gripper(100.0, blocking=True) is False
    assert len(recoveries) == 1


def test_open_gripper_blocking_passes_when_open():
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.8]))
    m.spin_node_to_refresh_topics = lambda *a, **k: None
    m._command_gripper = lambda j6, duration, blocking: True
    m.recover = lambda *a, **k: pytest.fail("recover must not run when the claw opened")
    assert m.open_gripper(100.0, blocking=True) is True


# --- reach clamp ------------------------------------------------------------


def test_clamp_reach_corners():
    x_lo, x_hi = Manipulation.REACH_X
    y_lo, y_hi = Manipulation.REACH_Y
    assert Manipulation.clamp_reach(0.0, 0.0) == (x_lo, 0.0)
    assert Manipulation.clamp_reach(1.0, 1.0) == (x_hi, y_hi)
    assert Manipulation.clamp_reach(0.3, -1.0) == (0.3, y_lo)
    inside = ((x_lo + x_hi) / 2, (y_lo + y_hi) / 2)
    assert Manipulation.clamp_reach(*inside) == inside


# --- torque gate ------------------------------------------------------------


def test_moves_refused_while_torque_known_off():
    client = FakeClient()
    m = bare_manipulation(_torque_enabled=False, _goto_js_client=client)
    assert m.move_to_joint_positions([0, 0, 0, 0, 0, 0]) is False
    assert client.requests == []


# --- lifecycle invariants ----------------------------------------------------


class FakeExecutor:
    instances = []

    def __init__(self):
        self.nodes = []
        self.shutdowns = 0
        FakeExecutor.instances.append(self)

    def add_node(self, node):
        self.nodes.append(node)

    def spin(self):
        pass

    def shutdown(self):
        self.shutdowns += 1


class FakeNode:
    def __init__(self):
        self.subscriptions_created = 0

    def create_subscription(self, *args, **kwargs):
        self.subscriptions_created += 1
        return SimpleNamespace()


def lifecycle_manipulation(monkeypatch):
    monkeypatch.setattr(rclpy.executors, "SingleThreadedExecutor", FakeExecutor)
    node = FakeNode()
    m = bare_manipulation(node=node, _active=False)
    m._ik_solution_sub = None
    m._ik_solution_fk_sub = None
    m._fk_pose_sub = None
    m._arm_state_sub = None
    return m, node


def test_start_creates_subscriptions_exactly_once(monkeypatch):
    m, node = lifecycle_manipulation(monkeypatch)
    m.start()
    assert node.subscriptions_created == 4
    m.start()
    assert node.subscriptions_created == 4  # idempotent
    assert m._active is True


def test_stop_parks_executor_but_never_destroys_subscriptions(monkeypatch):
    # Destroying a subscription under the spinning private executor races
    # _take_subscription (InvalidHandle → rmw_zenoh SIGABRT). stop() must gate
    # callbacks and park the executor, keeping every handle alive.
    m, node = lifecycle_manipulation(monkeypatch)
    m.start()
    m._fk_pose = SimpleNamespace()
    m._arm_state = SimpleNamespace()
    m.stop()
    assert m._active is False
    assert m._executor is None
    assert node.subscriptions_created == 4
    assert m._arm_state_sub is not None and m._fk_pose_sub is not None
    assert m._fk_pose is None and m._arm_state is None  # cached state cleared
    m.start()  # restart re-arms without re-creating subscriptions
    assert node.subscriptions_created == 4


def test_gated_callbacks_drop_messages_while_inactive(monkeypatch):
    m, _ = lifecycle_manipulation(monkeypatch)
    m._active = False
    m._fk_pose_callback(SimpleNamespace())
    m._arm_state_callback(SimpleNamespace())
    assert m._fk_pose is None and m._arm_state is None
    m._active = True
    sentinel = SimpleNamespace()
    m._fk_pose_callback(sentinel)
    assert m._fk_pose is sentinel
