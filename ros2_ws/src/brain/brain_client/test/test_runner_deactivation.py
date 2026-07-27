# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for PrimitiveRunner's deactivation state transitions.

interrupt_for_deactivation only touches ``_state`` and ``_goal_handle``, so it
is exercised on a plain stub via the unbound method — no action server needed.
"""

from types import SimpleNamespace

from brain_client.core.state import BrainState
from brain_client.skills.runner import PrimitiveRunner


def make_runner(running: dict | None, goal_handle=None):
    state = BrainState()
    state.primitive_running = running
    return SimpleNamespace(_state=state, _goal_handle=goal_handle), state


def test_deactivation_preserves_manual_running_state():
    # A manual (webapp/CLI) run has no local goal handle and keeps running on
    # the skills server: deactivation must not erase the mirrored state, or a
    # reactivated brain would start a second skill alongside it.
    runner, state = make_runner({"primitive_name": "wave", "skill_id": "local/wave", "manual": True})
    PrimitiveRunner.interrupt_for_deactivation(runner)
    assert state.primitive_running is not None
    assert state.primitive_running["manual"] is True


def test_deactivation_cancels_and_clears_brain_owned_run():
    cancelled = []
    handle = SimpleNamespace(cancel_goal_async=lambda: cancelled.append(True))
    runner, state = make_runner(
        {"primitive_name": "navigate_to_position", "skill_id": "innate-os/navigate_to_position"}, handle
    )
    PrimitiveRunner.interrupt_for_deactivation(runner)
    assert cancelled == [True]
    assert runner._goal_handle is None
    assert state.primitive_running is None


def test_deactivation_with_nothing_running_is_a_no_op():
    runner, state = make_runner(None)
    PrimitiveRunner.interrupt_for_deactivation(runner)
    assert state.primitive_running is None
    assert runner._goal_handle is None
