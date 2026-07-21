# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Handle the cloud agent's VisionAgentOutput: chat + drive the next primitive.

Validates the incoming payload, surfaces thoughts/speech/anticipation via the chat
manager, handles stop/cancel of the current task, applies local-navigation pose
compensation, and asks the runner to start the next task.
"""

from __future__ import annotations

import json
import math
import traceback

from brain_client.perception import pose as pose_math
from brain_client.transport.messages import VisionAgentOutput

_NAV_TO_POSITION = "innate-os/navigate_to_position"


class VisionOutputHandler:
    def __init__(self, node, state, *, runner, chat, gaze, pose_tracker):
        self._logger = node.get_logger()
        self._state = state
        self._runner = runner
        self._chat = chat
        self._gaze = gaze
        self._pose = pose_tracker

    def handle_message(self, msg) -> None:
        """ws handler entry point: validate, optionally log, then dispatch."""
        try:
            self._logger.info("[BrainClient] Received VisionAgentOutput")
            if not self._state.is_brain_active:
                self._logger.warn("[BrainClient] Brain is not active. Skipping VisionAgentOutput.")
                self._bounce_dropped_task(msg.payload, "the brain is not active")
                return
            if not self._state.primitives_registered:
                self._logger.warn("[BrainClient] Primitives not registered. Skipping VisionAgentOutput.")
                self._bounce_dropped_task(msg.payload, "skills were mid (re-)registration")
                return

            # HOTFIX: accept next_task with a "name" field by aliasing it to "type".
            next_task = msg.payload.get("next_task")
            if next_task is not None and "name" in next_task:
                next_task["type"] = next_task["name"]
                next_task.pop("name", None)

            payload = VisionAgentOutput.model_validate(msg.payload)

            if self._state.log_everything:
                self._chat.emit("vision_agent_output", json.dumps(msg.payload), speak=False)

            self._handle_output(payload)
        except Exception as e:
            self._logger.error(f"Error processing vision output: {e}. Traceback: {traceback.format_exc()}")

    def _bounce_dropped_task(self, raw_payload, why: str) -> None:
        """A dropped trigger must never be dropped SILENTLY: the cloud marks
        the primitive as running when it SENDS the task (optimistically, with
        no timeout of its own) and only a terminal lifecycle message clears
        it. Skipping a task without answering pins the agent in "the tool has
        already been called and has to finish" until a lucky stop_current_task
        (INN-711 tail — see PR #533 for the sibling never-started paths).
        Typical hit: a task computed from a pre-stop image landing during a
        stop, or during a registration round trip."""
        try:
            task = (raw_payload or {}).get("next_task") or {}
            primitive_id = task.get("primitive_id")
            if not primitive_id:
                return  # nothing the cloud is waiting on
            self._runner.report_start_failure(
                primitive_name=str(task.get("type") or task.get("name") or "unknown"),
                primitive_id=primitive_id,
                reason=f"The robot dropped this task: {why}.",
            )
        except Exception as e:  # noqa: BLE001 -- best-effort courtesy reply
            self._logger.warn(f"[BrainClient] Could not report dropped task to the cloud: {e}")

    def _handle_output(self, payload: VisionAgentOutput) -> None:
        execute_now = True

        if payload.stop_current_task:
            self._logger.info("[BrainClient] Stop signal received.")
            if self._runner.has_active_goal:
                if payload.next_task is not None:
                    self._logger.info(f"Storing pending task: {payload.next_task.type}")
                    self._runner.store_pending(payload.next_task)
                    execute_now = False
                self._runner.cancel_active_goal()
            else:
                self._logger.info("[BrainClient] Stop received but no goal handle active.")

        if payload.thoughts:
            self._chat.emit("robot_thoughts", payload.thoughts, speak=False)
        if payload.to_tell_user:
            self._chat.emit("robot", payload.to_tell_user)
        if payload.anticipation:
            self._chat.emit("robot_anticipation", payload.anticipation, speak=False)

        if execute_now and payload.next_task is not None:
            self._runner.clear_pending()
            self._start_next_task(payload.next_task)
        elif not execute_now and payload.next_task is not None:
            self._logger.info("[BrainClient] Next task stored, waiting for cancellation to complete.")
        else:
            self._runner.clear_pending()
            self._logger.info("[BrainClient] No next task provided or task is pending.")

    def _start_next_task(self, task) -> None:
        self._logger.info(f"[BrainClient] Next task: {task.type}")
        skill_id = self._state.registry.resolve_skill_id(task.type)
        if skill_id is None:
            self._runner.report_start_failure(
                primitive_name=task.type,
                primitive_id=task.primitive_id,
                reason=f"Unknown skill '{task.type}' — not in the registered skill set.",
            )
            return

        self._gaze.pause()
        inputs = self._maybe_compensate(skill_id, task.inputs)
        self._runner.start_task(skill_id, task.primitive_id, inputs)

    def _maybe_compensate(self, skill_id: str, inputs: dict) -> dict:
        """Apply local-nav motion compensation for navigate_to_position(local_frame)."""
        if not (skill_id == _NAV_TO_POSITION and inputs.get("local_frame", False)):
            return inputs
        if self._state.pose_at_image_send is None:
            return inputs
        current = self._pose.current_pose_xyt()
        if current is None:
            self._logger.warn("[PoseCompensation] Could not get current pose for compensation")
            return inputs
        delta = pose_math.compute_pose_delta(self._state.pose_at_image_send, current)
        adjusted = pose_math.adjust_local_nav_command(inputs, delta)
        self._logger.info(
            f"[PoseCompensation] local nav adjusted by "
            f"fwd={delta[0]:.2f}m lat={delta[1]:.2f}m rot={math.degrees(delta[2]):.1f}°"
        )
        return adjusted
