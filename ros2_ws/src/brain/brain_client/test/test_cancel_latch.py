# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the platform-owned skill cancel latch (Skill._cancelled).

Pins the fix for the lost-cancel race: a cancel() that fires between goal
acceptance and execute() entry must survive the `self._cancelled = False`
reset that most skills perform as their first statement.
"""

import logging
from types import SimpleNamespace

from brain_client.skills.types import Skill, SkillResult


class LegacyPatternSkill(Skill):
    """Mirrors the pattern used by the existing skill fleet: own _cancelled
    flag, reset at execute() entry, set by an overridden cancel()."""

    def __init__(self):
        super().__init__(logging.getLogger("test"))
        self._cancelled = False

    @property
    def name(self):
        return "legacy_pattern"

    def execute(self):
        self._cancelled = False  # the reset that used to wipe an early cancel
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED
        return "Done", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "cancelled"


def _goal_handle(cancel_requested):
    return SimpleNamespace(is_cancel_requested=cancel_requested)


def test_cancel_before_execute_survives_reset():
    skill = LegacyPatternSkill()
    skill._begin_run(_goal_handle(False))
    skill.cancel()  # races ahead of execute() on another thread
    skill.execute()  # legacy reset must not clear the latch
    assert skill._cancelled is True


def test_begin_run_clears_stale_latch_from_previous_run():
    skill = LegacyPatternSkill()
    skill.cancel()  # previous run was cancelled
    skill._begin_run(_goal_handle(False))
    assert skill._cancelled is False


def test_begin_run_recovers_cancel_from_goal_handle():
    # cancel landed before _begin_run: the latch was set then cleared, but the
    # goal's persistent cancel status re-latches it.
    skill = LegacyPatternSkill()
    skill.cancel()
    skill._begin_run(_goal_handle(True))
    assert skill._cancelled is True


def test_setting_false_is_ignored_and_true_latches():
    skill = LegacyPatternSkill()
    skill._begin_run(None)
    skill._cancelled = False
    assert skill._cancelled is False
    skill._cancelled = True
    skill._cancelled = False  # mid-run reset attempts must not unlatch
    assert skill._cancelled is True


def test_base_cancel_latches_without_invoker():
    skill = LegacyPatternSkill()
    skill._begin_run(None)
    Skill.cancel(skill)  # base implementation, no self.skills set
    assert skill._cancelled is True


class NoSuperInitSkill(Skill):
    """Some fleet skills (navigate_to_position, send_email, …) define __init__
    without calling super().__init__() — the latch must self-create."""

    def __init__(self):  # noqa: intentionally no super().__init__()
        self._cancelled = False

    @property
    def name(self):
        return "no_super_init"

    def execute(self):
        return "Done", SkillResult.SUCCESS


def test_latch_works_without_super_init():
    skill = NoSuperInitSkill()
    assert skill._cancelled is False
    skill._begin_run(_goal_handle(False))
    Skill.cancel(skill)
    assert skill._cancelled is True
    skill._begin_run(_goal_handle(False))
    assert skill._cancelled is False
