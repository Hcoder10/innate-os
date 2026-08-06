# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""self.skills.run(...) — lets one skill run others, in order. Children run
on the parent's goal; their start/finish is tagged onto the parent's feedback
stream and decoded back by PrimitiveRunner."""

from __future__ import annotations

import threading
import uuid

from brain_client.skills.lifecycle import encode_substep_feedback
from brain_client.skills.types import SkillCancelled, SkillOutput, SkillResult

_STEP_EVENT = {
    SkillResult.SUCCESS: "completed",
    SkillResult.FAILURE: "failed",
    SkillResult.CANCELLED: "interrupted",
}


class SkillInvoker:
    """Runs child skills on behalf of a parent skill's execute_skill goal."""

    def __init__(self, server, goal_handle, publish_feedback, run_node):
        self._server = server
        self._goal_handle = goal_handle
        self._publish_feedback = publish_feedback
        # children live on the top-level run's node; entities die with it
        self._run_node = run_node
        self._logger = server.get_logger()
        self._cancelled = False
        # a child's default cancel() delegates right back to the invoker
        # cancelling it — guard against the re-entry. The thread identity
        # tells that re-entry (same thread, mid-_cancel_active_child) apart
        # from a genuine Stop arriving on another thread.
        self._cancelling = False
        self._cancelling_thread = None
        self._active_code_skill = None
        self._active_physical_skill = None

    def run(self, skill_id, *, timeout=None, **inputs) -> SkillOutput:
        """Run one child to completion; returns a SkillOutput whose status is
        SUCCESS or FAILURE. A child outliving ``timeout`` seconds is
        cancelled and reported as a FAILURE without cancelling the rest of
        the routine. A cancelled routine raises SkillCancelled instead of
        returning, so callers never handle CANCELLED themselves."""
        if self._cancelled:
            raise SkillCancelled("Routine cancelled")

        skill_id, code, physical = self._resolve(skill_id)
        if code is not None:
            problem = self._invalid_inputs(code, skill_id, inputs)
            if problem:
                self._logger.error(f"[invoker] {problem}")
                return SkillOutput(problem, status=SkillResult.FAILURE)
            name = code.display_name
        elif physical is not None:
            name = physical.metadata.get("name", skill_id)
        else:
            reason = self._server.catalog.unavailable_reason(skill_id)
            if reason:
                self._logger.error(f"[invoker] skill '{skill_id}' {reason}")
                return SkillOutput(f"Skill '{skill_id}' {reason}", status=SkillResult.FAILURE)
            self._logger.error(f"[invoker] unknown skill '{skill_id}'")
            return SkillOutput(f"Unknown skill '{skill_id}'", status=SkillResult.FAILURE)
        step_id = uuid.uuid4().hex
        self._logger.info(f"[invoker] running '{skill_id}' inputs={inputs} timeout={timeout}")
        self._step(step_id, name, skill_id, "running")

        timed_out = threading.Event()
        watchdog = None
        # The settle handshake closes the window between the child finishing
        # (its finally restores the active-skill slot to the parent) and run()
        # cancelling the timer below: a timer expiring in that window would
        # cancel whatever ancestor now owns the slot. The child's finally
        # settles under the lock BEFORE restoring the slot, so _expire either
        # sees settled (no-op) or cancels while the slot still points at the
        # child.
        settle_lock = threading.Lock()
        settled = False

        def _settle():
            nonlocal settled
            with settle_lock:
                settled = True

        if timeout:

            def _expire():
                with settle_lock:
                    if settled:
                        return  # child already finished — the slot may belong to an ancestor
                    timed_out.set()
                    # Scope the cancel to THIS call's subtree. When the timed-out
                    # child is physical, _active_code_skill (if any) is the code
                    # skill that dispatched it — an ancestor, not the child — and
                    # cancelling it would unwind the very routine the timeout is
                    # documented not to cancel. A code child's descendants (deeper
                    # code, a physical it delegated to) are fair game: execution
                    # is strictly nested, so anything active below it is its own.
                    self._cancel_active_child(include_code=code is not None)

            watchdog = threading.Timer(timeout, _expire)
            watchdog.start()
        try:
            if code is not None:
                output = self._run_code(code, skill_id, inputs, settle=_settle)
            else:
                output = self._run_physical(skill_id, physical, settle=_settle)
        except Exception as e:
            self._logger.error(f"[invoker] '{skill_id}' raised: {e}")
            self._step(step_id, name, skill_id, "failed", reason=str(e))
            return SkillOutput(str(e), status=SkillResult.FAILURE)
        finally:
            if watchdog is not None:
                watchdog.cancel()

        if timed_out.is_set() and output.status is SkillResult.CANCELLED:
            # only this child timed out, not the routine — report a step
            # failure so later steps still run
            output = SkillOutput(f"'{skill_id}' timed out after {timeout}s", status=SkillResult.FAILURE)

        self._step(
            step_id,
            name,
            skill_id,
            _STEP_EVENT.get(output.status, "completed"),
            reason=output.message if output.status is SkillResult.FAILURE else None,
            output=output.message if output.status is SkillResult.SUCCESS else None,
        )
        if output.status is SkillResult.CANCELLED:
            self._cancelled = True
            raise SkillCancelled(output.message)
        return output

    def cancel(self):
        """Stop the routine: the running child and every step after it. The
        timeout watchdog uses _cancel_active_child directly so it never marks
        the whole routine cancelled."""
        if self._cancelling:
            if threading.current_thread() is self._cancelling_thread:
                return "Routine cancelled"  # re-entered from the child we're cancelling
            # a Stop racing the watchdog's child-cancel: the child is already
            # being cancelled, but the routine itself must still latch
            self._cancelled = True
            return "Routine cancelled"
        self._cancelled = True
        return self._cancel_active_child()

    def _cancel_active_child(self, include_code=True):
        """Cancel the running children. ``include_code=False`` is the timeout
        watchdog for a physical child: the active code skill (if any) is the
        one that DISPATCHED that child — cancelling it would unwind the
        routine the timeout must not cancel — so only the behavior goal is
        touched. The physical cancel always runs here (not via the code
        skill's own cancel()): its forward to invoker.cancel() re-enters on
        this thread and no-ops."""
        if self._cancelling:
            return "Routine cancelled"
        self._cancelling_thread = threading.current_thread()
        self._cancelling = True
        try:
            if include_code and self._active_code_skill is not None:
                try:
                    self._active_code_skill.cancel()
                except (Exception, SkillCancelled) as e:
                    # SkillCancelled (a BaseException) included: a child's
                    # cancel() override may raise it, and the physical-goal
                    # cancel below must still be dispatched.
                    self._logger.error(f"[invoker] error cancelling child: {e}")
            if self._active_physical_skill is not None:
                self._server._request_behavior_goal_cancel(self._goal_handle, self._active_physical_skill)
            return "Routine cancelled"
        finally:
            self._cancelling = False

    @staticmethod
    def _invalid_inputs(entry, skill_id, inputs):
        """An error message if inputs don't fit the harvested schema, else
        None — checked before an instance exists."""
        missing = [name for name, spec in entry.inputs.items() if spec.get("required") and name not in inputs]
        unknown = [] if entry.accepts_extra_inputs else [name for name in inputs if name not in entry.inputs]
        if not missing and not unknown:
            return None
        problems = []
        if missing:
            problems.append(f"missing required: {', '.join(missing)}")
        if unknown:
            problems.append(f"unexpected: {', '.join(unknown)}")
        expected = ", ".join(entry.inputs) or "no inputs"
        return f"Invalid inputs for '{skill_id}': {'; '.join(problems)}. Expected: ({expected})"

    def find(self, skill_id: str) -> str | None:
        """The resolved id for ``skill_id``, or None if no such skill.

        Used at wire time to fail a declared physical skill up front rather
        than mid-routine.
        """
        resolved, code, physical = self._resolve(skill_id)
        return resolved if (code is not None or physical is not None) else None

    def _resolve(self, skill_id):
        """Look skill_id up in the catalog. Returns (resolved_id, code_entry, physical_entry)."""
        candidates = self._server.catalog.bare_id_candidates(skill_id)
        for candidate in candidates:
            code = self._server.catalog.get_code_skill(candidate)
            if code is not None:
                return candidate, code, None
            physical = self._server.catalog.get_physical_skill(candidate)
            if physical is not None:
                return candidate, None, physical
        return skill_id, None, None

    def _run_code(self, entry, skill_id, inputs, settle=None):
        skill = self._server._instantiate_for_run(entry, self._run_node, self, self._publish_feedback)
        # save/restore, not clear: in a nested chain A→B→C the slot must hand
        # back to B when C ends, or cancel() can no longer reach B
        prev = self._active_code_skill
        self._active_code_skill = skill
        try:
            return self._server._run_code_skill_body(skill, entry, skill_id, inputs, self._goal_handle)
        finally:
            if settle is not None:
                settle()  # before the slot changes hands — see run()'s settle handshake
            self._active_code_skill = prev
            self._server._dispose_run_instance(skill)

    def _run_physical(self, skill_id, physical, settle=None):
        prev = self._active_physical_skill
        self._active_physical_skill = skill_id
        try:
            return self._server._run_physical_skill(self._goal_handle, skill_id, physical)
        finally:
            if settle is not None:
                settle()  # before the slot changes hands — see run()'s settle handshake
            self._active_physical_skill = prev

    def _step(self, step_id, name, skill_id, event, reason=None, output=None):
        self._publish_feedback(
            encode_substep_feedback(
                event=event, name=name, primitive_id=step_id, skill_id=skill_id, reason=reason, output=output
            )
        )
