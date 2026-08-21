# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import threading
import time
from collections.abc import Callable, Iterator

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile

from innate import Odometry, Skill, SkillCancelled, SkillFailed, SkillReturn, resource

# Frame local (robot-relative) goals are resolved into before being sent to
# Nav2. Must match the mapfree costmap's global_frame (mars_nav costmap.yaml)
# and Odometry.frame_id, which is what resolves them.
LOCAL_GOAL_FIXED_FRAME = "odom"
_POLL_INTERVAL_S = 0.1
_TERMINAL_STATUSES = frozenset(
    {
        GoalStatus.STATUS_SUCCEEDED,
        GoalStatus.STATUS_CANCELED,
        GoalStatus.STATUS_ABORTED,
    }
)


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
        self._client = ActionClient(skill.node, NavigateToPose, "/navigate_to_pose")

        # The exact goal this skill commands, latched so UIs can render the
        # true target (the replanned path's endpoint wiggles).
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._commanded_goal_pub = skill.node.create_publisher(PoseStamped, "/nav/commanded_goal", latched)
        self._goal_lock = threading.Lock()
        self._active_goal_handle = None
        self._active_result_future = None
        self._cancel_future = None
        self._latest_feedback = None
        # Stop must reach the external action immediately. The polling loop is
        # still the authority that unwinds the skill, while its finally block
        # waits for the outer action's terminal result before returning.
        skill.on_cancel(self._request_active_cancel)

    def _resolve_goal(self, x, y, theta, local_frame):
        if not local_frame:
            return x, y, theta, "map"
        # self.skill.odom is the odom->base_link pose the goal is relative to,
        # injected and kept current by the framework — so there is no TF buffer
        # to warm up here, which is what made a freshly built controller's
        # first local goal racy.
        base = self.skill.odom
        gx, gy, gyaw = resolve_local_goal(base.x, base.y, base.theta, x, y, theta)
        self.logger.info(f"Resolved local goal ({x}, {y}, {theta}) to ({gx:.3f}, {gy:.3f}, {gyaw:.3f})")
        return gx, gy, gyaw, LOCAL_GOAL_FIXED_FRAME

    def go_to_position(
        self,
        x: float,
        y: float,
        theta: float,
        local_frame: bool,
        *,
        timeout_s: float | None = None,
        safety_check: Callable[[], None] | None = None,
    ) -> None:
        """Navigate to the goal, blocking until Nav2 finishes. Raises
        SkillFailed with a human-readable reason; a skill cancel unwinds as
        SkillCancelled with the Nav2 task cancelled."""
        if timeout_s is not None and timeout_s <= 0.0:
            raise SkillFailed("navigation timeout must be positive")
        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        goal_x, goal_y, goal_yaw, goal_frame = self._resolve_goal(x, y, theta, local_frame)
        self._check_operational(deadline, timeout_s, safety_check)

        goal_pose = PoseStamped()
        goal_pose.header.frame_id = goal_frame
        goal_pose.header.stamp = self.skill.node.get_clock().now().to_msg()
        goal_pose.pose.position.x = goal_x
        goal_pose.pose.position.y = goal_y
        goal_pose.pose.orientation.z = math.sin(goal_yaw / 2.0)
        goal_pose.pose.orientation.w = math.cos(goal_yaw / 2.0)
        self._commanded_goal_pub.publish(goal_pose)

        goal = NavigateToPose.Goal()
        goal.pose = goal_pose
        # The root router treats this field as a planner selector.
        goal.behavior_tree = "mapfree" if local_frame else "navigation"

        send_future = None
        goal_handle = None
        result_future = None
        dispatched = False
        terminal = False
        initial_distance = -1.0
        last_distance = -1.0
        last_recoveries = 0
        last_progress_log = 0.0
        said_close_to_goal = False
        try:
            # Never use BasicNavigator's synchronous setup helpers here: their
            # wait_for_server/spin calls have no cancellation or deadline path.
            while not self._client.server_is_ready():
                self._poll(deadline, timeout_s, safety_check)

            self._check_operational(deadline, timeout_s, safety_check)
            self._latest_feedback = None
            send_future = self._client.send_goal_async(goal, feedback_callback=self._on_feedback)
            dispatched = True
            while not send_future.done():
                self._poll(deadline, timeout_s, safety_check)
            self._check_operational(deadline, timeout_s, safety_check)

            goal_handle = send_future.result()
            if goal_handle is None or not goal_handle.accepted:
                terminal = True
                raise SkillFailed("navigation goal was rejected")

            result_future = goal_handle.get_result_async()
            self._set_active_goal(goal_handle, result_future)
            # Close the race where Stop landed just before _set_active_goal.
            self._check_operational(deadline, timeout_s, safety_check)

            while not result_future.done():
                self._poll(deadline, timeout_s, safety_check)
                feedback = self._latest_feedback
                if feedback is None:
                    continue
                try:
                    distance = float(feedback.distance_remaining)
                    last_recoveries = int(feedback.number_of_recoveries)
                except (AttributeError, TypeError, ValueError):
                    continue
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

            # Cancellation wins a same-tick race with a terminal result.
            self._check_operational(deadline, timeout_s, safety_check)
            result_response = result_future.result()
            status = result_response.status
            terminal = status in _TERMINAL_STATUSES
            if status == GoalStatus.STATUS_SUCCEEDED:
                return
            if status == GoalStatus.STATUS_CANCELED:
                raise SkillCancelled("navigation was canceled")
            detail = f"Nav2 reported {self._status_name(status)}"
            if last_distance >= 0.0:
                detail += f" with {last_distance:.2f}m still to go"
            if last_recoveries > 0:
                detail += f" after {last_recoveries} recovery attempt{'s' if last_recoveries != 1 else ''}"
            raise SkillFailed(detail + " — the route may be blocked or the robot may be stuck")
        except (SkillCancelled, SkillFailed):
            raise
        except Exception as exc:
            raise SkillFailed(f"navigation action failed: {exc}") from exc
        finally:
            if dispatched and not terminal:
                self._cancel_dispatched_goal(send_future, goal_handle, result_future)
            self._clear_active_goal(goal_handle)

    def _check_operational(self, deadline, timeout_s, safety_check) -> None:
        self.skill.check_cancelled()
        if safety_check is not None:
            safety_check()
        if deadline is not None and time.monotonic() >= deadline:
            raise SkillFailed(f"navigation timed out after {timeout_s:.0f}s")

    def _poll(self, deadline, timeout_s, safety_check) -> None:
        self._check_operational(deadline, timeout_s, safety_check)
        self.skill.sleep(_POLL_INTERVAL_S)

    def _on_feedback(self, feedback_message) -> None:
        self._latest_feedback = getattr(feedback_message, "feedback", None)

    def _set_active_goal(self, goal_handle, result_future) -> None:
        with self._goal_lock:
            if self._active_goal_handle is not goal_handle:
                self._cancel_future = None
            self._active_goal_handle = goal_handle
            self._active_result_future = result_future

    def _clear_active_goal(self, goal_handle) -> None:
        with self._goal_lock:
            if goal_handle is not None and self._active_goal_handle is not goal_handle:
                return
            self._active_goal_handle = None
            self._active_result_future = None
            self._cancel_future = None

    def _request_active_cancel(self) -> None:
        with self._goal_lock:
            goal_handle = self._active_goal_handle
            result_future = self._active_result_future
            if goal_handle is None or result_future is None or result_future.done() or self._cancel_future is not None:
                return
            try:
                self._cancel_future = goal_handle.cancel_goal_async()
            except Exception as exc:
                self.logger.error(f"Failed to request navigation cancellation: {exc}")

    @staticmethod
    def _wait_committed(future) -> None:
        """Wait without consulting the skill cancel latch.

        Once a goal request has been sent, teardown is a committed safety
        section: returning early could leave Nav2 driving. The run node remains
        on the skills server's executor, so action futures continue progressing.
        """
        if future.done():
            return
        done = threading.Event()
        future.add_done_callback(lambda _future: done.set())
        while not future.done():
            done.wait(_POLL_INTERVAL_S)

    def _cancel_dispatched_goal(self, send_future, goal_handle, result_future) -> None:
        # A cancel can land before goal acceptance. Settle that response first;
        # if it was accepted, cancel and wait for the OUTER result. The router
        # keeps that result pending until its internal Nav2 goal is terminal.
        if goal_handle is None:
            self._wait_committed(send_future)
            try:
                goal_handle = send_future.result()
            except Exception as exc:
                self.logger.error(f"Navigation goal response failed during cancellation: {exc}")
                return
        if goal_handle is None or not goal_handle.accepted:
            return
        if result_future is not None and result_future.done():
            try:
                response = result_future.result()
                if response.status in _TERMINAL_STATUSES:
                    return
            except Exception as exc:
                self.logger.error(f"Navigation result failed during cancellation; requesting it again: {exc}")
            result_future = None
        if result_future is None:
            result_future = goal_handle.get_result_async()
        self._set_active_goal(goal_handle, result_future)
        self._request_active_cancel()
        self._wait_committed(result_future)

        with self._goal_lock:
            cancel_future = self._cancel_future
        if cancel_future is not None and cancel_future.done():
            try:
                response = cancel_future.result()
                if not getattr(response, "goals_canceling", []):
                    self.logger.error("Navigation action rejected the cancel request; waited for terminal result")
            except Exception as exc:
                self.logger.error(f"Navigation cancel response failed: {exc}")

    @staticmethod
    def _status_name(status: int) -> str:
        names = {
            GoalStatus.STATUS_UNKNOWN: "UNKNOWN",
            GoalStatus.STATUS_ACCEPTED: "ACCEPTED",
            GoalStatus.STATUS_EXECUTING: "EXECUTING",
            GoalStatus.STATUS_CANCELING: "CANCELING",
            GoalStatus.STATUS_SUCCEEDED: "SUCCEEDED",
            GoalStatus.STATUS_CANCELED: "CANCELED",
            GoalStatus.STATUS_ABORTED: "ABORTED",
        }
        return names.get(status, str(status))

    def destroy(self):
        """Destroy entities explicitly; the throwaway run node is destroyed next."""
        try:
            self._client.destroy()
        except Exception as exc:
            self.logger.warning(f"Error destroying navigation action client: {exc}")
        try:
            self.skill.node.destroy_publisher(self._commanded_goal_pub)
        except Exception as exc:
            self.logger.warning(f"Error destroying commanded-goal publisher: {exc}")


class NavigateToPosition(Skill):
    """Use when you need to navigate the robot to the specified position
    using provided x, y coordinates (meters), and theta_degrees (yaw) IN DEGREES.
    If local_frame is set to false, it navigates to a specific point in the map.
    If local_frame is set to true, it navigates locally, where the robot is currently (0,0)"""

    odom: Odometry
    """Resolves local_frame goals; see Nav2Controller._resolve_goal."""

    @resource
    def controller(self) -> Iterator[Nav2Controller]:
        controller = Nav2Controller(self)
        yield controller
        controller.destroy()

    def guidelines(self):
        return (
            "Move the robot to a position given x, y (meters) and theta_degrees (yaw IN DEGREES). "
            "Prefer local_frame=true: coordinates are then relative to where the robot stands now — "
            "robot at (0,0) facing theta=0, x forward, y left (e.g. turn around: x=0, y=0, theta_degrees=180). "
            "Use local_frame=false only to reach absolute coordinates in the frame your pose is reported in."
        )

    def execute(
        self,
        x: float,
        y: float,
        theta_degrees: float = 0.0,
        local_frame: bool = False,
        **legacy,
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
