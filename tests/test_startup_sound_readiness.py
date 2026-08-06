# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure-Python regression tests for readiness-gated startup audio."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts/play_startup_sound_when_ready.py"
_SPEC = importlib.util.spec_from_file_location("startup_sound_readiness", _MODULE_PATH)
assert _SPEC and _SPEC.loader
startup = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = startup
_SPEC.loader.exec_module(startup)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, duration: float) -> None:
        self.now += duration


@pytest.mark.parametrize("mode", sorted(startup.LIFECYCLE_EXPECTATIONS))
def test_exact_lifecycle_matrix_is_ready(mode):
    states = dict(startup.LIFECYCLE_EXPECTATIONS[mode])
    assert startup.lifecycle_readiness_errors(mode, states) == []


def test_persisted_mode_is_not_ready_before_lifecycle_startup():
    errors = startup.lifecycle_readiness_errors("navigation", {})
    assert errors
    assert any("unreachable" in error for error in errors)


def test_wrong_non_target_lifecycle_state_blocks_readiness():
    states = dict(startup.LIFECYCLE_EXPECTATIONS["navigation"])
    states["/slam_toolbox"] = startup.ACTIVE
    assert startup.lifecycle_readiness_errors("navigation", states) == ["/slam_toolbox=3, expected 1"]


def test_non_final_mode_blocks_readiness():
    assert startup.lifecycle_readiness_errors("switching", {}) == ["navigation mode not final (switching)"]
    assert startup.lifecycle_readiness_errors("", {}) == ["navigation mode not final (unpublished)"]


def test_plays_once_only_after_readiness_is_stable():
    clock = FakeClock()
    samples = iter(
        [
            ("nodes missing",),
            (),
            ("TensorRT startup sweep still running",),
            (),
            (),
            (),
        ]
    )
    events = []

    def probe():
        errors = next(samples)
        events.append((clock.now, "probe", errors))
        return errors

    def play():
        events.append((clock.now, "play", ()))
        return True

    assert startup.run_startup_sequence(
        probe,
        clock.advance,
        play,
        timeout_s=10,
        stable_s=2,
        poll_s=1,
        monotonic=clock.monotonic,
        log=lambda _message: None,
    )
    play_events = [event for event in events if event[1] == "play"]
    assert play_events == [(5.0, "play", ())]


def test_timeout_skips_sound_and_reports_missing_requirement():
    clock = FakeClock()
    played = []
    logs = []

    assert not startup.run_startup_sequence(
        lambda: ("sensor data missing: /scan",),
        clock.advance,
        lambda: played.append(True) or True,
        timeout_s=3,
        stable_s=2,
        poll_s=1,
        monotonic=clock.monotonic,
        log=logs.append,
    )
    assert played == []
    assert any("timed out" in message and "/scan" in message for message in logs)


def test_cancellation_skips_pending_sound():
    clock = FakeClock()
    played = []

    assert not startup.run_startup_sequence(
        lambda: ("not ready",),
        clock.advance,
        lambda: played.append(True) or True,
        timeout_s=30,
        stable_s=2,
        poll_s=1,
        monotonic=clock.monotonic,
        cancelled=lambda: clock.now >= 1,
        log=lambda _message: None,
    )
    assert played == []


def test_player_failure_is_contained_after_readiness():
    clock = FakeClock()
    calls = []

    def fail_player():
        calls.append("play")
        raise RuntimeError("audio device unavailable")

    assert not startup.run_startup_sequence(
        lambda: (),
        clock.advance,
        fail_player,
        timeout_s=1,
        stable_s=0,
        poll_s=1,
        monotonic=clock.monotonic,
        log=lambda _message: None,
    )
    assert calls == ["play"]


def test_launcher_tracks_readiness_helper_instead_of_disowning_fixed_delay():
    launcher = (_REPO_ROOT / "scripts/launch_ros_in_tmux.sh").read_text()
    assert "play_startup_sound_when_ready.py" in launcher
    assert "sleep 20 && XDG_RUNTIME_DIR=/run/user/1000 gst-play-1.0" not in launcher
    helper_block = launcher.split("play_startup_sound_when_ready.py", 1)[1].split("exec sleep infinity", 1)[0]
    assert "STARTUP_SOUND_PID=$!" in helper_block
    assert 'wait "$STARTUP_SOUND_PID"' in helper_block
    assert "disown" not in helper_block
    assert "--allow-missing-cloud" in launcher


def test_runtime_uses_humble_compatible_action_clients():
    helper = _MODULE_PATH.read_text()
    assert "ActionClient" in helper
    assert "server_is_ready()" in helper
    assert "self.get_action_names_and_types(" not in helper
