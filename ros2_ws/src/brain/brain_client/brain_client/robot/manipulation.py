#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Manipulation - Provides IK and arm control capabilities to skills.

This interface allows skills to:
1. Request inverse kinematics solutions for Cartesian poses
2. Command the arm to move to joint positions
3. Get current end-effector pose (forward kinematics)
"""

import math
import threading
import time

import rclpy
import rclpy.executors
from geometry_msgs.msg import PoseStamped, Twist
from mars_msgs.srv import GotoJS, GotoJSTrajectory
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from std_srvs.srv import Trigger


class ArmUnhealthy(RuntimeError):
    """Servo brownout/refusal — abort, don't continue limp."""


class ArmFailed(RuntimeError):
    """Joint/cartesian command rejected or did not complete."""


class Manipulation:
    """
    Interface for arm manipulation using IK and joint control.

    Injected as ``self.manipulation`` when a skill declares
    ``manipulation: Manipulation``; the framework constructs it. Provides
    high-level methods for skills to control the arm without needing to know
    the details of ROS topics and services.
    """

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
        self._executor = None
        self._executor_thread = None
        self._lifecycle_lock = threading.Lock()
        self._ik_lock = threading.Lock()

        # Publishers
        self._ik_target_pub = self.node.create_publisher(Twist, "/ik_delta", 10)

        # Cached state
        self._ik_solution = None
        self._ik_solution_fk = None
        self._fk_pose = None
        self._arm_state = None
        self._torque_enabled = None

        # Subscription handles (created once in start(); kept for the node's
        # lifetime — destroying one while the private executor thread spins
        # races _take_subscription and crashes the process with InvalidHandle).
        # stop() instead gates the callbacks via _active and parks the
        # executor.
        self._active = False
        self._ik_solution_sub = None
        self._ik_solution_fk_sub = None
        self._fk_pose_sub = None
        self._arm_state_sub = None

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
                self._executor_thread.join(timeout=2.0)
                self._executor = None
                self._executor_thread = None
        self._ik_solution = None
        self._ik_solution_fk = None
        self._fk_pose = None
        self._arm_state = None

    def shutdown(self):
        """Stop the manipulation helper node and its private executor."""
        self.stop()
        self.node.destroy_node()

    def spin_node_to_refresh_topics(self, count: int = 10, timeout_sec: float = 0.001):
        """Yield briefly while the manipulation helper executor processes callbacks."""
        for _ in range(count):
            time.sleep(timeout_sec)

    def _wait_for_future(self, future, timeout_sec: float | None = None) -> bool:
        """Wait for a ROS future without spinning or re-adding this node."""
        if future.done():
            return True

        done_event = threading.Event()
        future.add_done_callback(lambda _future: done_event.set())
        return done_event.wait(timeout=timeout_sec)

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

    @property
    def last_fk_pose(self) -> PoseStamped | None:
        """The latest cached /fk_pose message, or None. No spin, no warning —
        for high-rate ambient reads (RobotStateProvider.current_arm)."""
        return self._fk_pose

    def get_current_end_effector_pose(self) -> dict | None:
        """
        Get the current end-effector pose in Cartesian space.

        Returns:
            dict with keys: 'position' (x, y, z), 'orientation' (x, y, z, w)
            or None if no pose is available
        """
        self.spin_node_to_refresh_topics()

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
        """
        Get the current end-effector orientation as roll/pitch/yaw.

        Returns:
            dict with keys: 'roll', 'pitch', 'yaw' (in radians)
            or None if no pose is available
        """
        if self._fk_pose is None:
            self.logger.warn("No FK pose available yet")
            return None

        import math

        pose = self._fk_pose.pose
        x, y, z, w = pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w

        # Convert quaternion to roll, pitch, yaw
        sinr_cosp = 2 * (w * x + y * z)
        cosr_cosp = 1 - 2 * (x * x + y * y)
        roll = math.atan2(sinr_cosp, cosr_cosp)

        sinp = 2 * (w * y - z * x)
        if abs(sinp) >= 1:
            pitch = math.copysign(math.pi / 2, sinp)
        else:
            pitch = math.asin(sinp)

        siny_cosp = 2 * (w * z + x * y)
        cosy_cosp = 1 - 2 * (y * y + z * z)
        yaw = math.atan2(siny_cosp, cosy_cosp)

        return {"roll": roll, "pitch": pitch, "yaw": yaw}

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
        """
        Solve inverse kinematics for a target Cartesian pose.

        Args:
            x, y, z: Target position in meters (relative to base_link)
            roll, pitch, yaw: Target orientation in radians
            timeout: Maximum time to wait for IK solution in seconds

        Returns:
            List of joint angles in radians, or None if IK fails
        """
        with self._ik_lock:
            # Clear previous solution
            self._ik_solution = None
            self._ik_solution_fk = None

            # Publish target pose to IK node
            target = Twist()
            target.linear.x = x
            target.linear.y = y
            target.linear.z = z
            target.angular.x = roll
            target.angular.y = pitch
            target.angular.z = yaw

            self._ik_target_pub.publish(target)

            # Wait for the private executor thread to deliver the IK solution callback.
            start_time = time.time()
            iteration = 0
            while time.time() - start_time < timeout:
                self.spin_node_to_refresh_topics(count=1, timeout_sec=0.01)

                if self._ik_solution is not None:
                    joint_positions = list(self._ik_solution.position)

                    # Validate that we received a non-empty solution
                    if len(joint_positions) == 0:
                        self.logger.error("[Manipulation] IK solver returned empty solution (IK failed)")
                        return None

                    return joint_positions

                iteration += 1

        self.logger.error(f"[Manipulation] IK solution timeout after {timeout}s ({iteration} iterations)")
        return None

    def move_to_joint_positions(
        self, joint_positions: list[float], duration: float = 3.0, blocking: bool = False
    ) -> bool:
        """
        Move the arm to specified joint positions using smooth trajectory.

        Args:
            joint_positions: List of 6 joint angles in radians
            duration: Trajectory duration in seconds
            blocking: If True, block until motion completes

        Returns:
            True if command was successful, False otherwise
        """
        if len(joint_positions) != 6:
            self.logger.error(f"Expected 6 joint positions, got {len(joint_positions)}")
            return False
        if self._torque_enabled is False:
            self.logger.error("[Manipulation] Arm torque is disabled")
            return False

        # Ensure GotoJS client is available
        if self._goto_js_client is None:
            self.logger.error("[Manipulation] GotoJS v2 client is not initialized")
            return False

        # Non-blocking check for service readiness
        if not self._goto_js_client.service_is_ready():
            self.logger.error("[Manipulation] GotoJS v2 service is not ready")
            return False

        # Build request
        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = joint_positions
        request.time = float(duration)

        try:
            future = self._goto_js_client.call_async(request)
            # When blocking, wait for the service to respond (motion completion).
            if blocking and not self._await_motion_result(future, "GotoJS v2", duration + 1.0):
                return False
        except Exception as e:
            self.logger.error(f"[Manipulation] Exception calling GotoJS v2: {e}")
            return False

        return True

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
        """
        Move the arm to a Cartesian pose (combines IK solving and motion execution).

        Args:
            x, y, z: Target position in meters (relative to base_link)
            roll, pitch, yaw: Target orientation in radians
            duration: Trajectory duration in seconds
            ik_timeout: Maximum time to wait for IK solution
            blocking: If True, block until motion completes
            gripper_position: Target gripper joint position in radians.
                If None (default), uses the current actual gripper position.

        Returns:
            True if successful, False otherwise
        """
        # Reintroduce IK solving, but with solve_ik no longer performing
        # nested ROS spins inside the loop.
        joint_positions = self.solve_ik(x, y, z, roll, pitch, yaw, timeout=ik_timeout)
        if joint_positions is None:
            self.logger.error("Failed to solve IK for target pose")
            return False

        # IK returns 5 joints, but we need 6 - append gripper position
        if len(joint_positions) == 5:
            if gripper_position is not None:
                current_gripper = gripper_position
            elif self._arm_state is not None and len(self._arm_state.position) >= 6:
                current_gripper = self._arm_state.position[5]
            else:
                current_gripper = 0.0
            joint_positions.append(current_gripper)

        return self.move_to_joint_positions(
            joint_positions,
            duration=duration,
            blocking=blocking,
        )

    def move_cartesian_trajectory(
        self,
        poses: list[dict],
        segment_duration: float = 1.0,
        segment_durations: list[float] | None = None,
        ik_timeout: float = 2.0,
        gripper_position: float | None = None,
    ) -> bool:
        """
        Move the arm through a list of Cartesian poses as one smooth trajectory.

        Each pose is a dict with keys: x, y, z, roll, pitch, yaw.
        The arm linearly interpolates between consecutive joint-space waypoints
        at the servo rate with no deceleration at intermediate points.

        Args:
            poses: List of dicts, each with {x, y, z, roll, pitch, yaw}.
            segment_duration: Time in seconds for each segment between
                consecutive waypoints (used when segment_durations is None).
            segment_durations: Optional list of per-segment durations.
                Overrides segment_duration when provided.
            ik_timeout: Maximum time to wait for each IK solution.
            gripper_position: Target gripper joint position in radians.
                If None, uses the current actual gripper position.

        Returns:
            True if successful, False otherwise.
        """
        if len(poses) < 2:
            self.logger.error("[Manipulation] Need at least 2 poses for trajectory")
            return False
        if self._torque_enabled is False:
            self.logger.error("[Manipulation] Arm torque is disabled")
            return False

        # Resolve gripper position once
        if gripper_position is None:
            self.spin_node_to_refresh_topics(count=5, timeout_sec=0.01)
            if self._arm_state is not None and len(self._arm_state.position) >= 6:
                gripper_position = self._arm_state.position[5]
            else:
                gripper_position = 0.0

        # Solve IK for every pose
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
            # Append gripper (IK returns 5 joints)
            if len(joints) == 5:
                joints.append(gripper_position)
            waypoint_joints.append(joints)

        # Check service readiness
        if self._goto_js_traj_client is None:
            self.logger.error("[Manipulation] GotoJSTrajectory client not initialized")
            return False
        if not self._goto_js_traj_client.service_is_ready():
            self.logger.error("[Manipulation] GotoJSTrajectory service not ready")
            return False

        # Build flat waypoint array and segment durations
        num_joints = len(waypoint_joints[0])
        flat_waypoints = []
        for wj in waypoint_joints:
            flat_waypoints.extend(wj)

        if segment_durations is not None:
            seg_durations = list(segment_durations)
        else:
            seg_durations = [segment_duration] * (len(waypoint_joints) - 1)

        request = GotoJSTrajectory.Request()
        request.waypoints = Float64MultiArray()
        request.waypoints.data = flat_waypoints
        request.num_joints = num_joints
        request.segment_durations = seg_durations

        total_time = sum(seg_durations) + segment_duration  # extra for current->first
        try:
            future = self._goto_js_traj_client.call_async(request)
            if not self._await_motion_result(future, "GotoJSTrajectory", total_time + 5.0):
                return False
        except Exception as e:
            self.logger.error(f"[Manipulation] Exception calling GotoJSTrajectory: {e}")
            return False

        return True

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

    def torque_on(self) -> bool:
        """Enable torque on all arm motors. Returns True if successful."""
        success = self._call_trigger(self._torque_on_client, "Torque on", "Torque enabled on arm")
        if success:
            self._torque_enabled = True
        return success

    def torque_off(self) -> bool:
        """Disable torque on all arm motors (arm will be limp). Returns True if successful."""
        success = self._call_trigger(self._torque_off_client, "Torque off", "Torque disabled on arm")
        if success:
            self._torque_enabled = False
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
        return success

    # Gripper position constants (radians)
    GRIPPER_CLOSED = 0.0
    GRIPPER_OPEN = 0.85
    # Below this j6 the claw is still (tripped) shut after an open command.
    GRIPPER_SHUT_J6 = 0.10
    # Squeezing past the closed stop by more than this overcurrent-trips the
    # servo on a real object (0.7 and 0.8 both tripped on hardware; recovery
    # needs a reboot).
    GRIPPER_MAX_STRENGTH = 0.6

    def _command_gripper(self, j6: float, duration: float, blocking: bool) -> bool:
        """Send a joint command that moves only the gripper to ``j6``."""
        if self._arm_state is None:
            self.logger.error("No arm state available")
            return False

        self.spin_node_to_refresh_topics(count=5, timeout_sec=0.01)

        positions = list(self._arm_state.position)
        if len(positions) < 6:
            positions.extend([0.0] * (6 - len(positions)))

        positions[5] = j6
        return self.move_to_joint_positions(positions, duration=duration, blocking=blocking)

    def open_gripper(self, percent: float = 100.0, duration: float = 0.5, blocking: bool = False) -> bool:
        """
        Open the gripper (joint6).

        When blocking, verifies the claw actually opened — the servo can
        overcurrent-trip and stay shut — and reboots + retries once if not.

        Args:
            percent: How open to make the gripper, 0-100% (default 100% = fully open)
            duration: Time for gripper motion
            blocking: If True, block until motion completes and verify the claw opened

        Returns:
            True if successful, False otherwise
        """
        percent = max(0.0, min(100.0, percent))
        target = self.GRIPPER_CLOSED + (self.GRIPPER_OPEN - self.GRIPPER_CLOSED) * (percent / 100.0)
        for attempt in (1, 2):
            if not self._command_gripper(target, duration, blocking):
                return False
            # Can only verify a blocking move, and only when the target itself
            # clears the shut threshold.
            if not blocking or target < self.GRIPPER_SHUT_J6:
                return True
            self.spin_node_to_refresh_topics(count=5, timeout_sec=0.01)
            j6 = self.gripper_j6(self._arm_state)
            if j6 is None or j6 >= self.GRIPPER_SHUT_J6:
                return True
            if attempt == 1:
                self.logger.warning(f"Gripper did not open (j6={j6:.3f}); rebooting servos, then retrying")
                self.recover()
        self.logger.error("Gripper did not open (servo tripped shut)")
        return False

    def close_gripper(self, strength: float = 0.0, duration: float = 0.5, blocking: bool = False) -> bool:
        """
        Close the gripper (joint6).

        Args:
            strength: Additional radians to close beyond 0.0 for a firmer grip
                (e.g. 0.1 = close to -0.1 rad). Clamped to GRIPPER_MAX_STRENGTH.
            duration: Time for gripper motion
            blocking: If True, block until motion completes

        Returns:
            True if successful, False otherwise
        """
        strength = min(abs(strength), self.GRIPPER_MAX_STRENGTH)
        return self._command_gripper(self.GRIPPER_CLOSED - strength, duration, blocking)

    # --- arm primitives ---
    # Skills call these as methods: self.manipulation.go(...), .move_checked(...).

    # Grasp reach box (base_link m).
    REACH_X = (0.22, 0.40)
    REACH_Y = (-0.10, 0.10)

    # joints 1-6 = base yaw, shoulder, elbow, wrist pitch, wrist roll, gripper.
    # Folded rest with j4 lifted so the gripper clears the floor (verified live:
    # ee_link z ~0.042 m). j1/j2 clamp to their limits, so this is what the arm
    # actually reaches and holds.
    REST = [1.5708, -1.2195, 1.5723, 0.30, 0.0, 0.0031]
    ZERO = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    @classmethod
    def clamp_reach(cls, x, y):
        return (max(cls.REACH_X[0], min(cls.REACH_X[1], x)), max(cls.REACH_Y[0], min(cls.REACH_Y[1], y)))

    def ee_xyz(self):
        """FK end-effector (x,y,z), or None."""
        pose = self.get_current_end_effector_pose()
        try:
            p = pose["position"]
            return (float(p["x"]), float(p["y"]), float(p["z"]))
        except (KeyError, TypeError):
            return None

    @staticmethod
    def gripper_j6(joint_states):
        """Gripper joint j6, or None."""
        try:
            return joint_states.position[5] if joint_states else None
        except (AttributeError, IndexError, TypeError):
            return None

    @staticmethod
    def with_gripper(joints, j6):
        """Copy of joints with j6 set (no-op if j6 is None)."""
        out = list(joints)
        if j6 is not None:
            out[5] = float(j6)
        return out

    @classmethod
    def rest_joints(cls, joint_states=None, keep_gripper=True):
        """Joint target for the folded rest pose."""
        if keep_gripper:
            return cls.with_gripper(cls.REST, cls.gripper_j6(joint_states))
        return list(cls.REST)

    def go(self, joints, duration=3.0, *, times=1, pause=0.3, logger=None):
        """Move to joint positions, blocking for each move. Raises ArmFailed.
        ``times`` + ``pause`` repeat the move so the arm can settle (pick
        teardown). Deliberately not cancel-interruptible: an arm move is a
        short atomic commitment, and this must stay safe in teardown paths
        that run after a cancel."""
        logger = logger or self.logger
        joints = list(joints)
        for i in range(times):
            logger.info(f"[arm] joints {[round(j, 3) for j in joints]} over {duration}s")
            if not self.move_to_joint_positions(joint_positions=joints, duration=duration, blocking=True):
                raise ArmFailed("Failed to send arm command")
            if pause and i + 1 < times:
                time.sleep(pause)

    def recover(self, logger=None):
        """Reboot servos + torque on (clears overcurrent trip / brownout)."""
        (logger or self.logger).warning("[arm] recovering (reboot + torque on)")
        self.reboot_servos()
        time.sleep(2.0)
        self.torque_on()
        time.sleep(0.5)

    def move_checked(self, x, y, z, pitch, duration=1.5, tol_xy=0.05, tol_z=0.10, gripper=None, logger=None):
        """Cartesian move; verify FK within per-axis tolerances, recover+retry once, else raise.

        tol_z is looser than tol_xy on purpose: a z shortfall usually means the
        fingers met the object/floor early (expected while descending), while xy
        error means the grasp is off target.

        ``gripper``: j6 command to hold through the move. Pass the grip goal
        when moving with an object in the fingers — the default (None) re-seeds
        j6 from the measured position, and under current-based position control
        (mode 5) the standing position error IS the grip force, so re-seeding
        it releases the object.
        """
        logger = logger or self.logger
        for attempt in (1, 2):
            ok = self.move_to_cartesian_pose(
                x=x,
                y=y,
                z=z,
                roll=0.0,
                pitch=pitch,
                yaw=0.0,
                duration=duration,
                blocking=True,
                gripper_position=gripper,
            )
            cur = self.ee_xyz()
            err_xy = math.hypot(cur[0] - x, cur[1] - y) if cur is not None else None
            err_z = abs(cur[2] - z) if cur is not None else None
            if ok and err_xy is not None and err_z is not None and err_xy <= tol_xy and err_z <= tol_z:
                return True
            logger.warning(
                f"[arm] not tracking (ok={ok} err_xy={err_xy} err_z={err_z}) — "
                f"{'recovering' if attempt == 1 else 'giving up'}"
            )
            if attempt == 1:
                self.recover(logger)
        raise ArmUnhealthy(f"arm failed to reach ({x:.2f},{y:.2f},{z:.2f})")
