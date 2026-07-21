# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the activation registration window (INN-711 startup variant).

The webapp's Start sends set_directive (which registers the catalog) and
set_brain_active back-to-back. _finish_reactivation used to unregister and
re-register unconditionally 0.5s later — closing the trigger gate
(primitives_registered) for a ~2s round trip exactly while the cloud's first
decision (VLM latency 2-4s) was in flight, so the reply was dropped and the
agent started wedged on a phantom "running" primitive. Pins the fix: an acked
registration for the current directive survives reactivation untouched.
"""

from types import SimpleNamespace

from brain_client.core.lifecycle import BrainLifecycle
from brain_client.core.state import BrainState


class FakeLogger:
    def info(self, msg):
        pass

    def warn(self, msg):
        pass

    def error(self, msg):
        pass

    def debug(self, msg):
        pass


class FakeTimer:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def is_canceled(self):
        return self.cancelled


def _make_lifecycle(state):
    catalog = SimpleNamespace(register_calls=0)
    catalog.register = lambda: setattr(catalog, "register_calls", catalog.register_calls + 1)
    runner = SimpleNamespace(abort_calls=0)
    runner.abort_running = lambda: setattr(runner, "abort_calls", runner.abort_calls + 1)
    lifecycle = BrainLifecycle(
        SimpleNamespace(
            get_logger=lambda: FakeLogger(), create_timer=lambda *a: FakeTimer(), destroy_timer=lambda t: None
        ),
        state,
        SimpleNamespace(),
        ws_bridge=SimpleNamespace(send_message=lambda m: None),
        camera=SimpleNamespace(),
        pose_tracker=SimpleNamespace(),
        runner=runner,
        gaze=SimpleNamespace(),
        chat=SimpleNamespace(),
        orchestrator=SimpleNamespace(),
        catalog=catalog,
        active_inputs_pub=SimpleNamespace(publish=lambda m: None),
        stop_robot=lambda: None,
        publish_status=lambda: None,
    )
    lifecycle._reactivate_timer = FakeTimer()
    return lifecycle, catalog, runner


def test_acked_registration_survives_reactivation():
    # set_directive registered and the ack landed: the gate is open and the
    # first image may already be answered mid-flight — reactivation must not
    # close it, re-register, or abort anything.
    state = BrainState(is_brain_active=True, primitives_registered=True)
    lifecycle, catalog, runner = _make_lifecycle(state)

    lifecycle._finish_reactivation()

    assert state.primitives_registered is True  # gate never closed
    assert catalog.register_calls == 0
    assert runner.abort_calls == 0
    assert state.ready_for_image is True


def test_plain_activation_still_registers():
    # set_brain_active without a preceding set_directive (deactivate cleared
    # the flag): reactivation must still register the catalog.
    state = BrainState(is_brain_active=True, primitives_registered=False)
    lifecycle, catalog, _runner = _make_lifecycle(state)

    lifecycle._finish_reactivation()

    assert catalog.register_calls == 1
    assert state.ready_for_image is True


def test_deactivated_during_wait_is_a_noop():
    state = BrainState(is_brain_active=False, primitives_registered=False)
    lifecycle, catalog, _runner = _make_lifecycle(state)

    lifecycle._finish_reactivation()

    assert catalog.register_calls == 0
    assert state.ready_for_image is False
