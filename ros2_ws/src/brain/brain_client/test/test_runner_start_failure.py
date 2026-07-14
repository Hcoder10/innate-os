# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for primitive start-failure reporting (INN-711).

Pins the fix for the phantom-running bug: "running" must only reach the cloud
once the goal is actually dispatched. If the action server is unavailable (or
the skill ID is unknown), the cloud gets a terminal "failed" instead — never a
"running" it will wait on forever.
"""

from types import SimpleNamespace

import pytest

import brain_client.skills.runner as runner_module
from brain_client.skills.runner import PrimitiveRunner
from brain_client.transport.messages import MessageInType


class FakeLogger:
    def info(self, msg):
        pass

    def warn(self, msg):
        pass

    def error(self, msg):
        pass


class FakeActionClient:
    """Stands in for rclpy's ActionClient; server availability is scripted."""

    def __init__(self, server_available):
        self.server_available = server_available
        self.sent_goals = []

    def wait_for_server(self, timeout_sec=None):
        return self.server_available

    def send_goal_async(self, goal_msg, feedback_callback=None):
        self.sent_goals.append(goal_msg)
        return SimpleNamespace(add_done_callback=lambda cb: None)


def _make_runner(monkeypatch, server_available):
    monkeypatch.setattr(runner_module, "ActionClient", lambda node, action, name: FakeActionClient(server_available))
    node = SimpleNamespace(get_logger=lambda: FakeLogger())
    ws_messages = []
    ws = SimpleNamespace(send_message=ws_messages.append)
    chat_statuses = []
    chat = SimpleNamespace(
        publish_task_status=lambda **kwargs: chat_statuses.append(kwargs),
        emit=lambda *args, **kwargs: None,
    )
    state = SimpleNamespace(
        primitive_running=None,
        registry=SimpleNamespace(name_for=lambda skill_id: "victory_spin"),
    )
    finished = []
    runner = PrimitiveRunner(
        node,
        ws,
        chat,
        state,
        stop_robot=lambda: None,
        on_task_finished=lambda: finished.append(True),
    )
    return runner, ws_messages, chat_statuses, finished


def test_start_task_reports_failed_when_server_unavailable(monkeypatch):
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=False)

    runner.start_task("local/victory_spin", "prim-1", {})

    assert runner.action_client.sent_goals == []
    assert runner._state.primitive_running is None
    types = [m.type for m in ws_messages]
    assert MessageInType.PRIMITIVE_ACTIVATED not in types
    assert types == [MessageInType.PRIMITIVE_FAILED]
    assert ws_messages[0].payload["primitive_name"] == "victory_spin"
    assert "never started" in ws_messages[0].payload["reason"]
    assert chat_statuses and chat_statuses[0]["status"] == "failed"
    assert finished == [True]


def test_start_task_reports_running_when_goal_dispatched(monkeypatch):
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=True)

    runner.start_task("local/victory_spin", "prim-1", {})

    assert len(runner.action_client.sent_goals) == 1
    types = [m.type for m in ws_messages]
    assert types == [MessageInType.PRIMITIVE_ACTIVATED]
    assert runner._state.primitive_running == {
        "primitive_name": "victory_spin",
        "primitive_id": "prim-1",
        "skill_id": "local/victory_spin",
    }
    assert finished == []


def _reject_goal(runner):
    rejected_handle = SimpleNamespace(accepted=False)
    runner._on_goal_response(
        SimpleNamespace(result=lambda: rejected_handle),
        skill_name="victory_spin",
        primitive_id="prim-1",
        skill_id="local/victory_spin",
    )


def test_rejected_goal_sends_terminal_failed_to_cloud(monkeypatch):
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=True)
    runner.start_task("local/victory_spin", "prim-1", {})
    ws_messages.clear()

    _reject_goal(runner)

    types = [m.type for m in ws_messages]
    assert types == [MessageInType.PRIMITIVE_FAILED]
    assert runner._state.primitive_running is None
    assert finished == [True]


def test_rejected_goal_reports_failed_even_after_state_cleared(monkeypatch):
    """A deactivate/unregister racing the goal response clears primitive_running
    without telling the cloud (no goal handle yet) — the rejection must still
    send the terminal 'failed', but must NOT run finish side effects (gaze
    resume, re-registration drain) for a task the deactivation tore down."""
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=True)
    runner.start_task("local/victory_spin", "prim-1", {})
    ws_messages.clear()
    runner._state.primitive_running = None  # the race

    _reject_goal(runner)

    types = [m.type for m in ws_messages]
    assert types == [MessageInType.PRIMITIVE_FAILED]
    assert ws_messages[0].payload["primitive_id"] == "prim-1"
    assert finished == []


def test_accepted_goal_after_teardown_is_cancelled_not_adopted(monkeypatch):
    """An acceptance landing after a deactivate/unregister teardown must not
    reattach the goal (its result would later fire finish side effects) — it
    cancels the unwanted goal and tells the cloud 'interrupted'."""
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=True)
    runner.start_task("local/victory_spin", "prim-1", {})
    ws_messages.clear()
    runner._state.primitive_running = None  # the race

    cancels = []
    accepted = SimpleNamespace(
        accepted=True,
        cancel_goal_async=lambda: cancels.append(True),
        get_result_async=lambda: (_ for _ in ()).throw(AssertionError("must not adopt the goal")),
    )
    runner._on_goal_response(
        SimpleNamespace(result=lambda: accepted),
        skill_name="victory_spin",
        primitive_id="prim-1",
        skill_id="local/victory_spin",
    )

    assert cancels == [True]
    assert runner._goal_handle is None
    assert [m.type for m in ws_messages] == [MessageInType.PRIMITIVE_INTERRUPTED]
    assert finished == []


def test_accepted_goal_adopted_when_task_still_current(monkeypatch):
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=True)
    runner.start_task("local/victory_spin", "prim-1", {})

    result_callbacks = []
    accepted = SimpleNamespace(
        accepted=True,
        cancel_goal_async=lambda: (_ for _ in ()).throw(AssertionError("must not cancel")),
        get_result_async=lambda: SimpleNamespace(add_done_callback=result_callbacks.append),
    )
    runner._on_goal_response(
        SimpleNamespace(result=lambda: accepted),
        skill_name="victory_spin",
        primitive_id="prim-1",
        skill_id="local/victory_spin",
    )

    assert runner._goal_handle is accepted
    assert len(result_callbacks) == 1


def test_rejected_goal_leaves_a_newer_running_task_alone(monkeypatch):
    """If another task started after the race cleared ours, its state and gaze
    pause must survive our late rejection."""
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=True)
    runner.start_task("local/victory_spin", "prim-1", {})
    ws_messages.clear()
    newer = {"primitive_name": "wave", "primitive_id": "prim-2", "skill_id": "local/wave"}
    runner._state.primitive_running = dict(newer)

    _reject_goal(runner)

    assert runner._state.primitive_running == newer
    assert finished == []


def test_report_start_failure_used_for_unknown_skill(monkeypatch):
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=True)

    runner.report_start_failure(
        primitive_name="does_not_exist",
        primitive_id="prim-2",
        reason="Unknown skill 'does_not_exist' — not in the registered skill set.",
    )

    types = [m.type for m in ws_messages]
    assert types == [MessageInType.PRIMITIVE_FAILED]
    assert ws_messages[0].payload["primitive_id"] == "prim-2"
    assert runner._state.primitive_running is None
    assert finished == [True]


def test_report_start_failure_keeps_active_task_undisturbed(monkeypatch):
    """An unknown next_task while another primitive runs must tell the cloud
    'failed' but NOT fire on_task_finished (gaze resume) over the active task."""
    runner, ws_messages, chat_statuses, finished = _make_runner(monkeypatch, server_available=True)
    runner.start_task("local/victory_spin", "prim-1", {})
    ws_messages.clear()

    runner.report_start_failure(
        primitive_name="does_not_exist",
        primitive_id="prim-2",
        reason="Unknown skill 'does_not_exist' — not in the registered skill set.",
    )

    types = [m.type for m in ws_messages]
    assert types == [MessageInType.PRIMITIVE_FAILED]
    assert runner._state.primitive_running["primitive_id"] == "prim-1"
    assert finished == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
