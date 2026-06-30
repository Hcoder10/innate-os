"""Unit tests for manipulation.auto_stop.LearnedStopDetector.

ROS-free, runnable directly::

    cd ros2_ws/src/brain/manipulation
    python -m pytest test/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the in-tree package importable without rebuilding the colcon workspace.
_MANIPULATION_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_MANIPULATION_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_MANIPULATION_PKG_ROOT))

from manipulation.auto_stop import LearnedStopDetector, StepSignals  # noqa: E402

# A clearly-moving step: arm and base both well above the default idle thresholds.
MOVING = StepSignals(progress=0.0, arm_delta=1.0, base_speed=1.0)
# A clearly-still step (arm + base below the default 0.01 thresholds).
STILL = StepSignals(progress=0.0, arm_delta=0.0, base_speed=0.0)


def test_defaults_reduce_to_legacy_progress_check():
    """With no knobs set, the detector is just ``progress > progress_threshold``."""
    det = LearnedStopDetector(progress_threshold=0.9)
    # Below threshold: keep going.
    assert det.update(StepSignals(0.5, 1.0, 1.0), elapsed=10.0, now=0.0) == (False, None)
    # Above threshold: stop.
    stop, reason = det.update(StepSignals(0.95, 1.0, 1.0), elapsed=10.0, now=0.0)
    assert stop and "progress" in reason


def test_idle_disabled_by_default():
    """A motionless robot does not stop unless idle detection is enabled."""
    det = LearnedStopDetector(progress_threshold=0.9)  # idle_seconds defaults to 0
    for t in range(10):
        assert det.update(STILL, elapsed=float(t), now=float(t)) == (False, None)


def test_min_duration_floor_blocks_early_stop():
    det = LearnedStopDetector(progress_threshold=0.9, min_duration=5.0)
    # Progress is over threshold, but we're inside the floor: no stop.
    assert det.update(StepSignals(1.0, 1.0, 1.0), elapsed=1.0, now=0.0) == (False, None)
    # Past the floor: stop.
    stop, _ = det.update(StepSignals(1.0, 1.0, 1.0), elapsed=6.0, now=0.0)
    assert stop


def test_idle_stop_fires_after_dwell_with_progress_gate():
    det = LearnedStopDetector(idle_seconds=2.0, progress_gate=0.5)
    held_still = StepSignals(progress=0.6, arm_delta=0.0, base_speed=0.0)
    # Idle starts accumulating at now=100.0.
    assert det.update(held_still, elapsed=10.0, now=100.0) == (False, None)
    # Still short of the dwell window.
    assert det.update(held_still, elapsed=11.0, now=101.0) == (False, None)
    # Dwell satisfied (2.0s elapsed since idle began) and progress past the gate.
    stop, reason = det.update(held_still, elapsed=12.0, now=102.0)
    assert stop and "idle" in reason


def test_progress_gate_blocks_idle_stop_when_not_engaged():
    det = LearnedStopDetector(idle_seconds=2.0, progress_gate=0.5)
    low_progress_still = StepSignals(progress=0.2, arm_delta=0.0, base_speed=0.0)
    det.update(low_progress_still, elapsed=10.0, now=100.0)
    # Long past the dwell window, but progress never reached the gate.
    assert det.update(low_progress_still, elapsed=20.0, now=110.0) == (False, None)


def test_motion_resets_idle_timer():
    det = LearnedStopDetector(idle_seconds=2.0)  # progress_gate 0 -> idle alone may stop
    det.update(STILL, elapsed=10.0, now=100.0)  # idle starts at 100.0
    det.update(MOVING, elapsed=10.5, now=100.5)  # moved -> timer resets
    # Idle resumes at 101.0; only 1.0s has passed by 102.0, short of the 2.0s dwell.
    assert det.update(STILL, elapsed=11.0, now=101.0) == (False, None)
    assert det.update(STILL, elapsed=12.0, now=102.0) == (False, None)
    # 2.0s after the reset: now it fires.
    stop, _ = det.update(STILL, elapsed=13.0, now=103.0)
    assert stop


def test_first_step_missing_motion_is_not_idle():
    """arm_delta is None on the first step; that must not count as 'still'."""
    det = LearnedStopDetector(idle_seconds=0.5)
    first = StepSignals(progress=0.6, arm_delta=None, base_speed=0.0)
    assert det.update(first, elapsed=10.0, now=100.0) == (False, None)
    assert det.update(first, elapsed=11.0, now=101.0) == (False, None)


def test_ema_smoothing_rejects_a_single_spike():
    det = LearnedStopDetector(progress_threshold=0.9, progress_ema_alpha=0.3)
    # Establish a low baseline.
    det.update(StepSignals(0.0, 1.0, 1.0), elapsed=10.0, now=0.0)
    det.update(StepSignals(0.0, 1.0, 1.0), elapsed=10.0, now=1.0)
    # A lone spike to 1.0 only pulls the EMA to 0.3 -> below threshold, no stop.
    stop, _ = det.update(StepSignals(1.0, 1.0, 1.0), elapsed=10.0, now=2.0)
    assert not stop


def test_ema_reaches_threshold_when_progress_sustains():
    det = LearnedStopDetector(progress_threshold=0.9, progress_ema_alpha=0.5)
    stop = False
    for i in range(20):
        stop, _ = det.update(StepSignals(1.0, 1.0, 1.0), elapsed=10.0, now=float(i))
        if stop:
            break
    assert stop
