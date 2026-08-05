# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for PrimitiveRunner's release paths: deactivation, reset, and the
late callbacks of a disowned goal. Exercised on a bare instance (no ROS node or
action server) — these paths only touch ``_state``, ``_goal_handle``, and
``_generation``.
"""

from types import SimpleNamespace

from brain_client.core.state import BrainState
from brain_client.skills.runner import PrimitiveRunner


def make_runner(running: dict | None, goal_handle=None):
    runner = PrimitiveRunner.__new__(PrimitiveRunner)
    state = BrainState()
    state.primitive_running = running
    runner._state = state
    runner._goal_handle = goal_handle
    runner._generation = 0
    runner._logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    runner._stop_robot = lambda: None
    runner.on_event = lambda status, name, detail=None: None
    return runner, state


def make_handle(cancelled: list, accepted: bool = True):
    return SimpleNamespace(accepted=accepted, cancel_goal_async=lambda: cancelled.append(True))


def as_future(value):
    return SimpleNamespace(result=lambda: value)


BRAIN_RUN = {"primitive_name": "wave", "primitive_id": "p1", "skill_id": "local/wave"}
MANUAL_RUN = {"primitive_name": "wave", "primitive_id": "m1", "skill_id": "local/wave", "manual": True}


def test_deactivation_preserves_manual_running_state():
    # A manual (webapp/CLI) run has no local goal handle and keeps running on
    # the skills server: deactivation must not erase the mirrored state, or a
    # reactivated brain would start a second skill alongside it.
    runner, state = make_runner(dict(MANUAL_RUN))
    runner.interrupt_for_deactivation()
    assert state.primitive_running is not None
    assert state.primitive_running["manual"] is True


def test_reset_preserves_manual_running_state_and_leaves_the_robot_alone():
    stopped = []
    runner, state = make_runner(dict(MANUAL_RUN))
    runner._stop_robot = lambda: stopped.append(True)
    runner.abort_running()
    assert state.primitive_running is not None
    assert stopped == []


def test_deactivation_cancels_and_clears_brain_owned_run():
    cancelled = []
    runner, state = make_runner(dict(BRAIN_RUN), make_handle(cancelled))
    runner.interrupt_for_deactivation()
    assert cancelled == [True]
    assert runner._goal_handle is None
    assert state.primitive_running is None


def test_reset_cancels_the_goal_and_halts_the_robot():
    cancelled, stopped = [], []
    runner, state = make_runner(dict(BRAIN_RUN), make_handle(cancelled))
    runner._stop_robot = lambda: stopped.append(True)
    runner.abort_running()
    assert cancelled == [True]
    assert stopped == [True]
    assert state.primitive_running is None


def test_disowned_pending_goal_is_cancelled_when_its_handle_arrives():
    # The agent loop sent the goal moments before deactivation: no handle
    # exists yet, so the release bumps the generation and the goal response
    # callback cancels the run on arrival instead of retaining it.
    runner, state = make_runner(dict(BRAIN_RUN), goal_handle=None)
    sent_generation = runner._generation
    runner.interrupt_for_deactivation()
    assert state.primitive_running is None

    cancelled = []
    runner._on_goal_response(as_future(make_handle(cancelled)), sent_generation)
    assert cancelled == [True]
    assert runner._goal_handle is None


def test_stale_result_neither_clears_newer_state_nor_reports_to_the_brain():
    # Reset disowns the running goal; a manual run starts before the old
    # goal's terminal result lands. That result must not clear the newer run's
    # state or inject a false event into the fresh session.
    cancelled = []
    runner, state = make_runner(dict(BRAIN_RUN), make_handle(cancelled))
    events = []
    runner.on_event = lambda status, name, detail=None: events.append(status)
    sent_generation = runner._generation
    runner.abort_running()
    state.primitive_running = dict(MANUAL_RUN)

    result = SimpleNamespace(
        result=SimpleNamespace(success=False, success_type="CANCELLED", skill_type="local/wave", message="")
    )
    runner._on_result(as_future(result), sent_generation)
    assert state.primitive_running == MANUAL_RUN
    assert events == []


def test_stale_feedback_is_dropped():
    runner, state = make_runner(dict(BRAIN_RUN), goal_handle=None)
    forwarded = []
    runner.on_feedback = lambda name, feedback, image=None: forwarded.append(feedback)
    sent_generation = runner._generation
    runner.interrupt_for_deactivation()

    feedback = SimpleNamespace(feedback=SimpleNamespace(feedback="halfway there", image_b64=""))
    runner._on_feedback_msg(feedback, sent_generation)
    assert forwarded == []


def test_deactivation_with_nothing_running_is_a_no_op():
    runner, state = make_runner(None)
    runner.interrupt_for_deactivation()
    assert state.primitive_running is None
    assert runner._goal_handle is None
