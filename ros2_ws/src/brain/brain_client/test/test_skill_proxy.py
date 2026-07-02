# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the innate.skills function proxies.

Covers: the ambient-invoker contextvar (use_invoker), proxy calls resolving by
name and returning the child's message, raise-on-failure/cancel semantics, the
guard against calls outside a skill run, the default Skill.cancel() delegation,
and the proxies driving a real SkillInvoker end to end (fakes from the invoker
tests, no ROS runtime needed).
"""

import logging

import innate.skills as skills
import pytest
from innate.skills import SkillCancelled, SkillFailed, use_invoker
from test_skill_invoker import _Catalog, _CodeSkill, _Server

from brain_client.skills.invoker import SkillInvoker
from brain_client.skills.types import Skill, SkillResult


class _FakeInvoker:
    """The whole invoker surface the proxies need: run() and cancel()."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}
        self.cancelled = False

    def run(self, skill_id, **inputs):
        self.calls.append((skill_id, inputs))
        return self.results.get(skill_id, ("ok", SkillResult.SUCCESS))

    def cancel(self):
        self.cancelled = True
        return "Routine cancelled"


def test_proxy_outside_skill_run_raises():
    wave = skills.wave  # attribute access works anywhere; calling needs context
    with pytest.raises(RuntimeError, match="while a skill is executing"):
        wave()


def test_proxy_runs_by_name_and_returns_message():
    invoker = _FakeInvoker()
    with use_invoker(invoker):
        assert skills.wave(speed=2) == "ok"
    assert invoker.calls == [("wave", {"speed": 2})]


def test_failure_raises_skill_failed():
    invoker = _FakeInvoker({"grab": ("no object", SkillResult.FAILURE)})
    with use_invoker(invoker):
        with pytest.raises(SkillFailed, match="no object"):
            skills.grab()


def test_cancellation_raises_skill_cancelled():
    invoker = _FakeInvoker({"nav": ("stopped", SkillResult.CANCELLED)})
    with use_invoker(invoker):
        with pytest.raises(SkillCancelled, match="stopped"):
            skills.nav()


def test_use_invoker_restores_previous_context():
    outer, inner = _FakeInvoker(), _FakeInvoker()
    with use_invoker(outer):
        with use_invoker(inner):
            skills.wave()
        skills.wave()
    assert inner.calls == [("wave", {})]
    assert outer.calls == [("wave", {})]


def test_private_names_are_not_skills():
    with pytest.raises(AttributeError):
        skills.__wrapped__  # noqa: B018 -- attribute access is the test


def test_default_cancel_delegates_to_invoker():
    class ChainOnly(Skill):
        @property
        def name(self):
            return "chain_only"

        def execute(self):
            return "ok", SkillResult.SUCCESS

    skill = ChainOnly(logging.getLogger("test"))
    assert skill.cancel() == "Nothing to cancel"  # no invoker injected yet

    skill.skills = _FakeInvoker()
    assert skill.cancel() == "Routine cancelled"
    assert skill.skills.cancelled


def test_say_publishes_to_tts_topic_and_is_safe_without_node():
    class Chatty(Skill):
        @property
        def name(self):
            return "chatty"

        def execute(self):
            return "ok", SkillResult.SUCCESS

    skill = Chatty(logging.getLogger("test"))
    skill.say("hello")  # no node injected -> silent no-op, must not raise

    published = []

    class _Pub:
        def publish(self, msg):
            published.append(msg.data)

    class _Node:
        def create_publisher(self, msg_type, topic, depth):
            assert topic == "/brain/tts"
            return _Pub()

    skill.node = _Node()
    skill.say("hello robot")
    skill.say("")  # empty text is dropped
    assert published == ["hello robot"]


def test_proxy_resolves_physical_skill_from_any_scan_dir():
    """A learned/replay skill gets a local/<dirname> id no matter which scan
    directory it lives in (workspace/custom_skills, ~/skills, legacy, extras),
    so `from innate.skills import hello` reaches e.g. ~/skills/hello."""
    server = _Server(
        _Catalog(physical={"local/hello": {"metadata": {"name": "Hello"}, "directory": "/home/x/skills/hello"}})
    )
    invoker = SkillInvoker(server, goal_handle=object(), publish_feedback=lambda *_: None)

    with use_invoker(invoker):
        assert skills.hello() == "ok"  # bare name -> local/hello -> behavior server


def test_invoker_cancel_with_default_child_cancel_does_not_recurse():
    """A mid-run child using the default Skill.cancel() delegates back to the
    invoker that is cancelling it; that must terminate, not recurse."""

    cancel_calls = []

    class LeafChild(Skill):
        @property
        def name(self):
            return "leaf"

        def execute(self):
            return "ok", SkillResult.SUCCESS

        def cancel(self):
            cancel_calls.append(1)
            return super().cancel()  # the default delegation, counted

    child = LeafChild(logging.getLogger("test"))
    server = _Server(_Catalog(code={"local/leaf": ("leaf", child)}))
    invoker = SkillInvoker(server, goal_handle=object(), publish_feedback=lambda *_: None)
    child.skills = invoker  # what _run_code sets up while the child executes
    invoker._active_code_skill = child

    assert invoker.cancel() == "Routine cancelled"
    assert len(cancel_calls) == 1  # not ~1000 with a swallowed RecursionError
    _message, status = invoker.run("local/leaf")  # routine stays cancelled
    assert status is SkillResult.CANCELLED


def test_proxies_drive_a_real_invoker():
    """End to end minus ROS: proxy -> SkillInvoker -> fake server -> child skill."""
    ok = _CodeSkill("step_ok", ("done", SkillResult.SUCCESS))
    bad = _CodeSkill("step_bad", ("nope", SkillResult.FAILURE))
    server = _Server(
        _Catalog(
            code={
                "local/step_ok": ("step_ok", ok),
                "innate-os/step_bad": ("step_bad", bad),
            }
        )
    )
    invoker = SkillInvoker(server, goal_handle=object(), publish_feedback=lambda *_: None)

    with use_invoker(invoker):
        assert skills.step_ok(x=1) == "done"  # bare name resolves via local/
        with pytest.raises(SkillFailed, match="nope"):
            skills.step_bad()  # bare name falls back to innate-os/
    assert ok.last_inputs == {"x": 1}
