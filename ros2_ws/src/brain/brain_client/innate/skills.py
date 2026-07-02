# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skills as plain functions: import them, call them.

    from innate import Skill, SkillResult
    from innate.skills import head_emotion, navigate_to_position

    class MorningRoutine(Skill):
        def execute(self):
            head_emotion(emotion="excited")
            navigate_to_position(x=0.3, y=0.0, theta=0.0)
            return "Routine complete", SkillResult.SUCCESS

There is no head_emotion function in this file. Attribute access on this module
(PEP 562 module __getattr__) returns a small proxy that, when called, hands the
skill name to whichever SkillInvoker is currently executing a skill — kept in a
contextvar the skills server sets around every execute() (see use_invoker).
Names resolve against the live catalog, with a user's local/ skill shadowing a
shipped innate-os/ one of the same name.

Calls raise instead of returning a status: success returns the child's output
message, failure raises SkillFailed, cancellation raises SkillCancelled. A
routine is just consecutive calls, and "stop on first failure" is Python's own
semantics. The explicit tuple-returning form, self.skills.run(id, **inputs),
remains for dynamic skill ids.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from brain_client.skills.types import SkillResult


class SkillFailed(Exception):
    """A skill called through innate.skills reported FAILURE."""


class SkillCancelled(Exception):
    """A skill called through innate.skills was cancelled; unwinds the routine."""


# The invoker driving the currently-executing skill. The skills server sets this
# around every execute(), in the thread that runs it — which is exactly what
# makes the proxies below reach the right goal. None outside a skill run.
_current_invoker: ContextVar = ContextVar("innate_skills_invoker", default=None)


@contextmanager
def use_invoker(invoker):
    """Make ``invoker`` the ambient invoker for the duration of a ``with`` block.

    The skills server wraps every execute() in this. It doubles as the test
    fixture: wrap the routine under test with any fake exposing
    ``run(skill_id, **inputs) -> (message, SkillResult)``.
    """
    token = _current_invoker.set(invoker)
    try:
        yield
    finally:
        _current_invoker.reset(token)


def __getattr__(name: str):
    """``from innate.skills import anything`` -> a proxy that runs that skill."""
    if name.startswith("_"):  # dunder/private lookups are never skills
        raise AttributeError(name)

    def run_skill(**inputs):
        invoker = _current_invoker.get()
        if invoker is None:
            raise RuntimeError(
                f"innate.skills.{name}() can only be called while a skill is "
                "executing — from inside execute(), or under use_invoker() in tests."
            )
        message, status = invoker.run(name, **inputs)
        if status is SkillResult.CANCELLED:
            raise SkillCancelled(message)
        if status is not SkillResult.SUCCESS:
            raise SkillFailed(message)
        return message

    run_skill.__name__ = run_skill.__qualname__ = name
    return run_skill
