# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""self.skills.run(...) — let one skill run a few others, in order.

A skill's execute() can call e.g. self.skills.run("innate-os/wave") to fire off
another skill and block until it finishes. Any kind of skill works (python,
learned, replay): under the hood we just call back into the same server paths
that run a top-level skill, so a chained policy runs exactly as it would alone.

The one wrinkle is the UI. Children run on the *parent's* goal, so they have no
lifecycle of their own to announce. We smuggle each child's start/finish out
through the parent's feedback stream (encode_substep_feedback) and the runner
pulls it back apart into a real per-step status. See PrimitiveRunner._on_feedback.
"""

from __future__ import annotations

import uuid

from brain_client.skills.lifecycle import encode_substep_feedback
from brain_client.skills.types import SkillResult

# how a child's final SkillResult shows up as a step in the app
_STEP_EVENT = {
    SkillResult.SUCCESS: "completed",
    SkillResult.FAILURE: "failed",
    SkillResult.CANCELLED: "interrupted",
}


class SkillInvoker:
    """Runs child skills on behalf of a parent skill's execute_skill goal."""

    def __init__(self, server, goal_handle, publish_feedback):
        self._server = server
        self._goal_handle = goal_handle
        self._publish_feedback = publish_feedback
        self._logger = server.get_logger()
        # once any child is cancelled the whole routine is, so a plain
        # `for step in steps: self.skills.run(...)` stops cleanly.
        self._cancelled = False
        # whoever is running right now, so cancel() knows who to poke.
        self._active_code_skill = None
        self._active_physical_skill = None

    def run(self, skill_id, **inputs):
        """Run one child skill to completion. Returns (message, SkillResult)."""
        if self._cancelled:
            return "Routine cancelled", SkillResult.CANCELLED

        # resolve the id: python skill first, then learned/replay
        code = self._server.catalog.get_code_skill(skill_id)
        physical = None if code else self._server.catalog.get_physical_skill(skill_id)
        if code is None and physical is None:
            self._logger.error(f"[invoker] unknown skill '{skill_id}'")
            return f"Unknown skill '{skill_id}'", SkillResult.FAILURE

        name = code[1].name if code else physical["metadata"].get("name", skill_id)
        step_id = uuid.uuid4().hex
        self._logger.info(f"[invoker] running '{skill_id}' inputs={inputs}")
        self._step(step_id, name, skill_id, "running")

        try:
            if code is not None:
                message, status = self._run_code(code[1], skill_id, inputs)
            else:
                message, status = self._run_physical(skill_id, physical)
        except Exception as e:
            # a buggy child shouldn't take down the whole skills server
            self._logger.error(f"[invoker] '{skill_id}' raised: {e}")
            self._step(step_id, name, skill_id, "failed", reason=str(e))
            return str(e), SkillResult.FAILURE

        self._step(
            step_id,
            name,
            skill_id,
            _STEP_EVENT.get(status, "completed"),
            reason=message if status is SkillResult.FAILURE else None,
            output=message if status is SkillResult.SUCCESS else None,
        )
        self._cancelled = self._cancelled or status is SkillResult.CANCELLED
        return message, status

    def cancel(self):
        """Stop the child running right now. Call from the parent skill's cancel()."""
        self._cancelled = True
        if self._active_code_skill is not None:
            try:
                self._active_code_skill.cancel()
            except Exception as e:
                self._logger.error(f"[invoker] error cancelling child: {e}")
        if self._active_physical_skill is not None:
            self._server._request_behavior_goal_cancel(self._goal_handle, self._active_physical_skill)
        return "Routine cancelled"

    # The two run paths just delegate to the server (reusing the top-level
    # execution code) and remember who's live so cancel() can reach them.

    def _run_code(self, skill, skill_id, inputs):
        skill.set_feedback_callback(self._publish_feedback)
        skill.skills = self  # a child can chain too; _cancelled flows through
        self._active_code_skill = skill
        try:
            return self._server._run_code_skill_body(skill, skill_id, inputs)
        finally:
            self._active_code_skill = None

    def _run_physical(self, skill_id, physical):
        self._active_physical_skill = skill_id
        try:
            success, message, success_type, _finalize = self._server._run_physical_skill(
                self._goal_handle, skill_id, physical
            )
        finally:
            self._active_physical_skill = None
        return message, SkillResult(success_type)

    def _step(self, step_id, name, skill_id, event, reason=None, output=None):
        # children have no goal of their own, so announce each step by tagging the
        # parent's feedback; the runner turns this back into a real per-step status.
        self._publish_feedback(
            encode_substep_feedback(
                event=event, name=name, primitive_id=step_id, skill_id=skill_id, reason=reason, output=output
            )
        )
