# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit test for nested continuous-state updates: a chained child that declares
required robot states takes over the 50 Hz slot and hands it back to its parent
on exit, instead of silently tearing the parent's feed down (Greptile P1)."""

import threading
import time

from brain_client.skills.robot_state import RobotStateProvider


class _Logger:
    def info(self, *a, **k):
        pass

    error = warning = warn = debug = info


def _provider():
    p = RobotStateProvider.__new__(RobotStateProvider)
    p._logger = _Logger()
    p._current_skill = None
    p._current_skill_lock = threading.Lock()
    p._skill_stack = []
    p._state_update_thread = None
    p._state_update_stop_event = threading.Event()
    return p


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_child_suspends_parent_and_hands_slot_back():
    provider = _provider()
    updates = []
    provider.update_skill_robot_state = updates.append

    parent, child = object(), object()

    provider.begin_continuous_updates(parent)
    _wait_for(lambda: parent in updates)

    provider.begin_continuous_updates(child)  # chained child takes the slot
    _wait_for(lambda: child in updates)
    assert provider._current_skill is child

    provider.end_continuous_updates()  # child done -> parent resumes
    assert provider._current_skill is parent
    assert provider._state_update_thread is not None and provider._state_update_thread.is_alive()
    updates.clear()
    _wait_for(lambda: parent in updates)  # parent is receiving state again

    provider.end_continuous_updates()  # parent done -> feed stops
    assert provider._current_skill is None
    assert provider._state_update_thread is None


def test_single_skill_begin_end_still_stops_the_thread():
    provider = _provider()
    provider.update_skill_robot_state = lambda skill: None

    provider.begin_continuous_updates(object())
    thread = provider._state_update_thread
    assert thread.is_alive()

    provider.end_continuous_updates()
    assert provider._state_update_thread is None
    _wait_for(lambda: not thread.is_alive())
