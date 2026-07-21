# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for dropped-trigger reporting (INN-711 startup variant).

The cloud marks a primitive as running the moment it SENDS the task, with no
timeout of its own; only a terminal lifecycle message clears it. So when the
robot skips a VisionAgentOutput (brain inactive, or skills mid-registration),
it must bounce a terminal "failed" for the task's primitive_id — a silent
drop leaves the agent convinced forever that the skill is still running.
"""

from types import SimpleNamespace

from brain_client.core.vision_output import VisionOutputHandler


class FakeLogger:
    def info(self, msg):
        pass

    def warn(self, msg):
        pass

    def error(self, msg):
        pass


class FakeRunner:
    def __init__(self):
        self.start_failures = []
        self.started = []
        self.pending_cleared = False

    def report_start_failure(self, *, primitive_name, primitive_id, reason, skill_id=None):
        self.start_failures.append({"primitive_name": primitive_name, "primitive_id": primitive_id, "reason": reason})

    def start_task(self, skill_id, primitive_id, inputs):
        self.started.append((skill_id, primitive_id, inputs))

    def clear_pending(self):
        self.pending_cleared = True

    @property
    def has_active_goal(self):
        return False


def _make_handler(*, active, registered):
    runner = FakeRunner()
    state = SimpleNamespace(
        is_brain_active=active,
        primitives_registered=registered,
        log_everything=False,
        pose_at_image_send=None,
        registry=SimpleNamespace(resolve_skill_id=lambda t: f"innate-os/{t}"),
    )
    handler = VisionOutputHandler(
        SimpleNamespace(get_logger=lambda: FakeLogger()),
        state,
        runner=runner,
        chat=SimpleNamespace(emit=lambda *a, **k: None),
        gaze=SimpleNamespace(pause=lambda: None),
        pose_tracker=SimpleNamespace(),
    )
    return handler, runner


def _msg(next_task):
    payload = {
        "stop_current_task": False,
        "observation": "o",
        "thoughts": "t",
        "new_goal": None,
        "anticipation": None,
        "to_tell_user": None,
        "next_task": next_task,
    }
    return SimpleNamespace(payload=payload)


TASK = {"type": "turn_and_move", "inputs": {"x": 0.5}, "primitive_id": "prim-42"}


def test_drop_while_inactive_bounces_terminal_failed():
    handler, runner = _make_handler(active=False, registered=True)
    handler.handle_message(_msg(TASK))
    assert runner.started == []
    assert len(runner.start_failures) == 1
    assert runner.start_failures[0]["primitive_id"] == "prim-42"
    assert "not active" in runner.start_failures[0]["reason"]


def test_drop_while_unregistered_bounces_terminal_failed():
    handler, runner = _make_handler(active=True, registered=False)
    handler.handle_message(_msg(TASK))
    assert runner.started == []
    assert len(runner.start_failures) == 1
    assert runner.start_failures[0]["primitive_id"] == "prim-42"
    assert "registration" in runner.start_failures[0]["reason"]


def test_drop_without_task_stays_silent():
    # No primitive_id -> the cloud is not waiting on anything; no bounce.
    handler, runner = _make_handler(active=False, registered=False)
    handler.handle_message(_msg(None))
    assert runner.start_failures == []


def test_normal_path_dispatches_without_bounce():
    handler, runner = _make_handler(active=True, registered=True)
    handler.handle_message(_msg(TASK))
    assert runner.start_failures == []
    assert runner.started == [("innate-os/turn_and_move", "prim-42", {"x": 0.5})]
