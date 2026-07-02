# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the chaining ergonomics layer: structured results
(SkillOutput / 3-tuple execute), call-time input validation, the reserved
timeout= kwarg, say(wait=True), and the generated innate/skills.pyi stub."""

import logging
import threading
import time

import innate.skills as skills
import pytest
from brain_messages.msg import SkillInfo
from innate.skills import SkillFailed, use_invoker
from test_skill_invoker import _Catalog, _CodeSkill, _Server

from brain_client.skills.catalog import SkillRepository
from brain_client.skills.invoker import SkillInvoker
from brain_client.skills.types import Skill, SkillOutput, SkillResult, normalize_skill_result


class _NormalizingServer(_Server):
    """Fake server whose code path normalizes like the real _run_code_skill_body."""

    def _run_code_skill_body(self, skill, skill_id, inputs):
        return normalize_skill_result(skill.execute(**inputs))


def _invoker(code=None, feedbacks=None):
    server = _NormalizingServer(_Catalog(code=code or {}))
    return SkillInvoker(server, goal_handle=object(), publish_feedback=(feedbacks or []).append)


# --- structured results -----------------------------------------------------


def test_normalize_two_tuple_keeps_message_and_none_data():
    message, status = normalize_skill_result(("done", SkillResult.SUCCESS))
    assert message == "done" and isinstance(message, SkillOutput)
    assert message.data is None and status is SkillResult.SUCCESS


def test_normalize_three_tuple_carries_data():
    message, status = normalize_skill_result(("found it", SkillResult.SUCCESS, {"x": 1.5}))
    assert message == "found it" and message.data == {"x": 1.5}


def test_proxy_returns_data_from_three_tuple_child():
    class Detector:
        name = "detect"

        def set_feedback_callback(self, cb):
            pass

        def execute(self, **inputs):
            return "board at 1.5, 0.2", SkillResult.SUCCESS, {"x": 1.5, "y": 0.2}

        def cancel(self):
            pass

    invoker = _invoker(code={"local/detect": ("detect", Detector())})
    with use_invoker(invoker):
        result = skills.detect()
    assert result == "board at 1.5, 0.2"  # still the message string
    assert result.data == {"x": 1.5, "y": 0.2}  # payload for the next step


# --- input validation --------------------------------------------------------


def test_typoed_kwarg_fails_at_call_time_with_expected_params():
    class Mover:
        name = "mover"

        def set_feedback_callback(self, cb):
            pass

        def execute(self, distance: float, speed: float = 0.15):
            raise AssertionError("must not run with bad inputs")

        def cancel(self):
            pass

    invoker = _invoker(code={"local/mover": ("mover", Mover())})
    with use_invoker(invoker):
        with pytest.raises(SkillFailed, match="Invalid inputs for 'local/mover'.*distance.*speed"):
            skills.mover(distnace=0.3)  # typo


def test_missing_required_kwarg_fails_at_call_time():
    class Mover:
        name = "mover"

        def set_feedback_callback(self, cb):
            pass

        def execute(self, distance: float):
            raise AssertionError("must not run")

        def cancel(self):
            pass

    invoker = _invoker(code={"local/mover": ("mover", Mover())})
    message, status = invoker.run("mover")
    assert status is SkillResult.FAILURE and "Invalid inputs" in message


# --- timeout= ----------------------------------------------------------------


class _SlowSkill:
    """Blocks until cancelled, like a long navigation."""

    name = "slow"

    def __init__(self):
        self.cancelled = False

    def set_feedback_callback(self, cb):
        pass

    def execute(self, **inputs):
        while not self.cancelled:
            time.sleep(0.01)
        return "stopped", SkillResult.CANCELLED

    def cancel(self):
        self.cancelled = True


def test_timeout_cancels_child_but_not_the_routine():
    slow = _SlowSkill()
    ok = _CodeSkill("after", ("ran after", SkillResult.SUCCESS))
    invoker = _invoker(code={"local/slow": ("slow", slow), "local/after": ("after", ok)})

    message, status = invoker.run("slow", timeout=0.2)

    assert status is SkillResult.FAILURE and "timed out after 0.2s" in message
    assert slow.cancelled  # the child was actually stopped
    message, status = invoker.run("after")  # routine continues
    assert (message, status) == ("ran after", SkillResult.SUCCESS)


def test_timeout_via_proxy_raises_skill_failed():
    invoker = _invoker(code={"local/slow": ("slow", _SlowSkill())})
    with use_invoker(invoker):
        with pytest.raises(SkillFailed, match="timed out"):
            skills.slow(timeout=0.2)


def test_fast_child_is_untouched_by_timeout():
    ok = _CodeSkill("quick", ("done", SkillResult.SUCCESS))
    invoker = _invoker(code={"local/quick": ("quick", ok)})
    message, status = invoker.run("quick", timeout=5.0)
    assert (message, status) == ("done", SkillResult.SUCCESS)


# --- say(wait=True) -----------------------------------------------------------


def test_say_wait_blocks_until_playback_ends():
    class Chatty(Skill):
        @property
        def name(self):
            return "chatty"

        def execute(self):
            return "ok", SkillResult.SUCCESS

    class _Msg:
        def __init__(self, data):
            self.data = data

    subs = {}

    class _Node:
        def create_publisher(self, msg_type, topic, depth):
            class _Pub:
                def publish(self, msg):
                    pass

            return _Pub()

        def create_subscription(self, msg_type, topic, cb, depth):
            subs[topic] = cb
            return object()

    skill = Chatty(logging.getLogger("test"))
    skill.node = _Node()

    def _playback():  # what brain_client_node reports while audio plays
        time.sleep(0.1)
        subs["/tts/is_playing"](_Msg("true"))
        time.sleep(0.2)
        subs["/tts/is_playing"](_Msg("false"))

    threading.Thread(target=_playback, daemon=True).start()
    start = time.time()
    skill.say("hello there", wait=True)
    elapsed = time.time() - start
    assert 0.25 < elapsed < 5.0  # blocked through playback, no runaway wait


# --- generated import stub ----------------------------------------------------


def _info(skill_id, name, inputs_json="{}", guidelines=""):
    msg = SkillInfo()
    msg.id = skill_id
    msg.name = name
    msg.inputs_json = inputs_json
    msg.guidelines = guidelines
    return msg


def test_stub_renders_typed_defs_and_static_names():
    stub = SkillRepository._render_import_stub(
        [
            _info(
                "innate-os/move_straight",
                "move_straight",
                '{"distance": {"type": "float", "required": true}, "speed": {"type": "float", "required": false}}',
                "Move the robot straight.",
            ),
            _info("local/hello", "Hello"),
            _info("local/my-dashed-dir", "dashed"),  # not importable -> skipped
        ]
    )
    assert (
        "def move_straight(distance: float, speed: float = ..., *, timeout: float | None = ...) -> SkillOutput:" in stub
    )
    assert '"""Move the robot straight."""' in stub
    assert "def hello(*, timeout: float | None = ...) -> SkillOutput:" in stub
    assert "my-dashed-dir" not in stub
    for real_name in ("SkillFailed", "SkillCancelled", "use_invoker"):
        assert real_name in stub  # a .pyi must re-declare the module's real API


def test_stub_prefers_local_over_shipped_for_same_stem():
    stub = SkillRepository._render_import_stub(
        [
            _info("innate-os/wave", "wave", '{"speed": {"type": "float", "required": true}}'),
            _info("local/wave", "wave", "{}"),
        ]
    )
    assert "def wave(*, timeout: float | None = ...) -> SkillOutput:" in stub  # local signature won
    assert "speed: float" not in stub
