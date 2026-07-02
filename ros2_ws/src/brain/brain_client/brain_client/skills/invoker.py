# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""self.skills.run(...) — lets one skill run others, in order.

Children reuse the same server execution paths as a top-level skill, so any
kind of skill (python, learned, replay) works. They run on the parent's goal:
each child's start/finish is tagged onto the parent's feedback stream and
decoded back into a per-step status by PrimitiveRunner._on_feedback.
"""

from __future__ import annotations

import inspect
import threading
import uuid

from brain_client.skills.lifecycle import encode_substep_feedback
from brain_client.skills.types import SkillResult

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
        self._cancelled = False
        # guards cancel() against re-entry: a child's default cancel() delegates
        # right back to the invoker that is cancelling it.
        self._cancelling = False
        self._active_code_skill = None
        self._active_physical_skill = None

    def run(self, skill_id, *, timeout=None, **inputs):
        """Run one child skill to completion. Returns (message, SkillResult).

        Accepts a full catalog id ("innate-os/wave") or a bare name ("wave");
        bare names try local/ before innate-os/. A child still running when
        ``timeout`` (seconds) expires is cancelled and reported as a FAILURE,
        without cancelling the rest of the routine.
        """
        if self._cancelled:
            return "Routine cancelled", SkillResult.CANCELLED

        skill_id, code, physical = self._resolve(skill_id)
        if code is None and physical is None:
            self._logger.error(f"[invoker] unknown skill '{skill_id}'")
            return f"Unknown skill '{skill_id}'", SkillResult.FAILURE

        if code is not None:
            problem = self._invalid_inputs(code[1], skill_id, inputs)
            if problem:
                self._logger.error(f"[invoker] {problem}")
                return problem, SkillResult.FAILURE

        name = code[1].name if code else physical["metadata"].get("name", skill_id)
        step_id = uuid.uuid4().hex
        self._logger.info(f"[invoker] running '{skill_id}' inputs={inputs} timeout={timeout}")
        self._step(step_id, name, skill_id, "running")

        timed_out = threading.Event()
        watchdog = None
        if timeout:

            def _expire():
                timed_out.set()
                self.cancel()

            watchdog = threading.Timer(timeout, _expire)
            watchdog.start()
        try:
            if code is not None:
                message, status = self._run_code(code[1], skill_id, inputs)
            else:
                message, status = self._run_physical(skill_id, physical)
        except Exception as e:
            self._logger.error(f"[invoker] '{skill_id}' raised: {e}")
            self._step(step_id, name, skill_id, "failed", reason=str(e))
            return str(e), SkillResult.FAILURE
        finally:
            if watchdog is not None:
                watchdog.cancel()

        if timed_out.is_set() and status is SkillResult.CANCELLED:
            # only this child timed out, not the routine — undo the flag the
            # watchdog's cancel() set so later steps still run
            self._cancelled = False
            message, status = f"'{skill_id}' timed out after {timeout}s", SkillResult.FAILURE

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
        if self._cancelling:
            return "Routine cancelled"  # re-entered from the child we're cancelling
        self._cancelling = True
        try:
            if self._active_code_skill is not None:
                try:
                    self._active_code_skill.cancel()
                except Exception as e:
                    self._logger.error(f"[invoker] error cancelling child: {e}")
            if self._active_physical_skill is not None:
                self._server._request_behavior_goal_cancel(self._goal_handle, self._active_physical_skill)
            return "Routine cancelled"
        finally:
            self._cancelling = False

    @staticmethod
    def _invalid_inputs(skill, skill_id, inputs):
        """An error message if inputs don't fit execute()'s signature, else None."""
        try:
            signature = inspect.signature(skill.execute)
        except (TypeError, ValueError):
            return None  # can't introspect — let the call itself decide
        try:
            signature.bind(**inputs)
            return None
        except TypeError as e:
            expected = ", ".join(p for p in signature.parameters if p != "self") or "no inputs"
            return f"Invalid inputs for '{skill_id}': {e}. Expected: ({expected})"

    def _resolve(self, skill_id):
        """Look skill_id up in the catalog. Returns (resolved_id, code_entry, physical_entry)."""
        candidates = [skill_id] if "/" in skill_id else [f"local/{skill_id}", f"innate-os/{skill_id}"]
        for candidate in candidates:
            code = self._server.catalog.get_code_skill(candidate)
            if code is not None:
                return candidate, code, None
            physical = self._server.catalog.get_physical_skill(candidate)
            if physical is not None:
                return candidate, None, physical
        return skill_id, None, None

    def _run_code(self, skill, skill_id, inputs):
        skill.set_feedback_callback(self._publish_feedback)
        skill.skills = self  # a child can chain too
        # save/restore, not clear: in a nested chain A→B→C the slot must hand
        # back to B when C ends, or cancel() can no longer reach B
        prev = self._active_code_skill
        self._active_code_skill = skill
        try:
            return self._server._run_code_skill_body(skill, skill_id, inputs)
        finally:
            self._active_code_skill = prev

    def _run_physical(self, skill_id, physical):
        prev = self._active_physical_skill
        self._active_physical_skill = skill_id
        try:
            success, message, success_type, _finalize = self._server._run_physical_skill(
                self._goal_handle, skill_id, physical
            )
        finally:
            self._active_physical_skill = prev
        return message, SkillResult(success_type)

    def _step(self, step_id, name, skill_id, event, reason=None, output=None):
        self._publish_feedback(
            encode_substep_feedback(
                event=event, name=name, primitive_id=step_id, skill_id=skill_id, reason=reason, output=output
            )
        )
