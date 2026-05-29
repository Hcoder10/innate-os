#!/usr/bin/env python3
"""Simulator arm service shim.

The Genesis simulator owns the visual arm.  This ROS node exposes the same
joint-space services as the physical arm node, then streams joint commands to
``/mars/arm/commands`` so the simulator can animate the URDF joints.
"""

from __future__ import annotations

import threading
import time
from typing import Iterable

import rclpy
from maurice_msgs.srv import GotoJS, GotoJSTrajectory
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from std_srvs.srv import Trigger


ARM_JOINT_COUNT = 6


class SimArmServices(Node):
    def __init__(self) -> None:
        super().__init__("sim_arm_services")

        self.declare_parameter("command_topic", "/mars/arm/commands")
        self.declare_parameter("state_topic", "/mars/arm/state")
        self.declare_parameter("command_hz", 50.0)

        self._command_topic = str(self.get_parameter("command_topic").value)
        self._state_topic = str(self.get_parameter("state_topic").value)
        self._command_hz = max(1.0, float(self.get_parameter("command_hz").value))

        self._callback_group = ReentrantCallbackGroup()
        self._position_lock = threading.Lock()
        self._latest_positions = [0.0] * ARM_JOINT_COUNT
        self._torque_enabled = True

        self._command_pub = self.create_publisher(
            Float64MultiArray,
            self._command_topic,
            10,
        )
        self._state_sub = self.create_subscription(
            JointState,
            self._state_topic,
            self._state_callback,
            10,
            callback_group=self._callback_group,
        )

        for service_name in ("/mars/arm/goto_js", "/mars/arm/goto_js_v2"):
            self.create_service(
                GotoJS,
                service_name,
                self._handle_goto_js,
                callback_group=self._callback_group,
            )

        self.create_service(
            GotoJSTrajectory,
            "/mars/arm/goto_js_trajectory",
            self._handle_goto_js_trajectory,
            callback_group=self._callback_group,
        )

        self.create_service(
            Trigger,
            "/mars/arm/torque_on",
            self._handle_torque_on,
            callback_group=self._callback_group,
        )
        self.create_service(
            Trigger,
            "/mars/arm/torque_off",
            self._handle_torque_off,
            callback_group=self._callback_group,
        )
        self.create_service(
            Trigger,
            "/mars/arm/reboot",
            self._handle_reboot,
            callback_group=self._callback_group,
        )

        self.get_logger().info(
            "Sim arm services ready: /mars/arm/goto_js, "
            "/mars/arm/goto_js_v2, /mars/arm/goto_js_trajectory"
        )

    def _state_callback(self, msg: JointState) -> None:
        if len(msg.position) < ARM_JOINT_COUNT:
            return
        with self._position_lock:
            self._latest_positions = [float(v) for v in msg.position[:ARM_JOINT_COUNT]]

    def _current_positions(self) -> list[float]:
        with self._position_lock:
            return self._latest_positions.copy()

    def _coerce_target(self, values: Iterable[float]) -> list[float] | None:
        try:
            target = [float(v) for v in values]
        except (TypeError, ValueError):
            return None
        if len(target) < ARM_JOINT_COUNT:
            return None
        return target[:ARM_JOINT_COUNT]

    def _publish_positions(self, positions: list[float]) -> None:
        msg = Float64MultiArray()
        msg.data = positions
        self._command_pub.publish(msg)

    def _stream_to_target(self, target: list[float], duration: float) -> None:
        duration = max(0.0, float(duration))
        start = self._current_positions()
        if duration <= 0.0:
            self._publish_positions(target)
            with self._position_lock:
                self._latest_positions = target.copy()
            return

        dt = 1.0 / self._command_hz
        steps = max(1, int(duration * self._command_hz))
        for step in range(1, steps + 1):
            t = step / steps
            smooth = t * t * (3.0 - 2.0 * t)
            positions = [
                start[i] + smooth * (target[i] - start[i])
                for i in range(ARM_JOINT_COUNT)
            ]
            self._publish_positions(positions)
            time.sleep(dt)

        with self._position_lock:
            self._latest_positions = target.copy()

    def _handle_goto_js(self, request: GotoJS.Request, response: GotoJS.Response):
        if not self._torque_enabled:
            self.get_logger().warning("Rejecting arm goto: torque is disabled")
            response.success = False
            return response

        target = self._coerce_target(request.data.data)
        if target is None:
            self.get_logger().error("Rejecting arm goto: expected 6 joint values")
            response.success = False
            return response

        self.get_logger().info(
            f"Sim arm goto target={target} duration={float(request.time):.2f}s"
        )
        self._stream_to_target(target, float(request.time))
        response.success = True
        return response

    def _handle_goto_js_trajectory(
        self,
        request: GotoJSTrajectory.Request,
        response: GotoJSTrajectory.Response,
    ):
        if not self._torque_enabled:
            self.get_logger().warning("Rejecting arm trajectory: torque is disabled")
            response.success = False
            return response

        num_joints = int(request.num_joints or ARM_JOINT_COUNT)
        try:
            flat_waypoints = [float(v) for v in request.waypoints.data]
        except (TypeError, ValueError):
            self.get_logger().error("Rejecting arm trajectory: non-numeric waypoint")
            response.success = False
            return response
        if num_joints <= 0 or len(flat_waypoints) < num_joints:
            self.get_logger().error("Rejecting arm trajectory: no waypoints")
            response.success = False
            return response

        waypoints = [
            self._coerce_target(flat_waypoints[i : i + num_joints])
            for i in range(0, len(flat_waypoints), num_joints)
        ]
        if any(waypoint is None for waypoint in waypoints):
            self.get_logger().error("Rejecting arm trajectory: invalid waypoint")
            response.success = False
            return response

        durations = [float(v) for v in request.segment_durations]
        initial_duration = durations[0] if durations else 1.0
        self.get_logger().info(
            f"Sim arm trajectory waypoints={len(waypoints)} "
            f"segments={len(durations)}"
        )
        for index, waypoint in enumerate(waypoints):
            if index == 0:
                duration = initial_duration
            elif index - 1 < len(durations):
                duration = durations[index - 1]
            else:
                duration = initial_duration
            self._stream_to_target(waypoint, duration)

        response.success = True
        return response

    def _handle_torque_on(self, _request: Trigger.Request, response: Trigger.Response):
        self._torque_enabled = True
        self.get_logger().info("Sim arm torque enabled")
        response.success = True
        response.message = "Sim arm torque enabled."
        return response

    def _handle_torque_off(self, _request: Trigger.Request, response: Trigger.Response):
        self._torque_enabled = False
        self.get_logger().info("Sim arm torque disabled")
        response.success = True
        response.message = "Sim arm torque disabled; motion commands will be rejected."
        return response

    def _handle_reboot(self, _request: Trigger.Request, response: Trigger.Response):
        self._torque_enabled = True
        self.get_logger().info("Sim arm rebooted")
        response.success = True
        response.message = "Sim arm rebooted; torque enabled."
        return response


def main(args=None) -> None:
    rclpy.init(args=args)
    node = SimArmServices()
    executor = MultiThreadedExecutor(num_threads=2)
    try:
        rclpy.spin(node, executor=executor)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
