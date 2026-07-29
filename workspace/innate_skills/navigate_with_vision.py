#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Navigate With Vision Skill — sends a natural-language navigation instruction
to the UniNavid cloud service and follows the returned action commands until
the goal is reached (or canceled).

Uses the ``navigate_instruction`` ROS 2 action server exposed by the
``innate_uninavid`` node.
"""

import threading

from action_msgs.msg import GoalStatus
from innate_cloud_msgs.action import NavigateInstruction
from rclpy.action import ActionClient

from innate import Skill, SkillResult, SkillReturn, resource

# Human-readable labels for the integer action codes returned by the server.
_ACTION_LABELS = {
    0: "STOP",
    1: "FORWARD",
    2: "LEFT",
    3: "RIGHT",
}


class _NavigateClient:
    """The UniNavid action client, built on first use.

    No explicit teardown (it defines no close/destroy/shutdown for @resource
    to call): the client lives on the run's throwaway node and dies with it
    at run end.
    """

    def __init__(self, skill: Skill):
        self._client = ActionClient(skill.node, NavigateInstruction, "/navigate_instruction")

    def wait_for_server(self, timeout_sec: float) -> bool:
        return self._client.wait_for_server(timeout_sec=timeout_sec)

    def send_goal_async(self, goal, feedback_callback=None):
        return self._client.send_goal_async(goal, feedback_callback=feedback_callback)


class NavigateWithVision(Skill):
    """Use when you want the robot to navigate using camera vision and a
    natural-language instruction (e.g. 'walk to the red chair and stop').
    The instruction is sent to the UniNavid cloud service which streams
    back movement commands until the goal is reached.
    Requires param 'instruction' (str).
    Some other examples of instructions are 'move to the nearest sofa and then stop', 'follow the human wearing black pants'."""

    def __init__(self, logger):
        super().__init__(logger)
        self._last_feedback_action: int | None = None
        self._last_feedback_stops: int = 0

    @resource
    def client(self) -> _NavigateClient:
        return _NavigateClient(self)

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, instruction: str) -> SkillReturn:
        """Send *instruction* to UniNavid and block until the goal finishes."""
        if not self.node:
            msg = "Navigation skill has no ROS node and cannot execute."
            self.logger.error(msg)
            self.feedback(msg)
            self.fail(msg)

        self.logger.info(f"[NavigateWithVision] Instruction: {instruction!r}")
        self.feedback(f"Sending instruction: {instruction}")

        # ── Wait for the action server ────────────────────────────────────────
        if not self.client.wait_for_server(timeout_sec=10.0):
            msg = "Navigation server is not available. Please try again."
            self.logger.error(msg)
            self.feedback(msg)
            self.fail(msg)

        # ── Send goal ─────────────────────────────────────────────────────────
        goal_msg = NavigateInstruction.Goal()
        goal_msg.instruction = instruction

        goal_future = self.client.send_goal_async(goal_msg, feedback_callback=self._on_feedback)

        if not self._wait_for_future(goal_future, timeout_sec=10.0):
            msg = "Navigation goal timed out waiting for acceptance."
            self.logger.error(msg)
            self.feedback(msg)
            self.fail(msg)

        goal_handle = goal_future.result()
        if goal_handle is None or not goal_handle.accepted:
            msg = "Navigation goal was rejected."
            self.logger.info(msg)
            self.feedback(msg)
            self.fail(msg)

        # A cancel must reach the action server immediately, not at the next poll.
        self.on_cancel(goal_handle.cancel_goal_async)

        self.logger.info("Goal accepted — waiting for result …")
        self.feedback("Navigation started, waiting for completion …")

        result_future = goal_handle.get_result_async()

        # Wait for the result WITHOUT re-spinning self.node — the skills_server's
        # dedicated executor already services the result/feedback callbacks. Register
        # the done-callback once and poll the Event in short slices so we can react
        # to a cancel request; registering per-iteration would pile up closures on a
        # long-running goal. Calling rclpy.spin_until_future_complete(self.node, …)
        # here would add this node to the global executor and race the dedicated one,
        # corrupting the shared wait set (RCLError "wait set index … out of bounds"
        # → SIGABRT).
        result_ready = threading.Event()
        result_future.add_done_callback(lambda _future: result_ready.set())
        while not result_ready.wait(timeout=0.25):
            if self.cancelled:
                self.logger.info("Cancel requested — forwarding to action server")
                goal_handle.cancel_goal_async()
                # Wait for the server to acknowledge the cancel.
                result_ready.wait(timeout=10.0)
                break

        if not result_future.done():
            msg = "Navigation timed out."
            self.logger.error(msg)
            self.feedback(msg)
            self.fail(msg)

        result_response = result_future.result()
        status = result_response.status
        result = result_response.result

        if status == GoalStatus.STATUS_SUCCEEDED:
            msg = result.message or "Navigation completed"
            self.logger.info(f"Goal succeeded: {msg}")
            self.feedback(msg)
            return msg

        if status in (GoalStatus.STATUS_CANCELED, GoalStatus.STATUS_ABORTED):
            msg = result.message or "Navigation canceled"
            self.logger.info(f"Goal canceled/aborted: {msg}")
            self.feedback(msg)
            return msg, SkillResult.CANCELLED

        msg = result.message or "Navigation ended unexpectedly."
        self.logger.warning(msg)
        self.feedback(msg)
        self.fail(msg)

    # ── Future waiting (no node re-spin) ──────────────────────────────────────

    def _wait_for_future(self, future, timeout_sec=None):
        """Block until *future* completes, without spinning self.node.

        The skills_server node is already spun by its dedicated executor, which
        services this future's done-callback on another thread. We just wait on
        that callback via an Event. Returns True if the future completed, False
        on timeout.
        """
        if future.done():
            return True
        done_event = threading.Event()
        future.add_done_callback(lambda _future: done_event.set())
        return done_event.wait(timeout=timeout_sec)

    # ── Feedback callback (called on the executor thread) ─────────────────────

    def _on_feedback(self, feedback_msg):
        """Relay action feedback to the brain as a human-readable string.

        Only sends when the action changes or every 5th consecutive stop
        to avoid flooding the brain logs.
        """
        fb = feedback_msg.feedback
        action_label = _ACTION_LABELS.get(fb.latest_action, str(fb.latest_action))
        text = f"Action: {action_label} | Consecutive stops: {fb.consecutive_stops}/{fb.max_consecutive_stops}"
        self.logger.debug(f"[NavigateWithVision] feedback: {text}")

        action_changed = (
            fb.latest_action != self._last_feedback_action and fb.latest_action != 0  # don't report individual STOPs
        )
        stops_milestone = False  # no stop-count feedback
        self._last_feedback_action = fb.latest_action
        self._last_feedback_stops = fb.consecutive_stops

        if action_changed or stops_milestone:
            self.feedback(text)
