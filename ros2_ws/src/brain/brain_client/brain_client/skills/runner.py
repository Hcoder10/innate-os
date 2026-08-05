# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Primitive execution lifecycle over the ``execute_skill`` action.

Owns the action client and the execution state — the current goal handle and
the running primitive. Terminal results and feedback are reported to the brain
through the ``on_event`` / ``on_feedback`` callbacks so they land in the agent's
next turn.
"""

from __future__ import annotations

import base64
import json

from brain_messages.action import ExecuteSkill
from rclpy.action import ActionClient
from std_srvs.srv import Trigger

from brain_client.skills.lifecycle import PRIMITIVE_LIFECYCLE_STATUSES, decode_substep_feedback
from brain_client.skills.types import SkillResult


class PrimitiveRunner:
    def __init__(self, node, chat, state, *, stop_robot, on_task_finished):
        self._node = node
        self._logger = node.get_logger()
        self._chat = chat
        self._state = state
        self._stop_robot = stop_robot
        self._on_task_finished = on_task_finished

        # Bound late by the node (mutual cycle: the brain needs the runner too).
        self.on_event = lambda status, skill_name, detail=None: None
        self.on_feedback = lambda skill_name, feedback, image=None: None

        self.action_client = ActionClient(node, ExecuteSkill, "execute_skill")
        # Cancels runs this client doesn't own (manual webapp/CLI runs): the
        # execute_skill action only lets the goal's sender cancel.
        self._cancel_skill_client = node.create_client(Trigger, "/brain/cancel_skill")
        self._goal_handle = None
        # Bumped whenever the brain disowns its goal (reset/deactivation). Late
        # callbacks from a disowned goal compare against it and stand down, so
        # they can never clear a newer run's state or feed a fresh session.
        self._generation = 0

    # --- public API ---
    def start_task(self, skill_id: str, primitive_id: str | None, inputs: dict) -> None:
        """Send a goal for ``skill_id`` and mark it running.

        The local /brain/skill_status_update echo is the skills server's job (it
        publishes for every goal it runs) — announcing it here too would double
        the "running" entry in the chat.
        """
        skill_name = self._state.registry.name_for(skill_id)
        # Marked running before the goal is sent: the response and result
        # callbacks fire on the ROS thread and must find the state they clear.
        self._state.primitive_running = {
            "primitive_name": skill_name,
            "primitive_id": primitive_id,
            "skill_id": skill_id,
        }
        if not self._send_goal(skill_id, inputs):
            self._state.primitive_running = None
            self.report_start_failure(
                primitive_name=skill_name,
                primitive_id=primitive_id,
                skill_id=skill_id,
                reason="Skill execution server unavailable — the skill never started.",
            )

    def report_start_failure(self, *, primitive_name, primitive_id, reason, skill_id=None) -> None:
        """Tell the brain and the app that a task never started (no goal exists)."""
        self._logger.error(f"Primitive '{primitive_name}' failed to start: {reason}")
        self.on_event("failed", primitive_name, reason)
        self._chat.publish_task_status(
            primitive_name=primitive_name,
            primitive_id=primitive_id,
            status="failed",
            skill_id=skill_id,
            reason=reason,
        )
        if self._state.primitive_running is None and self._goal_handle is None:
            self._on_task_finished()

    @property
    def has_active_goal(self) -> bool:
        return self._goal_handle is not None

    def cancel_active_goal(self, on_done=None):
        """Request cancellation of the active goal (if any). Returns the future."""
        if self._goal_handle is None:
            return None
        future = self._goal_handle.cancel_goal_async()
        future.add_done_callback(on_done or self._on_cancel_response)
        return future

    def cancel_external(self) -> bool:
        """Ask the skills server to cancel a run this client didn't start.

        Fire-and-forget: the run's terminal event arrives like any other
        manual skill event. Returns False if the server is unreachable.
        """
        if not self._cancel_skill_client.service_is_ready():
            self._logger.error("Cannot cancel external skill run: /brain/cancel_skill unavailable")
            return False
        self._cancel_skill_client.call_async(Trigger.Request())
        return True

    def abort_running(self) -> None:
        """Stop the brain's primitive without announcing an interruption (used on reset)."""
        if self._state.primitive_running and not self._state.primitive_running.get("manual"):
            self._stop_robot()
        self.interrupt_for_deactivation()

    def interrupt_for_deactivation(self) -> None:
        """Cancel the running primitive on deactivate.

        Skips the local /brain/skill_status_update echo — the action server
        publishes "interrupted" itself once the cancellation actually lands.

        A manual (webapp/CLI) run is not the brain's to stop: it keeps running
        on the skills server, so its mirrored state is kept too — a reactivated
        brain still honors one-skill-at-a-time, and the run's own terminal
        event clears it (that handler is always on).
        """
        running = self._state.primitive_running
        if running and running.get("manual"):
            return
        self._disown_goal()
        self._state.primitive_running = None

    def _disown_goal(self) -> None:
        """Cancel the current goal and stand down its late callbacks.

        A goal whose handle hasn't resolved yet (sent moments before a reset or
        deactivation) is cancelled when it does — _on_goal_response sees the
        stale generation and cancels on arrival.
        """
        self._generation += 1
        if self._goal_handle:
            self._goal_handle.cancel_goal_async()  # fire-and-forget
            self._goal_handle = None

    # --- action plumbing ---
    def _send_goal(self, task_type: str, inputs: dict) -> bool:
        """Dispatch the goal; returns False if the action server is unavailable."""
        goal_msg = ExecuteSkill.Goal()
        goal_msg.skill_type = task_type
        goal_msg.inputs = json.dumps(inputs if inputs is not None else {})
        self._logger.info(f"Sending goal for skill: {task_type} with inputs: {goal_msg.inputs}")
        if not self.action_client.wait_for_server(timeout_sec=1.0):
            self._logger.error("Primitive execution action server not available!")
            return False
        generation = self._generation
        future = self.action_client.send_goal_async(
            goal_msg, feedback_callback=lambda msg: self._on_feedback_msg(msg, generation)
        )
        future.add_done_callback(lambda f: self._on_goal_response(f, generation))
        return True

    def _on_feedback_msg(self, feedback_wrapper, generation: int) -> None:
        if generation != self._generation:
            return  # feedback from a disowned goal
        try:
            feedback_text = feedback_wrapper.feedback.feedback
            substep = decode_substep_feedback(feedback_text)
            if substep is not None:
                self._handle_substep(substep)
                return
            self._logger.info(f"Received primitive feedback: {feedback_text}")
            image = None
            if feedback_wrapper.feedback.image_b64:
                image = base64.b64decode(feedback_wrapper.feedback.image_b64)
            running = self._state.primitive_running or {}
            self.on_feedback(running.get("primitive_name", "unknown"), feedback_text, image)
        except Exception as e:
            self._logger.error(f"Error in feedback handler: {e}")

    def _handle_substep(self, substep: dict) -> None:
        """Turn a chained child's piggybacked event into its own step in the app.

        Forwarded to the app only, deliberately NOT to the brain: the agent runs
        one primitive at a time and would read a child finishing as the parent
        finishing.
        """
        event = substep.get("event")
        if event not in PRIMITIVE_LIFECYCLE_STATUSES:
            self._logger.warn(f"Unknown substep event: {event}")
            return
        self._chat.publish_task_status(
            primitive_name=substep.get("name", ""),
            primitive_id=substep.get("primitive_id"),
            status=event,
            skill_id=substep.get("skill_id"),
            reason=substep.get("reason"),
        )
        output = substep.get("output")
        if event == "completed" and output and output.strip():
            self._chat.emit("skill_output", output, speak=False)

    def _on_goal_response(self, future, generation: int) -> None:
        goal_handle = future.result()
        if generation != self._generation:
            # The brain disowned this goal while its response was in flight:
            # cancel it now that a handle finally exists, touch nothing else.
            if goal_handle.accepted:
                goal_handle.cancel_goal_async()
            return
        if not goal_handle.accepted:
            self._logger.info("Primitive execution goal rejected.")
            if self._state.primitive_running:
                running = self._state.primitive_running
                self._chat.publish_task_status(
                    primitive_name=running["primitive_name"],
                    primitive_id=running["primitive_id"],
                    status="failed",
                    skill_id=running.get("skill_id"),
                    reason="Goal rejected by action server",
                )
                self.on_event("failed", running["primitive_name"], "Goal rejected by action server")
            self._state.primitive_running = None
            self._goal_handle = None
            self._on_task_finished()
            return
        self._goal_handle = goal_handle
        self._logger.info("Primitive execution goal accepted.")
        goal_handle.get_result_async().add_done_callback(lambda f: self._on_result(f, generation))

    def _on_cancel_response(self, future) -> None:
        cancel_response = future.result()
        self._logger.info("[BrainClient] Cancel response received.")
        if getattr(cancel_response, "goals_canceling", None):
            self._logger.info("Goal cancellation accepted.")
        else:
            self._logger.error("Goal cancellation rejected.")

    def _on_result(self, future, generation: int) -> None:
        result = future.result().result
        status_color = "\033[92m" if result.success else "\033[91m"
        self._logger.info(
            f"{status_color}Primitive execution result: {result.success}, Type: {result.success_type}\033[0m"
        )
        if generation != self._generation:
            # A disowned goal ending: a newer run (or a fresh session) may own
            # the state and the event queue now — this result concerns neither.
            return
        self._goal_handle = None
        self._stop_robot()

        skill_id = result.skill_type
        primitive_name = self._state.registry.name_for(skill_id)
        if self._state.primitive_running and self._state.primitive_running.get("skill_id") != skill_id:
            self._logger.warn(
                f"Skill ID mismatch in result ({skill_id}) and running ({self._state.primitive_running.get('skill_id')})"
            )
        self._state.primitive_running = None
        self._on_task_finished()

        is_code = self._is_code_skill(skill_id)
        # The action server publishes the terminal /brain/skill_status_update itself
        # (for every goal, not just the agent's) — only the brain event is this
        # client's job.
        status, detail = self._classify_result(result, is_code)
        if status is not None:
            self.on_event(status, primitive_name, detail)
        self._emit_skill_output(result, is_code)

    def _is_code_skill(self, skill_id: str) -> bool:
        meta = self._state.registry.primitives.get(skill_id)
        return meta is not None and meta.get("type") == "code"

    def _emit_skill_output(self, result, is_code: bool) -> None:
        """Surface a successful code skill's output in the chat (never spoken)."""
        if is_code and result.success and result.success_type == SkillResult.SUCCESS.value and result.message.strip():
            self._chat.emit("skill_output", result.message, speak=False)

    def _classify_result(self, result, is_code: bool) -> tuple[str | None, str | None]:
        """Map an action result to the brain-facing (status, detail) event."""
        if result.success and result.success_type == SkillResult.SUCCESS.value:
            output = result.message if is_code and result.message.strip() else None
            return "completed", output
        if result.success_type == SkillResult.CANCELLED.value:
            return "interrupted", None
        if not result.success or result.success_type == SkillResult.FAILURE.value:
            return "failed", result.message
        self._logger.error(
            f"Unknown primitive result combination: success={result.success}, type={result.success_type}"
        )
        return None, None
