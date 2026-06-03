#!/usr/bin/env python3
"""Skills action server: executes skills dispatched as ExecuteSkill goals.

Focused on the action/execution flow — code-skill execution, physical-skill
delegation to behavior_server (incl. cancellation + CLI worker), and the goal
lifecycle. Skill discovery/metadata/publishing/reload lives in
:mod:`brain_client.skills.catalog`; live robot-state injection lives in
:mod:`brain_client.skills.robot_state`. This node wires those together and owns
the action server.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import rclpy
from brain_messages.action import ExecuteBehavior, ExecuteSkill
from brain_messages.srv import CreatePhysicalSkill, ReloadSkillsAgents
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.node import Node
from std_srvs.srv import Trigger

from brain_client.perception.camera_provider import CameraProvider
from brain_client.robot.head import HeadInterface
from brain_client.robot.manipulation import ManipulationInterface
from brain_client.robot.mobility import MobilityInterface
from brain_client.skills.catalog import SkillRepository
from brain_client.skills.cli_bridge import SkillCliBridge, SkillCliGoalHandle
from brain_client.skills.robot_state import RobotStateProvider
from brain_client.skills.types import RobotStateType, SkillResult


class SkillsActionServer(Node):
    def __init__(self):
        super().__init__("skills_action_server")

        # Camera images handled by a dedicated lightweight node (own thread)
        self._camera_node = CameraProvider()

        self.declare_parameter("cmd_vel_topic", "/cmd_vel")
        self.cmd_vel_topic = self.get_parameter("cmd_vel_topic").value
        self.declare_parameter("head_position_topic", "/mars/head/set_position")
        self.head_position_topic = self.get_parameter("head_position_topic").value
        self.declare_parameter("head_current_position_topic", "/mars/head/current_position")
        self.head_current_position_topic = self.get_parameter("head_current_position_topic").value
        self.declare_parameter("simulator_mode", False)
        self.simulator_mode = self.get_parameter("simulator_mode").value

        # Robot interfaces injected into skills.
        self.manipulation = ManipulationInterface(self, self.get_logger(), lazy=True)
        self.mobility = MobilityInterface(self, self.get_logger(), self.cmd_vel_topic)
        self.head = HeadInterface(self, self.get_logger(), self.head_position_topic)

        # Robot-state provider (subscriptions, interface injection, live state).
        self.robot_state = RobotStateProvider(
            self,
            self._camera_node,
            manipulation=self.manipulation,
            mobility=self.mobility,
            head=self.head,
            head_current_position_topic=self.head_current_position_topic,
        )

        # Skill catalog (discovery, metadata, publishing, reload).
        self.catalog = SkillRepository(
            self,
            interface_injector=self.robot_state.inject_required_interfaces,
            simulator_mode=self.simulator_mode,
        )

        # Behavior delegation for physical skills.
        self._behavior_client = ActionClient(self, ExecuteBehavior, "/behavior/execute")
        self._behavior_goal_lock = threading.Lock()
        self._behavior_goal_handles = {}
        self._behavior_goal_cancel_requested = set()
        self._behavior_goal_cancel_sent = set()

        self._action_server = ActionServer(
            self,
            ExecuteSkill,
            "execute_skill",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
        )

        # CLI skill worker (runs CLI-submitted skills off the main spin thread).
        self._cli_skill_tasks = queue.Queue()
        self._cli_skill_worker_stop = threading.Event()
        self._cli_skill_worker = threading.Thread(target=self._run_cli_skill_worker, daemon=True)
        self._cli_skill_worker.start()
        self._skill_cli_bridge = SkillCliBridge(self.get_logger(), self._submit_cli_skill)

        # Services delegate to the catalog.
        self._reload_srv = self.create_service(Trigger, "/brain/reload_primitives", self._handle_reload_skills)
        self._create_physical_skill_srv = self.create_service(
            CreatePhysicalSkill, "/brain/create_physical_skill", self._handle_create_physical_skill
        )
        self._reload_skills_srv = self.create_service(
            ReloadSkillsAgents, "/brain/reload_skills", self._handle_reload_skills_agents
        )

        self.get_logger().debug("Skills Action Server has started.")
        self.get_logger().info(f"Total skills available: {self.catalog.code_count + self.catalog.physical_count}")
        self.catalog.publish_skills_list()
        self.catalog.start_watcher()

    # ================= service handlers (delegate to catalog) =================
    def _handle_reload_skills(self, request, response):
        try:
            self.catalog.reload_all()
            response.success = True
            response.message = f"Reloaded {self.catalog.code_count} code, {self.catalog.physical_count} physical skills"
        except Exception as e:
            response.success = False
            response.message = f"Failed to reload skills: {e}"
        return response

    def _handle_create_physical_skill(self, request, response):
        success, message, skill_dir, skill_id = self.catalog.create_physical_skill(request.name)
        response.success = success
        response.message = message
        response.skill_directory = skill_dir
        response.skill_id = skill_id
        return response

    def _handle_reload_skills_agents(self, request, response):
        try:
            skill_ids = list(request.skills) if request.skills else []
            reloaded = self.catalog.reload_selective(skill_ids)
            response.success = True
            response.reloaded_skills = reloaded
            response.reloaded_agents = []  # Skills server doesn't handle agents
            response.message = f"Reloaded {len(reloaded)} skills: {reloaded}"
        except Exception as e:
            response.success = False
            response.message = f"Failed to reload skills: {e}"
            response.reloaded_skills = []
            response.reloaded_agents = []
        return response

    # ================= action lifecycle =================
    def goal_callback(self, goal_request):
        self.get_logger().debug(f"Received goal for skill: '{goal_request.skill_type}'")
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        try:
            skill_type = goal_handle.request.skill_type
            code_entry = self.catalog.get_code_skill(skill_type)
            is_physical = self.catalog.get_physical_skill(skill_type) is not None

            if code_entry is not None:
                _name, instance = code_entry
                self.get_logger().debug(f"Canceling code skill: {skill_type}")
                instance.cancel()
            elif is_physical:
                self.get_logger().debug(f"Canceling physical skill: {skill_type}")
                self._request_behavior_goal_cancel(goal_handle, skill_type)
            else:
                self.get_logger().warning(f"Unknown skill type: {skill_type}")
        except Exception as e:
            self.get_logger().error(f"Error in cancel_callback: {str(e)}")
            self.get_logger().debug("Attempting to cancel all code skills")
            for sid, (_name, instance) in self.catalog.all_code_skills():
                try:
                    instance.cancel()
                except Exception as cancel_error:
                    self.get_logger().error(f"Error canceling {sid}: {str(cancel_error)}")
        return CancelResponse.ACCEPT

    def execute_callback(self, goal_handle):
        self.get_logger().debug(f"[SAS] execute_callback ENTER for skill: '{goal_handle.request.skill_type}'")
        try:
            inputs = json.loads(goal_handle.request.inputs)
        except Exception as e:
            self.get_logger().error(f"Invalid JSON for inputs: {str(e)}")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False, message="Invalid inputs JSON", success_type=SkillResult.FAILURE.value
            )

        skill_type = goal_handle.request.skill_type
        if self.catalog.get_code_skill(skill_type) is not None:
            return self._execute_code_skill(goal_handle, skill_type, inputs)
        if self.catalog.get_physical_skill(skill_type) is not None:
            return self._execute_physical_skill(goal_handle, skill_type, inputs)

        self.get_logger().error(f"Skill '{skill_type}' not available")
        self.get_logger().error(f"Available skills: {self.catalog.all_skill_ids()}")
        goal_handle.abort()
        return ExecuteSkill.Result(
            success=False, message="Skill not available", skill_type=skill_type, success_type=SkillResult.FAILURE.value
        )

    # ================= execution =================
    def _execute_code_skill(self, goal_handle, skill_type, inputs):
        entry = self.catalog.get_code_skill(skill_type)
        if entry is None:
            self.get_logger().error(f"Code skill '{skill_type}' disappeared during reload")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message=f"Skill '{skill_type}' was removed during a concurrent reload",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )
        _name, skill = entry

        def _publish_feedback(update_message: str, image_b64: str = None):
            feedback_msg = ExecuteSkill.Feedback()
            feedback_msg.feedback = update_message
            feedback_msg.image_b64 = image_b64 or ""
            goal_handle.publish_feedback(feedback_msg)
            self.get_logger().debug(f"Published feedback for '{skill_type}': {update_message}")

        skill.set_feedback_callback(_publish_feedback)

        required_states = skill.get_required_robot_states()
        needs_camera = required_states and (
            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64 in required_states
            or RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64 in required_states
        )

        try:
            self.robot_state.start_subscriptions()
            if needs_camera:
                self._camera_node.start()
            self.robot_state.update_skill_robot_state(skill)
            if required_states:
                self.robot_state.begin_continuous_updates(skill)
                self.get_logger().info(f"Started continuous state updates for '{skill_type}' at 50Hz")

            result_message, result_status = skill.execute(**inputs)

            if result_status == SkillResult.SUCCESS:
                self.get_logger().info(f"Skill '{skill_type}' succeeded: {result_message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True, message=result_message, skill_type=skill_type, success_type=SkillResult.SUCCESS.value
                )
            elif result_status == SkillResult.CANCELLED:
                self.get_logger().info(f"Skill '{skill_type}' cancelled: {result_message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True,
                    message=result_message,
                    skill_type=skill_type,
                    success_type=SkillResult.CANCELLED.value,
                )
            else:  # SkillResult.FAILURE
                self.get_logger().info(f"Skill '{skill_type}' failed: {result_message}")
                goal_handle.abort()
                return ExecuteSkill.Result(
                    success=False, message=result_message, skill_type=skill_type, success_type=SkillResult.FAILURE.value
                )
        except Exception as e:
            self.get_logger().error(f"Error executing skill: {str(e)}")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False, message=str(e), skill_type=skill_type, success_type=SkillResult.FAILURE.value
            )
        finally:
            self.robot_state.end_continuous_updates()
            if needs_camera:
                self._camera_node.stop()
            self.robot_state.stop_subscriptions()

    def _execute_physical_skill(self, goal_handle, skill_type, inputs):
        self.get_logger().info(f"Delegating physical skill '{skill_type}' to behavior_server")
        physical_data = self.catalog.get_physical_skill(skill_type)
        if physical_data is None:
            self.get_logger().error(f"Physical skill '{skill_type}' disappeared during reload")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message=f"Skill '{skill_type}' was removed during a concurrent reload",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )
        metadata = physical_data["metadata"]

        self.robot_state.start_subscriptions()
        try:
            if not self._behavior_client.wait_for_server(timeout_sec=5.0):
                self.get_logger().error("Behavior server not available!")
                goal_handle.abort()
                return ExecuteSkill.Result(
                    success=False,
                    message="Behavior server not available",
                    skill_type=skill_type,
                    success_type=SkillResult.FAILURE.value,
                )

            behavior_goal = ExecuteBehavior.Goal()
            behavior_goal.skill_dir = physical_data["directory"]
            behavior_goal.behavior_config = json.dumps(metadata)

            self.get_logger().info(f"Sending behavior goal to behavior_server: {skill_type}")
            send_goal_future = self._behavior_client.send_goal_async(behavior_goal)

            # CLI requests run on a background worker so the main spin loop can
            # service the behavior action response; recursive spinning from that
            # worker races the main executor and can falsely time out.
            if isinstance(goal_handle, SkillCliGoalHandle):
                goal_ready = self._wait_for_cli_future(send_goal_future, timeout_sec=10.0) == "done"
            else:
                rclpy.spin_until_future_complete(self, send_goal_future, timeout_sec=5.0)
                goal_ready = send_goal_future.done()

            if not goal_ready:
                self.get_logger().error("Timeout waiting for behavior goal acceptance")
                self._cancel_behavior_goal_when_ready(send_goal_future, skill_type)
                goal_handle.abort()
                return ExecuteSkill.Result(
                    success=False,
                    message="Timeout waiting for behavior goal acceptance",
                    skill_type=skill_type,
                    success_type=SkillResult.FAILURE.value,
                )

            behavior_goal_handle = send_goal_future.result()
            if not behavior_goal_handle.accepted:
                self.get_logger().error("Behavior goal rejected by behavior_server")
                goal_handle.abort()
                return ExecuteSkill.Result(
                    success=False,
                    message="Behavior goal rejected by behavior_server",
                    skill_type=skill_type,
                    success_type=SkillResult.FAILURE.value,
                )

            self._register_behavior_goal_handle(goal_handle, behavior_goal_handle, skill_type)
            self.get_logger().info("Behavior goal accepted, waiting for result...")

            result_future = behavior_goal_handle.get_result_async()
            if isinstance(goal_handle, SkillCliGoalHandle):
                result_wait_state = self._wait_for_cli_future(
                    result_future, server_ready_check=self._behavior_server_ready
                )
            else:
                rclpy.spin_until_future_complete(self, result_future)
                result_wait_state = "done" if result_future.done() else "pending"

            if result_wait_state == "server_unavailable":
                self.get_logger().error(f"Behavior server became unavailable while running '{skill_type}'")
                goal_handle.abort()
                return ExecuteSkill.Result(
                    success=False,
                    message="Behavior server became unavailable while waiting for result",
                    skill_type=skill_type,
                    success_type=SkillResult.FAILURE.value,
                )

            if not result_future.done():
                self.get_logger().info(f"Physical skill '{skill_type}' cancelled before behavior result was ready")
                goal_handle.canceled()
                return ExecuteSkill.Result(
                    success=True,
                    message="Physical skill cancelled",
                    skill_type=skill_type,
                    success_type=SkillResult.CANCELLED.value,
                )

            behavior_result = result_future.result().result
            if behavior_result.success:
                self.get_logger().info(f"Physical skill '{skill_type}' succeeded: {behavior_result.message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True,
                    message=behavior_result.message,
                    skill_type=skill_type,
                    success_type=SkillResult.SUCCESS.value,
                )
            if "cancel" in behavior_result.message.lower():
                self.get_logger().info(f"Physical skill '{skill_type}' cancelled: {behavior_result.message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True,
                    message=behavior_result.message,
                    skill_type=skill_type,
                    success_type=SkillResult.CANCELLED.value,
                )
            self.get_logger().error(f"Physical skill '{skill_type}' failed: {behavior_result.message}")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message=behavior_result.message,
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )
        except Exception as e:
            self.get_logger().error(f"Unexpected error executing physical skill '{skill_type}': {e}")
            try:
                goal_handle.abort()
            except Exception as abort_err:
                self.get_logger().error(f"Also failed to abort goal for '{skill_type}': {abort_err}")
            return ExecuteSkill.Result(
                success=False,
                message=f"Unexpected error executing physical skill: {e}",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )
        finally:
            self._unregister_behavior_goal_handle(goal_handle)
            self.robot_state.stop_subscriptions()

    # ================= behavior goal tracking =================
    def _skill_goal_key(self, goal_handle) -> int:
        return id(goal_handle)

    def _skill_goal_cancel_requested(self, goal_handle) -> bool:
        try:
            return bool(goal_handle.is_cancel_requested)
        except Exception:
            return False

    def _register_behavior_goal_handle(self, skill_goal_handle, behavior_goal_handle, skill_type: str) -> None:
        key = self._skill_goal_key(skill_goal_handle)
        with self._behavior_goal_lock:
            self._behavior_goal_handles[key] = behavior_goal_handle
            cancel_requested = key in self._behavior_goal_cancel_requested
        if cancel_requested or self._skill_goal_cancel_requested(skill_goal_handle):
            self.get_logger().info(f"Cancel was already requested for physical skill '{skill_type}'")
            self._request_behavior_goal_cancel(skill_goal_handle, skill_type)

    def _unregister_behavior_goal_handle(self, skill_goal_handle) -> None:
        key = self._skill_goal_key(skill_goal_handle)
        with self._behavior_goal_lock:
            self._behavior_goal_handles.pop(key, None)
            self._behavior_goal_cancel_requested.discard(key)
            self._behavior_goal_cancel_sent.discard(key)

    def _request_behavior_goal_cancel(self, skill_goal_handle, skill_type: str) -> None:
        key = self._skill_goal_key(skill_goal_handle)
        with self._behavior_goal_lock:
            self._behavior_goal_cancel_requested.add(key)
            behavior_goal_handle = self._behavior_goal_handles.get(key)
            if behavior_goal_handle is None:
                return
            if key in self._behavior_goal_cancel_sent:
                return
            self._behavior_goal_cancel_sent.add(key)
        try:
            self.get_logger().info(f"Requesting behavior_server cancel for physical skill '{skill_type}'")
            behavior_goal_handle.cancel_goal_async()
        except Exception as e:
            self.get_logger().error(f"Failed to cancel behavior goal for '{skill_type}': {e}")

    def _cancel_behavior_goal_when_ready(self, send_goal_future, skill_type: str) -> None:
        def _cancel_when_ready(future):
            try:
                behavior_goal_handle = future.result()
                if behavior_goal_handle is not None and behavior_goal_handle.accepted:
                    self.get_logger().info(f"Canceling late-accepted behavior goal for physical skill '{skill_type}'")
                    behavior_goal_handle.cancel_goal_async()
            except Exception as e:
                self.get_logger().error(f"Failed to cancel late behavior goal for '{skill_type}': {e}")

        send_goal_future.add_done_callback(_cancel_when_ready)

    # ================= CLI skill worker =================
    def _submit_cli_skill(self, task):
        goal_handle = SkillCliGoalHandle(task)
        task.set_cancel_handler(lambda: self.cancel_callback(goal_handle))
        self._cli_skill_tasks.put((task, goal_handle))

    def _run_cli_skill_worker(self):
        while not self._cli_skill_worker_stop.is_set():
            try:
                item = self._cli_skill_tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            task, goal_handle = item
            try:
                task.mark_started()
                if task.cancel_event.is_set():
                    task.set_error("Skill execution was cancelled before start")
                    continue
                try:
                    result = self.execute_callback(goal_handle)
                except Exception as e:
                    self.get_logger().error(f"Unexpected error executing CLI skill '{task.skill_type}': {e}")
                    task.set_error(f"Skill execution failed: {e}")
                    continue
                if result is None:
                    task.set_error("Skill execution returned no result")
                else:
                    task.set_result(result)
            finally:
                self._unregister_behavior_goal_handle(goal_handle)

    def _behavior_server_ready(self) -> bool:
        try:
            checker = getattr(self._behavior_client, "server_is_ready", None)
            if checker is not None:
                return bool(checker())
            return bool(self._behavior_client.wait_for_server(timeout_sec=0.0))
        except Exception as e:
            self.get_logger().error(f"Could not check behavior_server readiness: {e}")
            return False

    def _wait_for_cli_future(self, future, timeout_sec=None, server_ready_check=None):
        """Wait for a ROS future while the node executor spins in the main thread."""
        if future.done():
            return "done"
        done_event = threading.Event()
        future.add_done_callback(lambda _future: done_event.set())
        deadline = time.monotonic() + timeout_sec if timeout_sec is not None else None
        while True:
            wait_timeout = 0.2
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                wait_timeout = min(wait_timeout, remaining)
            if done_event.wait(timeout=wait_timeout):
                return "done"
            if server_ready_check is not None and not server_ready_check():
                return "server_unavailable"

    # ================= teardown =================
    def destroy(self):
        self.catalog.stop_watcher()
        if hasattr(self, "_skill_cli_bridge"):
            self._skill_cli_bridge.stop()
        self._cli_skill_worker_stop.set()
        self._cli_skill_tasks.put(None)
        self._cli_skill_worker.join(timeout=1.0)
        self._camera_node.shutdown()
        self._action_server.destroy()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    action_server = SkillsActionServer()
    try:
        while rclpy.ok():
            rclpy.spin_once(action_server, timeout_sec=1.0)
    except KeyboardInterrupt:
        pass
    action_server.destroy()
    # Guard against double-shutdown (see ws_client): avoids a teardown RCLError.
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == "__main__":
    main()
