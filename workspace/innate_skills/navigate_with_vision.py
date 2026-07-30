# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import threading

from action_msgs.msg import GoalStatus
from innate_cloud_msgs.action import NavigateInstruction
from rclpy.action import ActionClient

from innate import Skill, SkillCancelled, SkillReturn, resource

_ACTION_LABELS = {
    0: "STOP",
    1: "FORWARD",
    2: "LEFT",
    3: "RIGHT",
}


class _NavigateClient:
    """The UniNavid action client, built on first use. No explicit teardown:
    it lives on the run's throwaway node and dies with it at run end."""

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

    @resource
    def client(self) -> _NavigateClient:
        return _NavigateClient(self)

    def execute(self, instruction: str) -> SkillReturn:
        self.logger.info(f"[NavigateWithVision] Instruction: {instruction!r}")
        self.feedback(f"Sending instruction: {instruction}")

        if not self.client.wait_for_server(timeout_sec=10.0):
            self.fail("Navigation server is not available. Please try again.")

        goal_msg = NavigateInstruction.Goal()
        goal_msg.instruction = instruction
        goal_future = self.client.send_goal_async(goal_msg, feedback_callback=self._on_feedback)
        if not self._wait_for_future(goal_future, timeout_sec=10.0):
            self.fail("Navigation goal timed out waiting for acceptance.")

        goal_handle = goal_future.result()
        if goal_handle is None or not goal_handle.accepted:
            self.fail("Navigation goal was rejected.")

        # A cancel must reach the action server immediately, not at the next poll.
        self.on_cancel(goal_handle.cancel_goal_async)
        self.feedback("Navigation started, waiting for completion ...")

        # The skills_server's dedicated executor services the result callback;
        # spinning self.node here would race it and corrupt the wait set.
        result_future = goal_handle.get_result_async()
        result_ready = threading.Event()
        result_future.add_done_callback(lambda _future: result_ready.set())
        while not result_ready.is_set():
            self.sleep(0.25)

        result_response = result_future.result()
        status = result_response.status
        message = result_response.result.message

        if status == GoalStatus.STATUS_SUCCEEDED:
            msg = message or "Navigation completed"
            self.feedback(msg)
            return msg
        if status == GoalStatus.STATUS_CANCELED:
            msg = message or "Navigation canceled"
            self.feedback(msg)
            raise SkillCancelled(msg)
        if status == GoalStatus.STATUS_ABORTED:
            self.fail(message or "Navigation aborted.")
        self.fail(message or "Navigation ended unexpectedly.")

    def _wait_for_future(self, future, timeout_sec=None):
        if future.done():
            return True
        done_event = threading.Event()
        future.add_done_callback(lambda _future: done_event.set())
        return done_event.wait(timeout=timeout_sec)

    def _on_feedback(self, feedback_msg):
        fb = feedback_msg.feedback
        action_label = _ACTION_LABELS.get(fb.latest_action, str(fb.latest_action))
        action_changed = fb.latest_action != self._last_feedback_action and fb.latest_action != 0
        self._last_feedback_action = fb.latest_action
        if action_changed:
            self.feedback(
                f"Action: {action_label} | Consecutive stops: {fb.consecutive_stops}/{fb.max_consecutive_stops}"
            )
