#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Manipulation — the arm + gripper command interface for skills.

Injected as ``self.manipulation`` when a skill declares
``manipulation: Manipulation``. Every command blocks until the motion settles
and raises :class:`ArmFailed` / :class:`ArmUnhealthy` on failure; reads return
the ROS-free :class:`~brain_client.state.arm.Arm` dataclass (the same type as
the ambient ``arm:`` feed).

Arm motion is a short atomic commitment: no method here is interrupted by a
skill cancel — cancellation lands between calls — so every method is safe in
``finally``-block teardown. ``duration`` is advisory: the hardware jerk
limiter may extend it, and the joint-2 self-collision clamp can silently
alter targets near the body.

The gripper (j6) runs current-based position control: the standing position
error IS the grip force, so re-commanding j6 from its *measured* position
drops a held object. The interface therefore tracks the last *commanded* j6
(the standing grip target) and every motion default carries it — grip an
object once, then move freely.

The methods under ``_LegacyManipulationMixin`` are the released (0.6.0)
surface, kept as deprecated delegating shims for field-authored skills.
"""

import math
import threading
import time
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

import rclpy
import rclpy.executors
from geometry_msgs.msg import PoseStamped, Twist
from mars_msgs.msg import ArmStatus
from mars_msgs.srv import GotoJS, GotoJSTrajectory
from rclpy.node import Node
from rclpy.subscription import Subscription
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from std_srvs.srv import Trigger

from brain_client.state.arm import Arm, quat_to_rpy


class ArmUnhealthy(RuntimeError):
    """Servo brownout/trip/refusal — abort, don't continue limp."""


class ArmFailed(RuntimeError):
    """Arm command rejected, unreachable, or did not complete."""


@dataclass(frozen=True)
class Waypoint:
    """One stop on a cartesian path for :meth:`Manipulation.follow`.

    Positions are meters in ``base_link``; angles are radians. ``duration``
    is the seconds to travel to this waypoint from the previous one. The
    driver also spends the first segment's duration approaching waypoint 0
    from wherever the arm is, so uniform-duration paths behave exactly as
    written; a single-waypoint path is paced by its own ``duration``.
    """

    x: float
    y: float
    z: float
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    duration: float = 1.0


# All shipped skills use the new API; only field-authored skills still hit
# the shims, and each warns once per process so authors see the migration
# path without log spam.
_LEGACY_WARNINGS_ENABLED = True
_legacy_warned: set[str] = set()


class _LegacyManipulationMixin:
    """The released (0.6.0) arm API, preserved verbatim for field skills.

    Same signatures, same bool/None/dict returns, same non-blocking defaults,
    same gripper re-seed-from-measured behavior. Each method delegates to the
    shared plumbing and (once enabled) warns once per process.
    These will be removed in the 0.8.0 release.
    """

    def _warn_legacy(self, method: str, replacement: str) -> None:
        if not _LEGACY_WARNINGS_ENABLED or method in _legacy_warned:
            return
        _legacy_warned.add(method)
        msg = f"Manipulation.{method}() is deprecated; use {replacement}"
        self.logger.warning(msg)  # visible on-robot
        warnings.warn(msg, FutureWarning, stacklevel=3)  # visible in pytest/dev

    def solve_ik(
        self,
        x: float,
        y: float,
        z: float,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        timeout: float = 2.0,
    ) -> list[float] | None:
        """Deprecated: reachability is `clamp_reach()` + `move_to()` raising.

        Returns the 5 arm joint angles (no gripper), or None if IK fails.
        """
        self._warn_legacy("solve_ik", "move_to()")
        return self._solve_ik(x, y, z, roll, pitch, yaw, timeout=timeout)

    def move_to_joint_positions(
        self, joint_positions: list[float], duration: float = 3.0, blocking: bool = False
    ) -> bool:
        """Deprecated: use `move_joints()`. Requires exactly 6 joints."""
        self._warn_legacy("move_to_joint_positions", "move_joints()")
        return self._goto(list(joint_positions), duration, wait=blocking)

    def move_to_cartesian_pose(
        self,
        x: float,
        y: float,
        z: float,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        duration: float = 3.0,
        ik_timeout: float = 2.0,
        blocking: bool = False,
        gripper_position: float | None = None,
    ) -> bool:
        """Deprecated: use `move_to()` (verified, raising, grip-preserving)."""
        self._warn_legacy("move_to_cartesian_pose", "move_to()")
        joint_positions = self.solve_ik(x, y, z, roll, pitch, yaw, timeout=ik_timeout)
        if joint_positions is None:
            self.logger.error("Failed to solve IK for target pose")
            return False
        if len(joint_positions) == 5:
            # 0.6.0 default: re-seed j6 from the measured position (this is
            # the drop-the-object footgun move_to() fixes; preserved here).
            if gripper_position is not None:
                joint_positions.append(gripper_position)
            else:
                joint_positions.append(self._measured_gripper() or 0.0)
        return self._goto(joint_positions, duration, wait=blocking)

    def move_cartesian_trajectory(
        self,
        poses: list[dict],
        segment_duration: float = 1.0,
        segment_durations: list[float] | None = None,
        ik_timeout: float = 2.0,
        gripper_position: float | None = None,
    ) -> bool:
        """Deprecated: use `follow()` with `Waypoint`s."""
        self._warn_legacy("move_cartesian_trajectory", "follow()")
        if len(poses) < 2:
            self.logger.error("[Manipulation] Need at least 2 poses for trajectory")
            return False
        if self.torque_enabled is False:
            self.logger.error("[Manipulation] Arm torque is disabled")
            return False

        if gripper_position is None:
            self._spin_briefly(count=5, timeout_sec=0.01)
            gripper_position = self._measured_gripper() or 0.0

        waypoint_joints = []
        for i, p in enumerate(poses):
            joints = self.solve_ik(
                p["x"],
                p["y"],
                p["z"],
                p.get("roll", 0.0),
                p.get("pitch", 0.0),
                p.get("yaw", 0.0),
                timeout=ik_timeout,
            )
            if joints is None:
                self.logger.error(f"[Manipulation] IK failed for trajectory pose {i}: {p}")
                return False
            if len(joints) == 5:
                joints.append(gripper_position)
            waypoint_joints.append(joints)

        if segment_durations is not None:
            seg_durations = list(segment_durations)
        else:
            seg_durations = [segment_duration] * (len(waypoint_joints) - 1)
        return self._send_trajectory(waypoint_joints, seg_durations)

    def open_gripper(self, percent: float = 100.0, duration: float = 0.5, blocking: bool = False) -> bool:
        """Deprecated: use `gripper_open()` (blocking, verifying, raising)."""
        self._warn_legacy("open_gripper", "gripper_open()")
        percent = max(0.0, min(100.0, percent))
        target = self.GRIPPER_CLOSED + (self.GRIPPER_OPEN - self.GRIPPER_CLOSED) * (percent / 100.0)
        for attempt in (1, 2):
            if not self._command_gripper(target, duration, blocking):
                return False
            # Can only verify a blocking move, and only when the target itself
            # clears the shut threshold.
            if not blocking or target < self._GRIPPER_SHUT_J6:
                return True
            self._spin_briefly(count=5, timeout_sec=0.01)
            j6 = self._measured_gripper()
            if j6 is None or j6 >= self._GRIPPER_SHUT_J6:
                return True
            if attempt == 1:
                self.logger.warning(f"Gripper did not open (j6={j6:.3f}); rebooting servos, then retrying")
                self.recover()
        self.logger.error("Gripper did not open (servo tripped shut)")
        return False

    def close_gripper(self, strength: float = 0.0, duration: float = 0.5, blocking: bool = False) -> bool:
        """Deprecated: use `gripper_close()` (blocking, raising)."""
        self._warn_legacy("close_gripper", "gripper_close()")
        strength = min(abs(strength), self.GRIPPER_MAX_STRENGTH)
        return self._command_gripper(self.GRIPPER_CLOSED - strength, duration, blocking)

    def get_current_end_effector_pose(self) -> dict | None:
        """Deprecated: use the `pose` property (typed Arm, raising)."""
        self._warn_legacy("get_current_end_effector_pose", "the pose property")
        self._spin_briefly()
        if self._fk_pose is None:
            self.logger.warn("No FK pose available yet")
            return None
        pose = self._fk_pose.pose
        return {
            "position": {"x": pose.position.x, "y": pose.position.y, "z": pose.position.z},
            "orientation": {
                "x": pose.orientation.x,
                "y": pose.orientation.y,
                "z": pose.orientation.z,
                "w": pose.orientation.w,
            },
            "frame_id": self._fk_pose.header.frame_id,
        }

    def get_current_orientation_rpy(self) -> dict | None:
        """Deprecated: use `pose.rpy` / `pose.pitch`."""
        self._warn_legacy("get_current_orientation_rpy", "pose.rpy")
        if self._fk_pose is None:
            self.logger.warn("No FK pose available yet")
            return None
        q = self._fk_pose.pose.orientation
        roll, pitch, yaw = quat_to_rpy(q.x, q.y, q.z, q.w)
        return {"roll": roll, "pitch": pitch, "yaw": yaw}

    def spin_node_to_refresh_topics(self, count: int = 10, timeout_sec: float = 0.001):
        """Deprecated internal: state feeds refresh on their own executor."""
        self._warn_legacy("spin_node_to_refresh_topics", "nothing — reads are already live")
        self._spin_briefly(count=count, timeout_sec=timeout_sec)


class Manipulation(_LegacyManipulationMixin):
    """Arm + gripper command interface. See the module docstring for the
    contract: blocking, raising, grip-preserving, teardown-safe."""

    # --- hardware truths (public constants) ---
    # Gripper joint j6, radians: 0 = closed, 0.85 = fully open.
    GRIPPER_CLOSED = 0.0
    GRIPPER_OPEN = 0.85
    # Squeezing past the closed stop by more than this overcurrent-trips the
    # servo on a real object (0.7 and 0.8 both tripped on hardware; recovery
    # needs a reboot).
    GRIPPER_MAX_STRENGTH = 0.6
    # Below this j6 the claw is still (tripped) shut after an open command.
    _GRIPPER_SHUT_J6 = 0.10

    # joints 1-6 = base yaw, shoulder, elbow, wrist pitch, wrist roll, gripper.
    # Folded rest with j4 lifted so the gripper clears the floor (verified live:
    # ee_link z ~0.042 m). j1/j2 clamp to their limits, so this is what the arm
    # actually reaches and holds.
    REST = [1.5708, -1.2195, 1.5723, 0.30, 0.0, 0.0031]
    ZERO = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    # Grasp reach box (base_link m).
    REACH_X = (0.22, 0.40)
    REACH_Y = (-0.10, 0.10)

    def __init__(self, node: Node, logger, lazy: bool = False):
        """
        Initialize the manipulation interface.

        Args:
            node: ROS2 node for creating publishers/subscribers/clients
            logger: Logger for status messages
            lazy: If True, defer subscription creation until start() is called
        """
        self.node = rclpy.create_node(f"{node.get_name()}_manipulation_interface")
        self.logger = logger
        # The private executor spins only while a skill is active
        # (start()..stop()). While parked, messages arriving on the
        # always-alive subscriptions just rotate in the bounded rmw queues at
        # no Python cost — dispatching them through an executor costs ~half a
        # Jetson core at the ~600 msgs/s these feeds add up to.
        self._executor: rclpy.executors.SingleThreadedExecutor | None = None
        self._executor_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._ik_lock = threading.Lock()

        # Publishers
        self._ik_target_pub = self.node.create_publisher(Twist, "/ik_delta", 10)

        # Cached state
        self._ik_solution: JointState | None = None
        self._ik_solution_fk: PoseStamped | None = None
        self._fk_pose: PoseStamped | None = None
        self._arm_state: JointState | None = None
        # Torque state from two sources: local service results and the
        # driver's /mars/arm/status heartbeat (0.2 Hz, catches out-of-band
        # toggles from the webapp). torque_enabled returns the newer.
        self._torque_enabled: bool | None = None
        self._torque_stamp = 0.0
        self._status_torque: bool | None = None
        self._status_stamp = 0.0
        # The standing grip target: the last COMMANDED j6. Under current-based
        # position control the standing position error is the grip force, so
        # motion defaults carry this value instead of re-reading the measured
        # (stalled) position, which would release a held object.
        self._grip_target: float | None = None

        # Subscription handles (created once in start(); kept for the node's
        # lifetime — destroying one while the private executor thread spins
        # races _take_subscription and crashes the process with InvalidHandle).
        # stop() instead gates the callbacks via _active and parks the
        # executor.
        self._active = False
        self._ik_solution_sub: Subscription | None = None
        self._ik_solution_fk_sub: Subscription | None = None
        self._fk_pose_sub: Subscription | None = None
        self._arm_state_sub: Subscription | None = None
        self._arm_status_sub: Subscription | None = None

        if not lazy:
            self.start()

        # Service client for joint space control. We create the client once
        # here, but deliberately avoid any blocking wait_for_service calls
        # to keep the executor responsive.
        self._goto_js_client = self.node.create_client(GotoJS, "/mars/arm/goto_js_v2")
        self._goto_js_traj_client = self.node.create_client(GotoJSTrajectory, "/mars/arm/goto_js_trajectory")

        # Service clients for torque control
        self._torque_on_client = self.node.create_client(Trigger, "/mars/arm/torque_on")
        self._torque_off_client = self.node.create_client(Trigger, "/mars/arm/torque_off")
        self._reboot_servos_client = self.node.create_client(Trigger, "/mars/arm/reboot")

        self.logger.info("Manipulation initialized")

    # --- lifecycle (framework surface, not skill API) ---

    def _spin_executor(self, executor):
        try:
            executor.spin()
        except Exception as e:
            self.logger.error(f"[Manipulation] Executor stopped unexpectedly: {e}")

    def start(self):
        """Enable arm-state feeds: spin up the private executor and create the
        subscriptions once. Safe to call multiple times."""
        with self._lifecycle_lock:
            self._active = True
            if self._executor is None:
                self._executor = rclpy.executors.SingleThreadedExecutor()
                self._executor.add_node(self.node)
                self._executor_thread = threading.Thread(
                    target=self._spin_executor, args=(self._executor,), daemon=True
                )
                self._executor_thread.start()
            if self._arm_state_sub is not None:
                return
            self._ik_solution_sub = self.node.create_subscription(
                JointState, "/ik_solution", self._ik_solution_callback, 10
            )
            self._ik_solution_fk_sub = self.node.create_subscription(
                PoseStamped, "/ik_solution_fk", self._ik_solution_fk_callback, 10
            )
            self._fk_pose_sub = self.node.create_subscription(PoseStamped, "/fk_pose", self._fk_pose_callback, 10)
            self._arm_state_sub = self.node.create_subscription(
                JointState, "/mars/arm/state", self._arm_state_callback, 10
            )
            self._arm_status_sub = self.node.create_subscription(
                ArmStatus, "/mars/arm/status", self._arm_status_callback, 10
            )

    def stop(self):
        """Deactivate arm-state feeds, park the private executor and clear cached state.

        Subscriptions are deliberately kept alive (see __init__); the callbacks
        early-return while inactive, and with the executor parked they are not
        invoked at all between skills.
        """
        with self._lifecycle_lock:
            self._active = False
            if self._executor is not None:
                self._executor.shutdown()
                if self._executor_thread is not None:
                    self._executor_thread.join(timeout=2.0)
                self._executor = None
                self._executor_thread = None
        self._ik_solution = None
        self._ik_solution_fk = None
        self._fk_pose = None
        self._arm_state = None
        # _grip_target survives on purpose: a skill can end while the claw
        # still holds an object, and the next run must not release it.

    def shutdown(self):
        """Stop the manipulation helper node and its private executor."""
        self.stop()
        self.node.destroy_node()

    def halt(self) -> None:
        """Framework brake hook (Skill._halt_interfaces on cancel/run end).

        Deliberately a no-op: the goto services cannot preempt an in-flight
        motion, arm moves are short atomic commitments, and teardown
        legitimately moves the arm after halt fires. It must NOT freeze at
        the measured joints (that would re-seed j6 and drop a held object)
        and must NOT torque off (the arm would fall).
        """
        self.logger.debug("[Manipulation] halt(): arm motions are committed; nothing to brake")

    # --- callbacks (private executor thread) ---

    def _ik_solution_callback(self, msg: JointState):
        """Store the latest IK solution."""
        if self._active:
            self.logger.debug(f"IK solution received: {msg}")
            self._ik_solution = msg

    def _ik_solution_fk_callback(self, msg: PoseStamped):
        """Store the FK of the latest IK solution (what commanded joints map to)."""
        if self._active:
            self._ik_solution_fk = msg

    def _fk_pose_callback(self, msg: PoseStamped):
        """Store the latest FK pose."""
        if self._active:
            self._fk_pose = msg

    def _arm_state_callback(self, msg: JointState):
        """Store the latest arm state (includes effort/load)."""
        if self._active:
            self._arm_state = msg

    def _arm_status_callback(self, msg):
        """Track the driver's torque flag (catches out-of-band toggles)."""
        if self._active:
            self._status_torque = bool(msg.is_torque_enabled)
            self._status_stamp = time.monotonic()

    # --- reads ---

    @property
    def last_fk_pose(self) -> PoseStamped | None:
        """The latest cached /fk_pose message, or None. No spin, no warning —
        for high-rate ambient reads (RobotStateProvider.current_arm)."""
        return self._fk_pose

    @property
    def pose(self) -> Arm:
        """Live end-effector pose (same Arm type as the ambient ``arm:`` feed).

        Waits up to 1 s for the first FK fix after start(); raises ArmFailed
        if none arrives (IK node down or feeds not started).
        """
        msg = self._fk_pose
        if msg is None:
            deadline = time.monotonic() + 1.0
            while msg is None and time.monotonic() < deadline:
                time.sleep(0.02)  # committed section: a bounded read, teardown-safe by design
                msg = self._fk_pose
            if msg is None:
                raise ArmFailed("no end-effector pose on /fk_pose — is the IK node running?")
        p = msg.pose
        return Arm(
            x=p.position.x,
            y=p.position.y,
            z=p.position.z,
            qx=p.orientation.x,
            qy=p.orientation.y,
            qz=p.orientation.z,
            qw=p.orientation.w,
            gripper=self._measured_gripper(),
            frame_id=msg.header.frame_id,
        )

    @property
    def torque_enabled(self) -> bool | None:
        """Freshest known torque state (local commands vs /mars/arm/status);
        None while neither source has reported."""
        if self._status_stamp > self._torque_stamp:
            return self._status_torque
        return self._torque_enabled

    @classmethod
    def clamp_reach(cls, x, y):
        """Clamp (x, y) into the graspable reach box."""
        return (max(cls.REACH_X[0], min(cls.REACH_X[1], x)), max(cls.REACH_Y[0], min(cls.REACH_Y[1], y)))

    # --- motion ---

    def move_to(
        self,
        x: float,
        y: float,
        z: float,
        *,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        duration: float = 1.5,
        grip: float | None = None,
        tol_xy: float | None = 0.05,
        tol_z: float | None = 0.10,
    ) -> Arm:
        """Move the end-effector to a cartesian pose; return the settled pose.

        IK + execute + FK verify. If the arm doesn't track the target within
        the tolerances, recovers (reboot + torque on) and retries once, then
        raises ArmUnhealthy. Raises ArmFailed when the pose is unreachable.

        tol_z is looser than tol_xy on purpose: a z shortfall usually means
        the fingers met the object/floor early (expected while descending),
        while xy error means the move is off target. Pass ``tol_xy=None`` /
        ``tol_z=None`` to skip that axis check (expected-contact descents).

        ``grip``: j6 to hold through the move; default carries the standing
        grip target, so a held object stays held.

        With BOTH tolerances None the move is unverified: the service result
        is trusted, failure raises ArmFailed, and no recovery runs — for
        callers whose targets legitimately settle off-pose (joint limits).
        """
        joints = self._solve_ik(x, y, z, roll, pitch, yaw)
        if joints is None:
            raise ArmFailed(f"IK found no solution for ({x:.2f}, {y:.2f}, {z:.2f})")
        target = joints + [self._grip_or(grip)]
        verified = tol_xy is not None or tol_z is not None
        for attempt in (1, 2) if verified else (1,):
            ok = self._goto(target, duration, wait=True)
            settled = self._read_pose_after_motion()
            if not verified:
                if not ok:
                    raise ArmFailed(f"arm move to ({x:.2f}, {y:.2f}, {z:.2f}) rejected or did not complete")
                if settled is None:
                    raise ArmFailed("no end-effector pose after move — is the IK node running?")
                return settled
            err_xy = math.hypot(settled.x - x, settled.y - y) if settled is not None else None
            err_z = abs(settled.z - z) if settled is not None else None
            if (
                ok
                and settled is not None
                and (tol_xy is None or (err_xy is not None and err_xy <= tol_xy))
                and (tol_z is None or (err_z is not None and err_z <= tol_z))
            ):
                return settled
            self.logger.warning(
                f"[arm] not tracking (ok={ok} err_xy={err_xy} err_z={err_z}) — "
                f"{'recovering' if attempt == 1 else 'giving up'}"
            )
            if attempt == 1:
                self.recover()
        raise ArmUnhealthy(f"arm failed to reach ({x:.2f}, {y:.2f}, {z:.2f})")

    def follow(self, waypoints: Sequence[Waypoint], *, grip: float | None = None) -> Arm:
        """Sweep through cartesian waypoints as one smooth trajectory.

        The arm interpolates linearly between waypoints with no deceleration
        at intermediate points, starting from wherever it is (the driver
        prepends the current pose). A single waypoint is a plain smooth move
        paced by its ``duration``. Returns the pose where the arm settled
        (unverified — contact along the path is legitimate); raises ArmFailed
        if IK fails for any waypoint or the trajectory is rejected/preempted.

        ``grip``: j6 to hold along the path; default carries the standing
        grip target.
        """
        wps = list(waypoints)
        if not wps:
            raise ArmFailed("follow() needs at least one waypoint")
        j6 = self._grip_or(grip)

        waypoint_joints = []
        for i, wp in enumerate(wps):
            joints = self._solve_ik(wp.x, wp.y, wp.z, wp.roll, wp.pitch, wp.yaw)
            if joints is None:
                raise ArmFailed(f"IK found no solution for waypoint {i} ({wp.x:.2f}, {wp.y:.2f}, {wp.z:.2f})")
            waypoint_joints.append(joints + [j6])

        if len(waypoint_joints) == 1:
            # Single target: the quintic single-move service honors duration
            # and decelerates smoothly (the trajectory service would pace the
            # approach with a fixed 0.5 s default instead).
            if not self._goto(waypoint_joints[0], wps[0].duration, wait=True):
                raise ArmFailed("arm move rejected or did not complete")
        else:
            seg_durations = [wp.duration for wp in wps[1:]]
            if not self._send_trajectory(waypoint_joints, seg_durations):
                raise ArmFailed("trajectory rejected, failed, or preempted")
        settled = self._read_pose_after_motion()
        if settled is None:
            raise ArmFailed("no end-effector pose after trajectory — is the IK node running?")
        return settled

    def move_joints(self, joints: Sequence[float], *, duration: float = 3.0) -> None:
        """Move to joint positions (radians), blocking.

        5 values command j1-j5 and keep the standing grip; 6 values command
        j6 too (and become the new standing grip target). Raises ArmFailed
        if the command is rejected or does not complete.
        """
        target = [float(j) for j in joints]
        if len(target) == 5:
            target.append(self._grip_or(None))
        if len(target) != 6:
            raise ArmFailed(f"expected 5 or 6 joint positions, got {len(target)}")
        if not self._goto(target, duration, wait=True):
            raise ArmFailed("arm move rejected or did not complete")

    def rest(self, *, duration: float = 3.0) -> None:
        """Fold the arm to the rest pose, keeping the grip.

        Re-commands the pose once after a short pause so the servos settle
        under load (folding while carrying shifts the load mid-move).
        """
        target = list(self.REST)
        target[5] = self._grip_or(None)
        if not self._goto(target, duration, wait=True):
            raise ArmFailed("rest move rejected or did not complete")
        time.sleep(0.3)  # committed section: teardown-safe by design, see class docstring
        if not self._goto(target, 1.0, wait=True):
            raise ArmFailed("rest settle move rejected or did not complete")

    # --- gripper ---

    def gripper_open(self, percent: float = 100.0, *, duration: float = 0.5) -> None:
        """Open the claw to ``percent`` (100 = fully open), blocking.

        Verifies the claw physically opened — the servo can overcurrent-trip
        and stay shut — and reboots + retries once if not; raises ArmUnhealthy
        when it stays shut, ArmFailed when the command itself is rejected.
        The reached opening becomes the standing grip target (nothing held).
        """
        percent = max(0.0, min(100.0, percent))
        target = self.GRIPPER_CLOSED + (self.GRIPPER_OPEN - self.GRIPPER_CLOSED) * (percent / 100.0)
        for attempt in (1, 2):
            if not self._command_gripper(target, duration, True):
                raise ArmFailed("gripper command rejected or did not complete")
            # Verification is only meaningful when the target itself clears
            # the tripped-shut threshold.
            if target < self._GRIPPER_SHUT_J6:
                return
            self._spin_briefly(count=5, timeout_sec=0.01)
            j6 = self._measured_gripper()
            if j6 is None or j6 >= self._GRIPPER_SHUT_J6:
                return
            if attempt == 1:
                self.logger.warning(f"Gripper did not open (j6={j6:.3f}); rebooting servos, then retrying")
                self.recover()
        raise ArmUnhealthy("gripper did not open (servo tripped shut)")

    def gripper_close(self, strength: float = 0.0, *, duration: float = 0.5) -> None:
        """Close the claw, blocking; raises ArmFailed on rejection.

        ``strength`` is radians past the closed stop — the grip preload on an
        object, clamped to GRIPPER_MAX_STRENGTH (beyond trips the servo). It
        becomes the standing grip target, carried by every following move.
        """
        strength = min(abs(strength), self.GRIPPER_MAX_STRENGTH)
        if not self._command_gripper(self.GRIPPER_CLOSED - strength, duration, True):
            raise ArmFailed("gripper command rejected or did not complete")

    # --- servo power / recovery ---

    def torque_on(self) -> bool:
        """Enable torque on all arm motors. Returns True if successful."""
        success = self._call_trigger(self._torque_on_client, "Torque on", "Torque enabled on arm")
        if success:
            self._torque_enabled = True
            self._torque_stamp = time.monotonic()
        return success

    def torque_off(self) -> bool:
        """Disable torque on all arm motors (arm will be limp). Returns True if successful."""
        success = self._call_trigger(self._torque_off_client, "Torque off", "Torque disabled on arm")
        if success:
            self._torque_enabled = False
            self._torque_stamp = time.monotonic()
            self._grip_target = None  # a limp claw holds nothing
        return success

    def reboot_servos(self) -> bool:
        """Reboot all arm Dynamixel servos, clearing hardware errors. Returns True if successful."""
        success = self._call_trigger(
            self._reboot_servos_client,
            "Reboot servos",
            "Servos rebooted; arm torque is disabled",
            timeout_sec=10.0,
        )
        if success:
            self._torque_enabled = False
            self._torque_stamp = time.monotonic()
            self._grip_target = None
        return success

    def recover(self) -> None:
        """Full recovery: reboot servos, re-enable torque, settle (~2.5 s).

        Preserves the standing grip target across the reboot so a mid-pick
        retry re-asserts the grip preload instead of releasing the object.
        """
        grip = self._grip_target
        self.logger.warning("[arm] recovering (reboot + torque on)")
        self.reboot_servos()
        time.sleep(2.0)  # committed section: teardown-safe by design, see class docstring
        self.torque_on()
        time.sleep(0.5)  # committed section: servo re-init settle
        self._grip_target = grip

    # --- internals ---

    def _spin_briefly(self, count: int = 10, timeout_sec: float = 0.001):
        """Yield briefly while the private executor delivers callbacks."""
        for _ in range(count):
            time.sleep(timeout_sec)

    def _measured_gripper(self) -> float | None:
        """Measured j6, or None while arm state is missing/short."""
        state = self._arm_state
        try:
            return state.position[5] if state is not None else None
        except (AttributeError, IndexError, TypeError):
            return None

    def _grip_or(self, explicit: float | None) -> float:
        """The j6 a motion should carry: explicit > standing target > measured
        (start of a run, nothing held yet) > 0.0."""
        if explicit is not None:
            return float(explicit)
        if self._grip_target is not None:
            return self._grip_target
        measured = self._measured_gripper()
        return float(measured) if measured is not None else 0.0

    def _wait_for_future(self, future, timeout_sec: float | None = None) -> bool:
        """Wait for a ROS future without spinning or re-adding this node."""
        if future.done():
            return True

        done_event = threading.Event()
        future.add_done_callback(lambda _future: done_event.set())
        return done_event.wait(timeout=timeout_sec)

    def _solve_ik(
        self,
        x: float,
        y: float,
        z: float,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        timeout: float = 2.0,
    ) -> list[float] | None:
        """IK for a cartesian pose: the 5 arm joints, or None on failure.

        The IK node's request/response runs over topics (/ik_delta in,
        /ik_solution out) with no correlation id, so requests serialize on
        _ik_lock and failure surfaces as a timeout.
        """
        with self._ik_lock:
            self._ik_solution = None
            self._ik_solution_fk = None

            target = Twist()  # /ik_delta is an ABSOLUTE pose despite the name
            target.linear.x = x
            target.linear.y = y
            target.linear.z = z
            target.angular.x = roll
            target.angular.y = pitch
            target.angular.z = yaw
            self._ik_target_pub.publish(target)

            start_time = time.time()
            while time.time() - start_time < timeout:
                self._spin_briefly(count=1, timeout_sec=0.01)
                if self._ik_solution is not None:
                    joint_positions = list(self._ik_solution.position)
                    if len(joint_positions) == 0:
                        self.logger.error("[Manipulation] IK solver returned empty solution (IK failed)")
                        return None
                    return joint_positions

        self.logger.error(f"[Manipulation] IK solution timeout after {timeout}s")
        return None

    def _goto(self, joint_positions: list[float], duration: float, wait: bool) -> bool:
        """Send a 6-joint GotoJS command; when ``wait``, block to completion.

        The commanded j6 becomes the standing grip target on success.
        """
        if len(joint_positions) != 6:
            self.logger.error(f"Expected 6 joint positions, got {len(joint_positions)}")
            return False
        if self.torque_enabled is False:
            self.logger.error("[Manipulation] Arm torque is disabled")
            return False
        if self._goto_js_client is None or not self._goto_js_client.service_is_ready():
            self.logger.error("[Manipulation] GotoJS v2 service is not ready")
            return False

        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = [float(j) for j in joint_positions]
        request.time = float(duration)

        try:
            future = self._goto_js_client.call_async(request)
            if wait and not self._await_motion_result(future, "GotoJS v2", duration + 1.0):
                return False
        except Exception as e:
            self.logger.error(f"[Manipulation] Exception calling GotoJS v2: {e}")
            return False

        self._grip_target = float(joint_positions[5])
        return True

    def _send_trajectory(self, waypoint_joints: list[list[float]], seg_durations: list[float]) -> bool:
        """Send a multi-waypoint GotoJSTrajectory and block to completion.

        The driver prepends the current measured pose as waypoint 0 (gripper
        seeded from the last commanded goal, preserving grip preload) and
        paces that approach with a copy of the first segment duration.
        """
        if self._goto_js_traj_client is None or not self._goto_js_traj_client.service_is_ready():
            self.logger.error("[Manipulation] GotoJSTrajectory service not ready")
            return False

        num_joints = len(waypoint_joints[0])
        flat_waypoints: list[float] = []
        for wj in waypoint_joints:
            flat_waypoints.extend(float(j) for j in wj)
        seg_durations = [float(d) for d in seg_durations]

        request = GotoJSTrajectory.Request()
        request.waypoints = Float64MultiArray()
        request.waypoints.data = flat_waypoints
        request.num_joints = num_joints
        request.segment_durations = seg_durations

        # + the driver's prepended approach segment (first duration, or 0.5 s)
        total_time = sum(seg_durations) + (seg_durations[0] if seg_durations else 0.5)
        try:
            future = self._goto_js_traj_client.call_async(request)
            if not self._await_motion_result(future, "GotoJSTrajectory", total_time + 5.0):
                return False
        except Exception as e:
            self.logger.error(f"[Manipulation] Exception calling GotoJSTrajectory: {e}")
            return False

        self._grip_target = float(waypoint_joints[-1][5])
        return True

    def _command_gripper(self, j6: float, duration: float, blocking: bool) -> bool:
        """Send a joint command that moves only the gripper to ``j6``."""
        if self._arm_state is None:
            self.logger.error("No arm state available")
            return False

        self._spin_briefly(count=5, timeout_sec=0.01)

        positions = list(self._arm_state.position)
        if len(positions) < 6:
            positions.extend([0.0] * (6 - len(positions)))
        positions[5] = j6
        return self._goto(positions, duration, wait=blocking)

    def _read_pose_after_motion(self) -> Arm | None:
        """Fresh-ish FK after a blocking move (10 Hz feed), or None."""
        self._spin_briefly(count=5, timeout_sec=0.01)
        msg = self._fk_pose
        if msg is None:
            return None
        p = msg.pose
        return Arm(
            x=p.position.x,
            y=p.position.y,
            z=p.position.z,
            qx=p.orientation.x,
            qy=p.orientation.y,
            qz=p.orientation.z,
            qw=p.orientation.w,
            gripper=self._measured_gripper(),
            frame_id=msg.header.frame_id,
        )

    def _await_motion_result(self, future, name: str, timeout_sec: float) -> bool:
        """Block on a motion-service future; return True iff it completed successfully."""
        if not self._wait_for_future(future, timeout_sec=timeout_sec):
            self.logger.error(f"[Manipulation] {name} call timed out")
            return False
        result = future.result()
        if result is None:
            self.logger.error(f"[Manipulation] {name} call timed out")
            return False
        if not result.success:
            self.logger.error(f"[Manipulation] {name} returned failure")
            return False
        return True

    def _call_trigger(self, client, action_name: str, success_msg: str, timeout_sec: float = 2.0) -> bool:
        """Call a std_srvs/Trigger service, log the outcome, and return whether it succeeded."""
        if not client.service_is_ready():
            self.logger.error(f"[Manipulation] {action_name} service not ready")
            return False

        try:
            future = client.call_async(Trigger.Request())
            if not self._wait_for_future(future, timeout_sec=timeout_sec):
                self.logger.error(f"{action_name} service call timed out")
                return False
            result = future.result()
            if result is None:
                self.logger.error(f"{action_name} service call timed out")
                return False
            if not result.success:
                self.logger.error(f"{action_name} failed: {result.message}")
                return False
            self.logger.info(success_msg)
            return True
        except Exception as e:
            self.logger.error(f"Exception calling {action_name}: {e}")
            return False
