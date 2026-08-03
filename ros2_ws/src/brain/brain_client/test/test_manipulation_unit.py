# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""No-ROS behavioral tests for the arm interface.

Pins the behaviors field skills depend on — IK 5→6 gripper padding, gripper
percent/strength math, the reach clamp, the open-verify self-heal, the
standing grip target, and the start()/stop() lifecycle invariants
(subscriptions are created once and never destroyed; destroying one under
the spinning private executor crashes the process with InvalidHandle →
rmw_zenoh SIGABRT).

Instances are built with ``Manipulation.__new__`` + hand-set attributes; no
node, no executor, no rclpy.init. These tests stub internal seams and may be
updated alongside internal refactors — the strict compat tripwire is
test_manipulation_surface.py, which stubs nothing.
"""

import math
import threading
from types import SimpleNamespace

import pytest
import ros_stubs

ros_stubs.install()

import rclpy.executors  # noqa: E402  (real or stubbed, both fine)

from brain_client.robot.manipulation import ArmFailed, ArmUnhealthy, Manipulation, Waypoint  # noqa: E402
from brain_client.state.arm import Arm  # noqa: E402


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
    m._torque_stamp = 0.0
    m._status_torque = None
    m._status_stamp = 0.0
    m._grip_target = None
    m._spin_briefly = lambda *a, **k: None
    for key, value in overrides.items():
        setattr(m, key, value)
    return m


def joint_state(positions):
    return SimpleNamespace(position=list(positions))


def fk_pose(x, y, z, qx=0.0, qy=0.0, qz=0.0, qw=1.0, frame_id="base_link"):
    return SimpleNamespace(
        header=SimpleNamespace(frame_id=frame_id),
        pose=SimpleNamespace(
            position=SimpleNamespace(x=x, y=y, z=z),
            orientation=SimpleNamespace(x=qx, y=qy, z=qz, w=qw),
        ),
    )


def capture_gotos(m, ok=True):
    calls = []

    def fake_goto(joint_positions, duration, wait):
        calls.append((list(joint_positions), duration, wait))
        if ok:
            m._grip_target = float(joint_positions[5])
        return ok

    m._goto = fake_goto
    return calls


# --- legacy shims: IK 5→6 gripper padding ------------------------------------


def test_legacy_cartesian_pads_gripper_with_explicit_position():
    m = bare_manipulation()
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    calls = capture_gotos(m)
    assert m.move_to_cartesian_pose(0.3, 0.0, 0.1, gripper_position=0.7)
    assert calls[0][0] == [0.1, 0.2, 0.3, 0.4, 0.5, 0.7]


def test_legacy_cartesian_reseeds_gripper_from_measured_state():
    # The released 0.6.0 default: j6 re-seeded from the MEASURED position
    # (the drop-the-object footgun move_to() fixes; preserved in the shim).
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.42]))
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    calls = capture_gotos(m)
    assert m.move_to_cartesian_pose(0.3, 0.0, 0.1)
    assert calls[0][0][5] == 0.42


def test_legacy_cartesian_pads_gripper_zero_when_unknown():
    m = bare_manipulation()
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    calls = capture_gotos(m)
    assert m.move_to_cartesian_pose(0.3, 0.0, 0.1)
    assert calls[0][0][5] == 0.0


def test_legacy_cartesian_fails_when_ik_fails():
    m = bare_manipulation()
    m.solve_ik = lambda *a, **k: None
    calls = capture_gotos(m)
    assert not m.move_to_cartesian_pose(0.3, 0.0, 0.1)
    assert calls == []


def test_legacy_cartesian_defaults_to_non_blocking():
    m = bare_manipulation()
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    calls = capture_gotos(m)
    assert m.move_to_cartesian_pose(0.3, 0.0, 0.1)
    assert calls[0][2] is False  # blocking=False is released field behavior


def test_legacy_trajectory_pads_every_waypoint_and_flattens():
    client = FakeClient()
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.3]), _goto_js_traj_client=client)
    m.solve_ik = lambda x, y, z, *a, **k: [x, y, z, 0.0, 0.0]
    poses = [{"x": 0.30, "y": 0.0, "z": 0.20}, {"x": 0.30, "y": 0.0, "z": 0.10}]
    assert m.move_cartesian_trajectory(poses, segment_durations=[0.8])
    request = client.requests[0]
    assert request.num_joints == 6
    assert list(request.segment_durations) == [0.8]
    assert list(request.waypoints.data) == [0.30, 0.0, 0.20, 0.0, 0.0, 0.3, 0.30, 0.0, 0.10, 0.0, 0.0, 0.3]


def test_legacy_trajectory_prefers_explicit_gripper():
    client = FakeClient()
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.3]), _goto_js_traj_client=client)
    m.solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    poses = [{"x": 0.3, "y": 0.0, "z": 0.2}, {"x": 0.3, "y": 0.0, "z": 0.1}]
    assert m.move_cartesian_trajectory(poses, gripper_position=0.55)
    data = list(client.requests[0].waypoints.data)
    assert data[5] == 0.55 and data[11] == 0.55


def test_legacy_trajectory_still_requires_two_poses():
    m = bare_manipulation()
    assert m.move_cartesian_trajectory([{"x": 0.3, "y": 0.0, "z": 0.2}]) is False


# --- legacy shims: gripper math ----------------------------------------------


def capture_gripper(m):
    calls = []

    def fake_command(j6, duration, blocking):
        calls.append(j6)
        return True

    m._command_gripper = fake_command
    return calls


def test_legacy_open_gripper_percent_math():
    m = bare_manipulation()
    calls = capture_gripper(m)
    assert m.open_gripper(100.0)
    assert m.open_gripper(50.0)
    assert m.open_gripper(0.0)
    assert m.open_gripper(250.0)  # clamped to 100
    assert calls == pytest.approx([0.85, 0.425, 0.0, 0.85])


def test_legacy_close_gripper_strength_clamped():
    m = bare_manipulation()
    calls = capture_gripper(m)
    assert m.close_gripper()  # released default = just closed
    assert m.close_gripper(strength=0.3)
    assert m.close_gripper(strength=5.0)  # clamped to GRIPPER_MAX_STRENGTH
    assert calls == pytest.approx([0.0, -0.3, -Manipulation.GRIPPER_MAX_STRENGTH])


def test_legacy_open_gripper_blocking_self_heals_tripped_servo():
    # Servo reads shut (< the tripped threshold) after a blocking open:
    # recover once, retry once, then give up with False.
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.02]))
    m._command_gripper = lambda j6, duration, blocking: True
    recoveries = []
    m.recover = lambda *a, **k: recoveries.append(1)
    assert m.open_gripper(100.0, blocking=True) is False
    assert len(recoveries) == 1


def test_legacy_open_gripper_blocking_passes_when_open():
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.8]))
    m._command_gripper = lambda j6, duration, blocking: True
    m.recover = lambda *a, **k: pytest.fail("recover must not run when the claw opened")
    assert m.open_gripper(100.0, blocking=True) is True


# --- reach clamp ---------------------------------------------------------------


def test_clamp_reach_corners():
    x_lo, x_hi = Manipulation.REACH_X
    y_lo, y_hi = Manipulation.REACH_Y
    assert Manipulation.clamp_reach(0.0, 0.0) == (x_lo, 0.0)
    assert Manipulation.clamp_reach(1.0, 1.0) == (x_hi, y_hi)
    assert Manipulation.clamp_reach(0.3, -1.0) == (0.3, y_lo)
    inside = ((x_lo + x_hi) / 2, (y_lo + y_hi) / 2)
    assert Manipulation.clamp_reach(*inside) == inside


# --- torque gate ---------------------------------------------------------------


def test_moves_refused_while_torque_known_off():
    client = FakeClient()
    m = bare_manipulation(_torque_enabled=False, _torque_stamp=1.0, _goto_js_client=client)
    assert m.move_to_joint_positions([0, 0, 0, 0, 0, 0]) is False
    assert client.requests == []


def test_torque_enabled_prefers_fresher_status_heartbeat():
    # Local flag says on, but a newer /mars/arm/status (webapp toggled torque
    # out-of-band) says off — the merged view must believe the heartbeat.
    m = bare_manipulation(_torque_enabled=True, _torque_stamp=1.0, _status_torque=False, _status_stamp=2.0)
    assert m.torque_enabled is False
    m2 = bare_manipulation(_torque_enabled=True, _torque_stamp=3.0, _status_torque=False, _status_stamp=2.0)
    assert m2.torque_enabled is True
    assert bare_manipulation().torque_enabled is None


# --- new API: motion -------------------------------------------------------------


def test_move_to_returns_settled_pose():
    m = bare_manipulation(_fk_pose=fk_pose(0.30, 0.00, 0.10), _arm_state=joint_state([0, 0, 0, 0, 0, 0.5]))
    m._solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    calls = capture_gotos(m)
    settled = m.move_to(0.30, 0.00, 0.10)
    assert isinstance(settled, Arm)
    assert settled.position == (0.30, 0.00, 0.10)
    assert calls[0][2] is True  # always blocking
    assert calls[0][0][5] == 0.5  # carried the measured grip (nothing commanded yet)


def test_move_to_unreachable_raises_armfailed():
    m = bare_manipulation()
    m._solve_ik = lambda *a, **k: None
    m._goto = lambda *a, **k: pytest.fail("must not send a command without IK")
    with pytest.raises(ArmFailed):
        m.move_to(9.9, 9.9, 9.9)


def test_move_to_recovers_once_then_raises_unhealthy():
    # FK stuck far from the target: recover + retry once, then ArmUnhealthy.
    m = bare_manipulation(_fk_pose=fk_pose(0.10, 0.10, 0.30))
    m._solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    capture_gotos(m)
    recoveries = []
    m.recover = lambda: recoveries.append(1)
    with pytest.raises(ArmUnhealthy):
        m.move_to(0.30, 0.00, 0.10)
    assert len(recoveries) == 1


def test_move_to_tolerance_none_skips_that_axis():
    # z stopped short (fingers met the floor) — tol_z=None accepts it.
    m = bare_manipulation(_fk_pose=fk_pose(0.30, 0.00, 0.19))
    m._solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    capture_gotos(m)
    m.recover = lambda: pytest.fail("within-tolerance move must not recover")
    settled = m.move_to(0.30, 0.00, 0.10, tol_z=None)
    assert settled.z == 0.19


def test_move_to_unverified_raises_without_recovery():
    # Both tolerances None = caller opted out of health checking: a rejected
    # command is ArmFailed, never a reboot cycle.
    m = bare_manipulation(_fk_pose=fk_pose(0.1, 0.1, 0.3))
    m._solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    capture_gotos(m, ok=False)
    m.recover = lambda: pytest.fail("unverified move must not recover")
    with pytest.raises(ArmFailed):
        m.move_to(0.3, 0.0, 0.1, tol_xy=None, tol_z=None)


def test_move_joints_five_values_keeps_standing_grip():
    m = bare_manipulation(_grip_target=0.6)
    calls = capture_gotos(m)
    m.move_joints([0.1, 0.2, 0.3, 0.4, 0.5])
    assert calls[0][0] == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]


def test_move_joints_six_values_sets_standing_grip():
    m = bare_manipulation(_grip_target=0.6)
    capture_gotos(m)
    m.move_joints([0, 0, 0, 0, 0, 0.85])
    assert m._grip_target == 0.85


def test_move_joints_raises_on_rejection():
    m = bare_manipulation()
    capture_gotos(m, ok=False)
    with pytest.raises(ArmFailed):
        m.move_joints([0, 0, 0, 0, 0, 0])


def test_move_joints_rejects_wrong_arity():
    m = bare_manipulation()
    capture_gotos(m)
    with pytest.raises(ArmFailed):
        m.move_joints([0.0, 0.0])


def test_rest_recommands_to_settle_and_keeps_grip(monkeypatch):
    monkeypatch.setattr("brain_client.robot.manipulation.time.sleep", lambda s: None)
    m = bare_manipulation(_grip_target=0.55)
    calls = capture_gotos(m)
    m.rest(duration=2.0)
    assert len(calls) == 2  # travel + settle re-command
    assert calls[0][0][:5] == Manipulation.REST[:5] and calls[0][1] == 2.0
    assert calls[0][0][5] == 0.55 and calls[1][0][5] == 0.55


def test_follow_single_waypoint_uses_quintic_goto():
    m = bare_manipulation(_fk_pose=fk_pose(0.30, 0.00, 0.20))
    m._solve_ik = lambda *a, **k: [0.1, 0.2, 0.3, 0.4, 0.5]
    m._send_trajectory = lambda *a, **k: pytest.fail("single waypoint must not use the trajectory service")
    calls = capture_gotos(m)
    settled = m.follow([Waypoint(0.30, 0.00, 0.20, duration=2.0)])
    assert calls[0][1] == 2.0 and calls[0][2] is True
    assert isinstance(settled, Arm)


def test_follow_multi_waypoint_sends_one_trajectory():
    m = bare_manipulation(_fk_pose=fk_pose(0.30, 0.00, 0.10), _grip_target=0.3)
    m._solve_ik = lambda x, y, z, *a, **k: [x, y, z, 0.0, 0.0]
    sent = []
    m._send_trajectory = lambda wps, durs: (sent.append((wps, durs)), True)[1]
    m.follow(
        [Waypoint(0.30, 0.0, 0.20, duration=0.4), Waypoint(0.30, 0.0, 0.15), Waypoint(0.30, 0.0, 0.10, duration=0.8)]
    )
    waypoints, durations = sent[0]
    assert len(waypoints) == 3 and all(len(w) == 6 for w in waypoints)
    assert all(w[5] == 0.3 for w in waypoints)  # standing grip carried
    assert durations == [1.0, 0.8]  # per-segment, from the waypoint being reached


def test_follow_raises_on_ik_failure_with_waypoint_index():
    m = bare_manipulation()
    m._solve_ik = lambda x, y, z, *a, **k: None if z < 0.15 else [0.1, 0.2, 0.3, 0.4, 0.5]
    with pytest.raises(ArmFailed, match="waypoint 1"):
        m.follow([Waypoint(0.3, 0.0, 0.2), Waypoint(0.3, 0.0, 0.1)])


def test_follow_empty_raises():
    with pytest.raises(ArmFailed):
        bare_manipulation().follow([])


# --- new API: gripper + grip target ---------------------------------------------


def test_gripper_close_sets_standing_grip_target():
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.8]))
    capture_gotos(m)
    m.gripper_close(0.6)
    assert m._grip_target == pytest.approx(-0.6)


def test_gripper_close_strength_clamped():
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.8]))
    calls = capture_gotos(m)
    m.gripper_close(5.0)
    assert calls[0][0][5] == pytest.approx(-Manipulation.GRIPPER_MAX_STRENGTH)


def test_gripper_open_raises_unhealthy_when_tripped_shut():
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.02]))
    m._command_gripper = lambda j6, duration, blocking: True
    recoveries = []
    m.recover = lambda: recoveries.append(1)
    with pytest.raises(ArmUnhealthy):
        m.gripper_open()
    assert len(recoveries) == 1


def test_gripper_open_verifies_and_returns():
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.8]))
    m._command_gripper = lambda j6, duration, blocking: True
    m.recover = lambda: pytest.fail("recover must not run when the claw opened")
    m.gripper_open(100.0)


def test_standing_grip_survives_moves_and_recover(monkeypatch):
    monkeypatch.setattr("brain_client.robot.manipulation.time.sleep", lambda s: None)
    m = bare_manipulation(_arm_state=joint_state([0, 0, 0, 0, 0, 0.8]))
    calls = capture_gotos(m)
    m.gripper_close(0.5)  # grip an object
    m._arm_state = joint_state([0, 0, 0, 0, 0, 0.12])  # fingers stalled on it
    m.move_joints([0.1, 0.2, 0.3, 0.4, 0.5])  # 5-joint move: carry the grip
    assert calls[-1][0][5] == pytest.approx(-0.5), "move re-seeded j6 from measured — would drop the object"
    rebooted = []
    m.reboot_servos = lambda: (rebooted.append(1), setattr(m, "_grip_target", None))[0] or True
    m.torque_on = lambda: True
    m.recover()
    assert m._grip_target == pytest.approx(-0.5), "recover() must preserve the grip preload for the retry"


def test_torque_off_clears_standing_grip():
    m = bare_manipulation(_grip_target=-0.4)
    m._call_trigger = lambda *a, **k: True
    m._torque_off_client = object()
    assert m.torque_off()
    assert m._grip_target is None  # a limp claw holds nothing


# --- new API: reads --------------------------------------------------------------


def test_pose_returns_arm_with_gripper():
    m = bare_manipulation(_fk_pose=fk_pose(0.3, 0.1, 0.2, qw=1.0), _arm_state=joint_state([0, 0, 0, 0, 0, 0.44]))
    pose = m.pose
    assert isinstance(pose, Arm)
    assert pose.position == (0.3, 0.1, 0.2)
    assert pose.gripper == 0.44


def test_pose_raises_when_feed_dead(monkeypatch):
    monkeypatch.setattr("brain_client.robot.manipulation.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "brain_client.robot.manipulation.time.monotonic",
        iter([0.0, 0.5, 2.0]).__next__,
    )
    with pytest.raises(ArmFailed):
        _ = bare_manipulation().pose


def test_arm_rpy_math():
    # 90° yaw about z: q = (0, 0, sin45, cos45)
    arm = Arm(x=0, y=0, z=0, qx=0.0, qy=0.0, qz=math.sin(math.pi / 4), qw=math.cos(math.pi / 4))
    roll, pitch, yaw = arm.rpy
    assert roll == pytest.approx(0.0, abs=1e-9)
    assert pitch == pytest.approx(0.0, abs=1e-9)
    assert yaw == pytest.approx(math.pi / 2)
    assert arm.yaw == pytest.approx(math.pi / 2)


def test_halt_is_a_committed_noop():
    m = bare_manipulation()
    m._goto = lambda *a, **k: pytest.fail("halt must not command motion")
    m._call_trigger = lambda *a, **k: pytest.fail("halt must not touch torque")
    m.halt()


# --- lifecycle invariants ----------------------------------------------------


class FakeExecutor:
    def __init__(self):
        self.nodes = []
        self.shutdowns = 0

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


SUBSCRIPTION_COUNT = 5  # ik_solution, ik_solution_fk, fk_pose, arm state, arm status


def lifecycle_manipulation(monkeypatch):
    monkeypatch.setattr(rclpy.executors, "SingleThreadedExecutor", FakeExecutor)
    node = FakeNode()
    m = bare_manipulation(node=node, _active=False)
    m._ik_solution_sub = None
    m._ik_solution_fk_sub = None
    m._fk_pose_sub = None
    m._arm_state_sub = None
    m._arm_status_sub = None
    return m, node


def test_start_creates_subscriptions_exactly_once(monkeypatch):
    m, node = lifecycle_manipulation(monkeypatch)
    m.start()
    assert node.subscriptions_created == SUBSCRIPTION_COUNT
    m.start()
    assert node.subscriptions_created == SUBSCRIPTION_COUNT  # idempotent
    assert m._active is True


def test_stop_parks_executor_but_never_destroys_subscriptions(monkeypatch):
    # Destroying a subscription under the spinning private executor races
    # _take_subscription (InvalidHandle → rmw_zenoh SIGABRT). stop() must gate
    # callbacks and park the executor, keeping every handle alive.
    m, node = lifecycle_manipulation(monkeypatch)
    m.start()
    m._fk_pose = SimpleNamespace()
    m._arm_state = SimpleNamespace()
    m._grip_target = -0.4
    m.stop()
    assert m._active is False
    assert m._executor is None
    assert node.subscriptions_created == SUBSCRIPTION_COUNT
    assert m._arm_state_sub is not None and m._fk_pose_sub is not None
    assert m._fk_pose is None and m._arm_state is None  # cached state cleared
    assert m._grip_target == -0.4  # a held object stays held across runs
    m.start()  # restart re-arms without re-creating subscriptions
    assert node.subscriptions_created == SUBSCRIPTION_COUNT


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
