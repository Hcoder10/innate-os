"""Primitive execution lifecycle over the ``execute_skill`` action.

Owns the action client and the execution state — the current goal handle, the
running primitive, and any task pending behind a cancellation. The "activate a
primitive" sequence (send goal -> announce -> mark running) that was duplicated in
the old node lives once here as :meth:`start_task`.
"""

from __future__ import annotations

import json

from brain_messages.action import ExecuteSkill
from rclpy.action import ActionClient

from brain_client.skills.types import SkillResult
from brain_client.transport.messages import MessageIn, MessageInType


class PrimitiveRunner:
    def __init__(self, node, ws_bridge, chat, state, *, stop_robot, on_task_finished):
        self._node = node
        self._logger = node.get_logger()
        self._ws = ws_bridge
        self._chat = chat
        self._state = state
        self._stop_robot = stop_robot
        self._on_task_finished = on_task_finished

        self.action_client = ActionClient(node, ExecuteSkill, "execute_skill")
        self.primitive_running: dict | None = None
        self._goal_handle = None
        self._pending_next_task = None

    # --- public API ---
    def start_task(self, skill_id: str, primitive_id: str | None, inputs: dict) -> None:
        """Send a goal for ``skill_id`` and mark it running (announce + status)."""
        skill_name = self._state.registry.name_for(skill_id)
        self._send_goal(skill_id, inputs)
        self._ws.send_message(
            MessageIn(
                type=MessageInType.PRIMITIVE_ACTIVATED,
                payload={"primitive_name": skill_name, "primitive_id": primitive_id},
            )
        )
        self._chat.publish_task_status(
            primitive_name=skill_name, primitive_id=primitive_id, status="running", skill_id=skill_id
        )
        self.primitive_running = {
            "primitive_name": skill_name,
            "primitive_id": primitive_id,
            "skill_id": skill_id,
        }

    @property
    def has_active_goal(self) -> bool:
        return self._goal_handle is not None

    def store_pending(self, task) -> None:
        self._pending_next_task = task

    def clear_pending(self) -> None:
        self._pending_next_task = None

    def cancel_active_goal(self, on_done=None):
        """Request cancellation of the active goal (if any). Returns the future."""
        if self._goal_handle is None:
            return None
        future = self._goal_handle.cancel_goal_async()
        if on_done is not None:
            future.add_done_callback(on_done)
        return future

    def abort_running(self) -> None:
        """Stop any running primitive without announcing an interruption (used on unregister)."""
        if self.primitive_running:
            if self._goal_handle:
                self._goal_handle.cancel_goal_async()  # fire-and-forget
                self._goal_handle = None
            self.primitive_running = None
            self._stop_robot()
        self._pending_next_task = None

    def interrupt_for_deactivation(self) -> None:
        """Cancel the running primitive and announce it interrupted (used on deactivate)."""
        if self.primitive_running and self._goal_handle:
            self._goal_handle.cancel_goal_async()  # fire-and-forget
            self._ws.send_message(
                MessageIn(
                    type=MessageInType.PRIMITIVE_INTERRUPTED,
                    payload={
                        "primitive_name": self.primitive_running["primitive_name"],
                        "primitive_id": self.primitive_running["primitive_id"],
                    },
                )
            )
            self._chat.publish_task_status(
                primitive_name=self.primitive_running["primitive_name"],
                primitive_id=self.primitive_running["primitive_id"],
                status="interrupted",
                skill_id=self.primitive_running.get("skill_id"),
            )
            self._goal_handle = None
        self.primitive_running = None
        self._pending_next_task = None

    # --- action plumbing ---
    def _send_goal(self, task_type: str, inputs: dict) -> None:
        goal_msg = ExecuteSkill.Goal()
        goal_msg.skill_type = task_type
        goal_msg.inputs = json.dumps(inputs if inputs is not None else {})
        self._logger.info(f"Sending goal for skill: {task_type} with inputs: {goal_msg.inputs}")
        if not self.action_client.wait_for_server(timeout_sec=1.0):
            self._logger.error("Primitive execution action server not available!")
            return
        future = self.action_client.send_goal_async(goal_msg, feedback_callback=self._on_feedback)
        future.add_done_callback(self._on_goal_response)

    def _on_feedback(self, feedback_wrapper) -> None:
        try:
            feedback_text = feedback_wrapper.feedback.feedback
            self._logger.info(f"Received primitive feedback: {feedback_text}")
            payload = {"feedback": feedback_text}
            if feedback_wrapper.feedback.image_b64:
                payload["image_b64"] = feedback_wrapper.feedback.image_b64
            self._ws.send_message(MessageIn(type=MessageInType.PRIMITIVE_FEEDBACK, payload=payload))
        except AttributeError:
            self._logger.error(f"Error accessing feedback text. Structure: {feedback_wrapper}")
        except Exception as e:
            self._logger.error(f"Error in _on_feedback: {e}")

    def _on_goal_response(self, future) -> None:
        goal_handle = future.result()
        if not goal_handle.accepted:
            self._logger.info("Primitive execution goal rejected.")
            if self.primitive_running:
                self._chat.publish_task_status(
                    primitive_name=self.primitive_running["primitive_name"],
                    primitive_id=self.primitive_running["primitive_id"],
                    status="failed",
                    skill_id=self.primitive_running.get("skill_id"),
                    reason="Goal rejected by action server",
                )
            self.primitive_running = None
            self._goal_handle = None
            self._on_task_finished()
            return
        self._goal_handle = goal_handle
        self._logger.info("Primitive execution goal accepted.")
        goal_handle.get_result_async().add_done_callback(self._on_result)

    def _on_cancel_response(self, future) -> None:
        cancel_response = future.result()
        self._logger.info("[BrainClient] Cancel response received.")
        if getattr(cancel_response, "goals_canceling", None):
            self._logger.info("Goal cancellation accepted.")
        else:
            self._logger.error("Goal cancellation rejected.")

    def _on_result(self, future) -> None:
        result = future.result().result
        status_color = "\033[92m" if result.success else "\033[91m"
        self._logger.info(
            f"{status_color}Primitive execution result: {result.success}, Type: {result.success_type}\033[0m"
        )
        self._goal_handle = None
        self._stop_robot()

        skill_id = result.skill_type
        primitive_name = self._state.registry.name_for(skill_id)
        primitive_id = None
        if self.primitive_running:
            primitive_id = self.primitive_running["primitive_id"]
            if self.primitive_running.get("skill_id") != skill_id:
                self._logger.warn(
                    f"Skill ID mismatch in result ({skill_id}) and running ({self.primitive_running.get('skill_id')})"
                )
        self.primitive_running = None
        self._on_task_finished()

        outgoing_msg, local_status, local_reason = self._classify_result(result, primitive_name, primitive_id)
        if outgoing_msg:
            self._ws.send_message(outgoing_msg)
            self._logger.info(f"Sent primitive status message: {outgoing_msg.type.name}")
        if local_status:
            self._chat.publish_task_status(
                primitive_name=primitive_name,
                primitive_id=primitive_id,
                status=local_status,
                skill_id=skill_id,
                reason=local_reason,
            )

        self._maybe_run_pending(result)

    def _classify_result(self, result, primitive_name, primitive_id):
        """Map an action result to (ws message, local status, reason)."""
        if result.success and result.success_type == SkillResult.SUCCESS.value:
            msg = MessageIn(
                type=MessageInType.PRIMITIVE_COMPLETED,
                payload={"primitive_name": primitive_name, "primitive_id": primitive_id},
            )
            return msg, "completed", None
        if result.success_type == SkillResult.CANCELLED.value:
            msg = MessageIn(
                type=MessageInType.PRIMITIVE_INTERRUPTED,
                payload={"primitive_name": primitive_name, "primitive_id": primitive_id},
            )
            return msg, "interrupted", None
        if not result.success or result.success_type == SkillResult.FAILURE.value:
            msg = MessageIn(
                type=MessageInType.PRIMITIVE_FAILED,
                payload={"primitive_name": primitive_name, "reason": result.message, "primitive_id": primitive_id},
            )
            return msg, "failed", result.message
        self._logger.error(
            f"Unknown primitive result combination: success={result.success}, type={result.success_type}"
        )
        return None, None, None

    def _maybe_run_pending(self, result) -> None:
        if self._pending_next_task is None:
            return
        if result.success_type in (SkillResult.CANCELLED.value, SkillResult.SUCCESS.value):
            pending = self._pending_next_task
            self._pending_next_task = None
            self._logger.info(f"Executing pending task after internal cancellation: {pending.type}")
            skill_id = self._state.registry.resolve_skill_id(pending.type)
            if skill_id is not None:
                self.start_task(skill_id, pending.primitive_id, pending.inputs)
            else:
                self._logger.warn(f"Unknown pending primitive: {pending.type}")
        else:
            self._logger.warn(
                f"Clearing pending task {self._pending_next_task.type} because previous task "
                f"finished with type {result.success_type}"
            )
            self._pending_next_task = None
