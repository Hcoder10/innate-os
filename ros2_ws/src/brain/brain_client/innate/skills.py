# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skills as plain functions: import them, call them.

    from innate.skills import head_emotion, navigate_to_position

    head_emotion(emotion="excited")
    navigate_to_position(x=0.3, y=0.0, theta_degrees=0.0)

Module __getattr__ (PEP 562) returns a proxy that hands the skill name to
whichever SkillInvoker is currently executing a skill (a contextvar the skills
server sets around every execute(); see use_invoker). Calls block until the
child finishes and raise instead of returning a status: success returns the
child's output message (a SkillOutput — ``.data`` carries any structured
payload), failure raises SkillFailed, cancellation raises SkillCancelled.
Every call accepts a reserved ``timeout=`` seconds kwarg. The tuple-returning
form, self.skills.run(id, **inputs), remains for dynamic skill ids.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from brain_client.skills.types import SkillResult


class SkillFailed(Exception):
    """A skill called through innate.skills reported FAILURE."""


class SkillCancelled(Exception):
    """A skill called through innate.skills was cancelled; unwinds the routine."""


# The invoker driving the currently-executing skill; None outside a skill run.
_current_invoker: ContextVar = ContextVar("innate_skills_invoker", default=None)


@contextmanager
def use_invoker(invoker):
    """Make ``invoker`` the ambient invoker for a ``with`` block.

    The skills server wraps every execute() in this. Doubles as the test
    fixture: any fake exposing ``run(skill_id, **inputs)`` works.
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
