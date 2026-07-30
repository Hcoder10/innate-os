# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time
from collections.abc import Iterator

import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from rclpy.duration import Duration
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.time import Time

# TransformException is re-exported from the compiled tf2_py module, which
# pyright cannot introspect.
from tf2_ros import TransformException  # pyright: ignore[reportAttributeAccessIssue]
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

from brain_client.common.geometry import quaternion_to_yaw
from innate import Skill, SkillCancelled, SkillFailed, SkillReturn, resource

# Frame local (robot-relative) goals are resolved into before being sent to
# Nav2. Must match the mapfree costmap's global_frame (mars_nav costmap.yaml).
LOCAL_GOAL_FIXED_FRAME = "odom"


def resolve_local_goal(base_x, base_y, base_yaw, x, y, theta):
    """Compose a base_link-relative (x, y, theta) goal with the robot's pose in
    the fixed frame, returning (gx, gy, gyaw) expressed in that fixed frame."""
    gx = base_x + x * math.cos(base_yaw) - y * math.sin(base_yaw)
    gy = base_y + x * math.sin(base_yaw) + y * math.cos(base_yaw)
    return gx, gy, base_yaw + theta


class Nav2Controller:
    def __init__(self, skill):
        self.skill = skill
        self.logger = skill.logger
        self.navigator = BasicNavigator(namespace="")
        self.navigator_mapfree = BasicNavigator(namespace="mapfree")
        self.navigator_navigation = BasicNavigator(namespace="navigation")

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self.navigator)

        # The exact goal this skill commands, latched so UIs can render the
        # true target (the replanned path's endpoint wiggles).
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._commanded_goal_pub = self.navigator.create_publisher(PoseStamped, "/nav/commanded_goal", latched)

    def _lookup_fresh_base_pose(self, timeout_sec: float = 2.0, max_age_sec: float = 1.0):
        """Latest odom->base_link transform no older than max_age_sec, or None.

        Spins the navigator node so the TF listener actually receives data —
        outside Nav2's own blocking helpers nothing services its subscriptions.
        """
        clock = self.navigator.get_clock()
        deadline = clock.now() + Duration(seconds=timeout_sec)  # pyright: ignore[reportArgumentType]
        min_stamp = clock.now() - Duration(seconds=max_age_sec)  # pyright: ignore[reportArgumentType]
        while clock.now() < deadline:
            rclpy.spin_once(self.navigator, timeout_sec=0.05)
            try:
                transform = self.tf_buffer.lookup_transform(LOCAL_GOAL_FIXED_FRAME, "base_link", Time())
            except TransformException:
                continue
            if Time.from_msg(transform.header.stamp) >= min_stamp:
                return transform
        return None

    def _resolve_goal(self, x, y, theta, local_frame):
        if not local_frame:
            return x, y, theta, "map"
        base_tf = self._lookup_fresh_base_pose()
        if base_tf is None:
            raise SkillFailed(
                "could not determine the robot's current pose "
                f"(no fresh {LOCAL_GOAL_FIXED_FRAME} transform), so the local goal could not be resolved"
            )
        base = base_tf.transform
        gx, gy, gyaw = resolve_local_goal(
            base.translation.x, base.translation.y, quaternion_to_yaw(base.rotation), x, y, theta
        )
        self.logger.info(f"Resolved local goal ({x}, {y}, {theta}) to ({gx:.3f}, {gy:.3f}, {gyaw:.3f})")
        return gx, gy, gyaw, LOCAL_GOAL_FIXED_FRAME

    def go_to_position(self, x: float, y: float, theta: float, local_frame: bool) -> None:
        """Navigate to the goal, blocking until Nav2 finishes. Raises
        SkillFailed with a human-readable reason; a skill cancel unwinds as
        SkillCancelled with the Nav2 task cancelled."""
        goal_x, goal_y, goal_yaw, goal_frame = self._resolve_goal(x, y, theta, local_frame)

        goal_pose = PoseStamped()
        goal_pose.header.frame_id = goal_frame
        goal_pose.header.stamp = self.navigator.get_clock().now().to_msg()
        goal_pose.pose.position.x = goal_x
        goal_pose.pose.position.y = goal_y
        goal_pose.pose.orientation.z = math.sin(goal_yaw / 2.0)
        goal_pose.pose.orientation.w = math.cos(goal_yaw / 2.0)
        self._commanded_goal_pub.publish(goal_pose)

        path_navigator = self.navigator_mapfree if local_frame else self.navigator_navigation
        if path_navigator.getPath(goal_pose, goal_pose, use_start=False) is None:
            raise SkillFailed(
                f"the planner found no path to ({goal_x:.2f}, {goal_y:.2f}) in the {goal_frame} frame "
                "— the goal may be unreachable, blocked, or outside the map"
            )

        self.navigator.goToPose(goal_pose, behavior_tree="mapfree" if local_frame else "navigation")

        initial_distance = -1.0
        last_distance = -1.0
        last_recoveries = 0
        last_progress_log = 0.0
        said_close_to_goal = False
        while not self.navigator.isTaskComplete():
            try:
                self.skill.sleep(0.1)
            except SkillCancelled:
                self.navigator.cancelTask()
                raise

            feedback = self.navigator.getFeedback()
            if not feedback:
                continue
            # Nav2's own distance_remaining: feedback.current_pose is in the
            # navigator's global frame while our goal may be in another.
            distance = feedback.distance_remaining
            last_recoveries = feedback.number_of_recoveries
            if distance > 0.0:
                last_distance = distance
                if initial_distance < 0.0:
                    initial_distance = distance

            now = time.monotonic()
            if now - last_progress_log >= 1.0:
                last_progress_log = now
                completion = 100.0 * (1.0 - distance / initial_distance) if initial_distance > 0.0 else 0.0
                self.logger.info(
                    f"Navigation progress: {max(0.0, min(100.0, completion)):.0f}% "
                    f"({distance:.2f}m remaining, {last_recoveries} recoveries)"
                )

            if 0.0 < distance < 0.2 and not said_close_to_goal:
                said_close_to_goal = True
                self.skill.feedback(
                    "I'm almost done with this movement, if I think I should navigate again to pursue this task"
                    ", I should stop the current primitive and start a new navigation movement."
                )

        result = self.navigator.getResult()
        if result == TaskResult.SUCCEEDED:
            return
        detail = f"Nav2 reported {getattr(result, 'name', result)}"
        if last_distance >= 0.0:
            detail += f" with {last_distance:.2f}m still to go"
        if last_recoveries > 0:
            detail += f" after {last_recoveries} recovery attempt{'s' if last_recoveries != 1 else ''}"
        raise SkillFailed(detail + " — the route may be blocked or the robot may be stuck")

    def destroy(self):
        """Destroy the navigator nodes so their graph entities disappear now,
        not at some eventual GC pass."""
        for navigator in (self.navigator, self.navigator_mapfree, self.navigator_navigation):
            try:
                # Humble's BasicNavigator.destroy_node() misses this client; its
                # live handle would keep the rcl node and graph entities alive.
                navigator.assisted_teleop_client.destroy()
            except Exception as e:
                self.logger.warning(f"Error destroying assisted_teleop client: {e}")
            try:
                navigator.destroy_node()
            except Exception as e:
                self.logger.warning(f"Error destroying navigator node: {e}")


class NavigateToPosition(Skill):
    """Use when you need to navigate the robot to the specified position
    using provided x, y coordinates (meters), and theta_degrees (yaw) IN DEGREES.
    If local_frame is set to false, it navigates to a specific point in the map.
    If local_frame is set to true, it navigates locally, where the robot is currently (0,0)"""

    @resource
    def controller(self) -> Iterator[Nav2Controller]:
        controller = Nav2Controller(self)
        yield controller
        controller.destroy()

    def execute(
        self, x: float, y: float, theta_degrees: float = 0.0, local_frame: bool = False, **legacy
    ) -> SkillReturn:
        # The tool schema speaks theta_degrees, but the cloud agent and the
        # pose-adjustment pipeline speak `theta` in radians.
        if legacy.get("theta") is not None:
            theta = float(legacy["theta"])
            theta_degrees = math.degrees(theta)
        else:
            theta = math.radians(theta_degrees)
        self.logger.info(f"Navigating to x={x}, y={y}, theta_degrees={theta_degrees}, local_frame={local_frame}")

        goal_desc = f"({x}, {y}, {theta_degrees} deg, {'local' if local_frame else 'map'} frame)"
        try:
            self.controller.go_to_position(x, y, theta, local_frame)
        except SkillFailed as e:
            self.fail(f"Navigation to {goal_desc} failed: {e}")
        return f"Reached position ({x}, {y}, {theta_degrees} deg)"
