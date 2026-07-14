# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the registration gate (INN-711 root cause).

Registration is the cloud's licence to trigger skills, so it must not be sent
before the execute_skill action server is discoverable — otherwise the very
first trigger races graph discovery and fails. The gate defers registration on
a retry timer and fails open (loudly) after a window so a wedged skills server
degrades to per-trigger "failed" reports instead of a brain that never
registers.
"""

import time
from types import SimpleNamespace

import pytest

from brain_client.skills.registration import REGISTRATION_GATE_FAIL_OPEN_SEC, SkillCatalog
from brain_client.transport.messages import MessageInType


class FakeLogger:
    def info(self, msg):
        pass

    def warn(self, msg):
        pass

    def error(self, msg):
        pass


class FakeNode:
    def __init__(self):
        self.timers = []

    def get_logger(self):
        return FakeLogger()

    def create_subscription(self, *args, **kwargs):
        return None

    def create_timer(self, period, callback):
        timer = SimpleNamespace(period=period, callback=callback)
        self.timers.append(timer)
        return timer

    def destroy_timer(self, timer):
        self.timers.remove(timer)


def _make_catalog(ready, gated=True):
    node = FakeNode()
    messages = []
    ws = SimpleNamespace(send_message=messages.append)
    directive = SimpleNamespace(id="patrol", get_prompt=lambda: "patrol the flat", get_skills=lambda: ["s1"])
    state = SimpleNamespace(
        current_directive=directive,
        registry=SimpleNamespace(metadata=[{"id": "s1", "name": "victory_spin"}]),
        active_skill_ids=None,
        token="tok",
        primitives_registered=False,
    )
    flag = {"ready": ready}
    catalog = SkillCatalog(node, ws, state, execute_skill_ready=(lambda: flag["ready"]) if gated else None)
    return catalog, node, messages, flag


def test_registration_deferred_while_server_not_ready():
    catalog, node, messages, flag = _make_catalog(ready=False)

    catalog.register()

    assert messages == []
    assert len(node.timers) == 1


def test_registration_sent_once_server_becomes_ready():
    catalog, node, messages, flag = _make_catalog(ready=False)
    catalog.register()

    flag["ready"] = True
    node.timers[0].callback()

    assert [m.type for m in messages] == [MessageInType.REGISTER_PRIMITIVES_AND_DIRECTIVE]
    assert node.timers == []  # gate timer cleaned up


def test_registration_fails_open_after_window():
    catalog, node, messages, flag = _make_catalog(ready=False)
    catalog.register()
    assert messages == []

    catalog._gate_started = time.monotonic() - (REGISTRATION_GATE_FAIL_OPEN_SEC + 1)
    node.timers[0].callback()

    assert [m.type for m in messages] == [MessageInType.REGISTER_PRIMITIVES_AND_DIRECTIVE]
    assert node.timers == []


def test_registration_immediate_when_gate_disabled():
    catalog, node, messages, flag = _make_catalog(ready=False, gated=False)

    catalog.register()

    assert [m.type for m in messages] == [MessageInType.REGISTER_PRIMITIVES_AND_DIRECTIVE]
    assert node.timers == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
